"""Reset-related accessors and helpers (sketch §15 / §22).

Implements the reset-value surface described in ``docs/IDEAL_API_SKETCH.md``
§12.3 / §22. Three callables are wired in via the runtime registry:

* ``reg.reset_value`` — the integer reset value for a register, read from
  the per-register metadata dict stashed on the class as
  ``__peakrdl_meta__["reset"]`` by :mod:`_default_shims`. ``None`` when
  the metadata doesn't carry a reset (the register is "not resettable"
  from the runtime's perspective; the underlying RDL may still have a
  reset value, but it wasn't surfaced to us).
* ``reg.is_at_reset()`` — read the register and compare to its reset
  value. Returns ``False`` when the register has no reset metadata
  (rather than ``None``) to keep the type signature simple ``bool`` —
  callers that need to distinguish "no reset" from "not at reset" should
  read ``reg.reset_value is None`` directly.
* ``soc.reset_all()`` / ``soc.<subtree>.reset_all()`` — walk the subtree
  rooted at the receiver and write each resettable register's reset
  value to the bus via ``reg.write(reset_value, raw=True)``. The
  ``rw_only=True`` keyword skips registers whose underlying fields are
  all non-writable (read-only registers).

Registration uses Unit 1's seams:

* :func:`_attach_reset_accessors` is a register enhancement — fires once
  per generated register class to install ``reset_value`` and
  ``is_at_reset`` on the class.
* :func:`_attach_reset_all` is a post-create hook — walks the live SoC
  tree once and attaches a bound ``reset_all`` to every container
  (the root and every regfile/addrmap-like inner node).

When the registry seam isn't present (this module imported in isolation)
the bottom-of-module wiring silently no-ops. Callers can still drive the
free functions directly.
"""

from __future__ import annotations

import logging
import types
from collections.abc import Iterator
from typing import Any, cast

logger = logging.getLogger("peakrdl_pybind11.runtime.reset")

__all__ = [
    "attach_reset_all",
    "is_at_reset",
    "reset_all",
    "reset_value_of",
]


# ---------------------------------------------------------------------------
# Metadata lookups
# ---------------------------------------------------------------------------


def _metadata_for(cls_or_instance: Any) -> dict[str, Any] | None:
    """Return the ``__peakrdl_meta__`` dict for ``cls_or_instance`` or ``None``.

    Looks on the class first (where :mod:`_default_shims` stashes it), then
    on the instance as a fallback for hand-rolled mocks that may put it
    on the instance.
    """

    cls = cls_or_instance if isinstance(cls_or_instance, type) else type(cls_or_instance)
    meta = getattr(cls, "__peakrdl_meta__", None)
    if isinstance(meta, dict):
        return meta
    meta = getattr(cls_or_instance, "__peakrdl_meta__", None)
    if isinstance(meta, dict):
        return meta
    return None


def reset_value_of(reg: Any) -> int | None:
    """Return the integer reset value for ``reg`` or ``None`` if not resettable.

    Free-function form of the ``reg.reset_value`` accessor; useful when
    the register hasn't been routed through the registry seam (e.g.
    in-line tests).
    """

    meta = _metadata_for(reg)
    if meta is None:
        return None
    raw = meta.get("reset")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_register_writable(reg: Any) -> bool:
    """Return ``True`` when at least one of ``reg``'s fields is software-writable.

    Reads the ``writable`` sub-dict from ``__peakrdl_meta__``. Missing or
    empty metadata is treated as *permissive* (returns ``True``) — same
    convention as :func:`_default_register_shim` uses for the readable
    spec. This keeps hand-rolled mocks and pre-enforcement metadata
    pipelines working unchanged.
    """

    meta = _metadata_for(reg)
    if meta is None:
        return True
    writable = meta.get("writable")
    if not isinstance(writable, dict) or not writable:
        # No per-field writable info — be permissive.
        return True
    return any(bool(v) for v in writable.values())


# ---------------------------------------------------------------------------
# Register-side accessors
# ---------------------------------------------------------------------------


def is_at_reset(reg: Any) -> bool:
    """Return ``True`` if ``reg.read(raw=True)`` equals the register's reset value.

    Returns ``False`` when the register has no reset metadata (i.e.
    :func:`reset_value_of` would return ``None``). Read with
    ``raw=True`` so we bypass the typed wrapper and avoid building a
    :class:`~peakrdl_pybind11.runtime.values.RegisterValue` for a value
    we only need to integer-compare.
    """

    expected = reset_value_of(reg)
    if expected is None:
        return False
    read = getattr(reg, "read", None)
    if not callable(read):
        return False
    try:
        actual = read(raw=True)
    except TypeError:
        # The bound read may not accept ``raw=`` on hand-rolled mocks; fall
        # back to a plain ``read()`` and coerce to int.
        actual = read()
    return int(cast(int, actual)) == int(expected)


def _attach_reset_accessors(cls: type, metadata: dict) -> None:
    """Register enhancement: attach ``reset_value`` and ``is_at_reset`` to ``cls``.

    ``reset_value`` is exposed as a plain class attribute (an ``int`` or
    ``None``) so ``reg.reset_value`` is a cheap attribute read with no
    descriptor overhead — the value is fixed at class-attach time.
    ``is_at_reset`` is a method that re-reads the register at call time.

    Idempotent: re-attaching the same attribute is harmless.
    """

    # Don't shadow an attribute the class (or a sibling unit) has already
    # defined — the register may be a hand-shaped descriptor that exposes
    # ``reset_value`` natively.
    if not hasattr(cls, "reset_value"):
        raw = metadata.get("reset") if isinstance(metadata, dict) else None
        try:
            cls.reset_value = int(raw) if raw is not None else None  # type: ignore[attr-defined]
        except (TypeError, ValueError):
            cls.reset_value = None  # type: ignore[attr-defined]

    if not hasattr(cls, "is_at_reset") or not callable(getattr(cls, "is_at_reset", None)):
        cls.is_at_reset = is_at_reset  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tree walking — local copies of the routing-module helpers
#
# We deliberately don't import ``_walk`` / ``_kind_for`` from
# :mod:`routing` directly. The task brief says "use ``soc.walk(kind="reg")``
# if available — else duck-typed tree walk". Mirroring the duck typing
# here keeps the module dependency-light and avoids the ``"reg" in
# "regfile"`` substring trap in :func:`routing._matches_kind`.
# ---------------------------------------------------------------------------


_CHILD_ATTR_HINTS = ("read", "write", "bits", "lsb", "offset", "info")


def _is_register(node: Any) -> bool:
    """Duck-typed register check: callable ``read`` and ``write``, no ``bits``/``lsb``.

    Mirrors the leaf detection in :mod:`routing` but tightened to exclude
    Field-like objects (which also expose ``read``/``write``). The
    ``__peakrdl_meta__`` attribute on the class is a near-certain
    register marker when present.
    """

    if node is None:
        return False
    has_meta = isinstance(getattr(type(node), "__peakrdl_meta__", None), dict)
    has_rw = callable(getattr(node, "read", None)) and callable(getattr(node, "write", None))
    has_bits = hasattr(node, "bits") or hasattr(node, "lsb")
    if has_bits:
        # Field-like — its ``read``/``write`` aren't register-shaped.
        return False
    if has_meta:
        return True
    if has_rw:
        # Last-resort duck check for hand-rolled mocks without meta.
        return True
    return False


def _looks_like_container(value: Any) -> bool:
    """Duck-typed container-or-leaf detection used by :func:`_walk_subtree`."""

    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return False
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return False
    if callable(value) and not hasattr(value, "__dict__"):
        return False
    if any(hasattr(value, attr) for attr in _CHILD_ATTR_HINTS):
        return True
    try:
        nested = vars(value)
    except TypeError:
        return False
    for child_name, child in nested.items():
        if child_name.startswith("_") or child is value:
            continue
        if any(hasattr(child, attr) for attr in _CHILD_ATTR_HINTS):
            return True
    return False


def _iter_children(node: Any) -> Iterator[Any]:
    """Yield duck-typed child nodes of ``node``."""

    try:
        items = vars(node)
    except TypeError:
        return
    for name, value in items.items():
        if name.startswith("_") or name in ("parent", "master", "info"):
            continue
        if value is node:
            continue
        if _looks_like_container(value):
            yield value


def _walk_subtree(root: Any) -> Iterator[Any]:
    """Pre-order DFS over ``root`` using a cycle-safe visited set.

    Prefers ``root.walk()`` when present (so generated SoCs with the
    discovery API installed by :mod:`routing` get the same traversal),
    but avoids the discovery wrapper's ``kind=`` substring filter — we
    apply our own :func:`_is_register` predicate at the call site to
    avoid the ``"reg" in "regfile"`` false-positive trap.
    """

    walk_fn = getattr(root, "walk", None)
    if callable(walk_fn) and not getattr(walk_fn, "__peakrdl_discovery_walk__", False):
        try:
            from collections.abc import Iterable

            seq: list[Any] = list(cast("Iterable[Any]", walk_fn()))
        except (TypeError, AttributeError):
            seq = []
        if seq:
            if seq[0] is not root:
                seq.insert(0, root)
            seen: set[int] = set()
            for n in seq:
                key = id(n)
                if key in seen:
                    continue
                seen.add(key)
                yield n
            return

    visited: set[int] = set()
    stack: list[Any] = [root]
    while stack:
        cur = stack.pop()
        key = id(cur)
        if key in visited:
            continue
        visited.add(key)
        yield cur
        children = list(_iter_children(cur))
        for child in reversed(children):
            stack.append(child)


# ---------------------------------------------------------------------------
# reset_all — free function + bound-method seam
# ---------------------------------------------------------------------------


def reset_all(root: Any, *, rw_only: bool = False) -> list[Any]:
    """Write each resettable register's reset value under ``root``.

    Walks the subtree rooted at ``root`` and, for every register that
    carries a ``reset`` entry in its ``__peakrdl_meta__``, issues
    ``reg.write(reset_value, raw=True)``. Registers without reset
    metadata are silently skipped (no-op).

    With ``rw_only=True``, registers whose fields are all
    non-software-writable are skipped. The decision uses the
    ``writable`` sub-dict in the per-register metadata; if the dict is
    missing or empty, the register is *included* (permissive default,
    matching the rest of the runtime).

    Returns the list of registers that were written, in traversal order.
    Useful for tests and for callers that want to verify exactly which
    registers got touched.
    """

    written: list[Any] = []
    for node in _walk_subtree(root):
        if not _is_register(node):
            continue
        expected = reset_value_of(node)
        if expected is None:
            continue
        if rw_only and not _is_register_writable(node):
            continue
        write = getattr(node, "write", None)
        if not callable(write):
            continue
        try:
            write(int(expected), raw=True)
        except TypeError:
            # Mocks may not accept ``raw=``; fall back to positional write.
            write(int(expected))
        written.append(node)
    return written


def attach_reset_all(soc: Any) -> Any:
    """Attach a bound ``reset_all`` to ``soc`` and every container under it.

    Walks ``soc`` once and, for every node that *isn't* a register (so
    the root SoC and every regfile/addrmap-like inner container),
    binds a ``reset_all(*, rw_only=False)`` method that calls
    :func:`reset_all` against that node. Existing ``reset_all``
    attributes are left alone so user-defined or natively-provided
    implementations win.

    Returns ``soc`` for fluent chaining. Errors from individual
    ``setattr`` calls (pybind11 slotted classes without
    ``py::dynamic_attr()``) are swallowed silently — same policy as
    :mod:`routing.attach_discovery`.
    """

    if soc is None:
        return soc

    for node in _walk_subtree(soc):
        # Skip leaves — only attach the bound method to containers (the
        # root and every regfile/addrmap-like inner node). Registers are
        # not themselves subtrees, so attaching ``reset_all`` to one
        # would be misleading.
        if _is_register(node):
            continue
        if hasattr(node, "reset_all") and callable(getattr(node, "reset_all", None)):
            # Don't shadow an existing (user-defined or native) impl.
            continue

        def _bound_reset_all(self: Any, *, rw_only: bool = False) -> list[Any]:
            return reset_all(self, rw_only=rw_only)

        try:
            node.reset_all = types.MethodType(_bound_reset_all, node)
        except (AttributeError, TypeError):
            # Pybind11 classes without dynamic_attr — nothing we can do.
            logger.debug("could not attach reset_all to %r", node)

    return soc


# ---------------------------------------------------------------------------
# Registry wiring (sibling-dep: Unit 1's runtime/_registry).
# ---------------------------------------------------------------------------


try:  # pragma: no cover - depends on Unit 1 landing order
    from . import _registry  # type: ignore[attr-defined]
except ImportError:
    _registry = None  # type: ignore[assignment]


if _registry is not None and hasattr(_registry, "register_register_enhancement"):
    _registry.register_register_enhancement(_attach_reset_accessors)
if _registry is not None and hasattr(_registry, "register_post_create"):
    _registry.register_post_create(attach_reset_all)
