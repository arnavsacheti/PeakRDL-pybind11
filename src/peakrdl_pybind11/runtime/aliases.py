"""Alias node helpers for the PeakRDL-pybind11 runtime.

Implements the surface described in ``docs/IDEAL_API_SKETCH.md`` §10 and the
companion concept page ``docs/concepts/aliases.rst``.

SystemRDL ``alias`` lets multiple register definitions point at the same
address. The Python view treats one as **canonical** and the rest as
**views**; reads and writes through any view land at the same underlying
address, but the field set and access policy may differ.

This module wires the relationship at module-load time onto the generated
register classes:

- ``alt.target``  — the canonical register class at the same address.
- ``alt.is_alias`` — ``True`` for alias views, ``False`` on the canonical.
- ``primary.aliases`` — tuple of all aliases pointing at this primary.
- ``alt.info.alias_kind`` (defined in Unit 4's :mod:`info` module) is
  consumed by the repr override below; this module never re-defines it.

Detection (which class is an alias of what) is the exporter's job
(Unit 23). The runtime consumes the detection metadata via two seams:

* a **register enhancement** — fires per generated register class with
  its metadata dict; if the dict carries ``alias`` / ``alias_target``,
  the relationship is wired immediately.
* a **post-create hook** — once the SoC instance exists, we import the
  sibling ``aliases.py`` emitted by Unit 23's exporter plugin (the
  ``{path: target_path}`` mapping) and walk the tree to resolve those
  paths to the live register classes. Mirrors how :mod:`interrupts` and
  :mod:`trace` plug into Unit 1's registry.

The local ``register_register_enhancement`` shim below predates Unit 1
and accepts a single-argument callable; it stays for the in-module tests
that drive the seam manually. Production wiring goes through
``runtime/_registry.py``.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger("peakrdl_pybind11.runtime.aliases")

__all__ = [
    "apply_alias_relationship",
    "make_alias_repr",
    "register_register_enhancement",
    "registered_enhancements",
    "reset_enhancements",
]


# ---------------------------------------------------------------------------
# Local stub for Unit 1's enhancement registry.
#
# TODO(Unit 1): Replace this with an import from
# ``peakrdl_pybind11.runtime._registry`` once that module lands. The signature
# of :func:`register_register_enhancement` is intentionally kept narrow (a
# single-argument callable) so it can re-export the upstream registry verbatim.
# ---------------------------------------------------------------------------

EnhancementCallable = Callable[[type], None]

_REGISTERED_ENHANCEMENTS: list[EnhancementCallable] = []


def register_register_enhancement(enhancement: EnhancementCallable) -> EnhancementCallable:
    """Register a class-level enhancement callable.

    The callable is stored for later invocation against generated register
    classes by the per-SoC runtime template. Returns the callable so the
    function can be used as a decorator.

    This is a local placeholder. Unit 1 will own the canonical registry; the
    contract here is intentionally narrow so the upstream version can be a
    drop-in replacement.
    """

    _REGISTERED_ENHANCEMENTS.append(enhancement)
    return enhancement


def registered_enhancements() -> tuple[EnhancementCallable, ...]:
    """Return the currently-registered enhancement callables (for tests)."""

    return tuple(_REGISTERED_ENHANCEMENTS)


def reset_enhancements() -> None:
    """Clear the registered enhancements (test-only helper)."""

    _REGISTERED_ENHANCEMENTS.clear()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def make_alias_repr(alt_cls: type, primary_cls: type) -> Callable[[object], str]:
    """Build a ``__repr__`` override that names the alias relationship.

    The output matches the form documented in the sketch §10, e.g.::

        <Reg uart.control_alt @ 0x40001000  alias-of=uart.control  rw>

    The primary's path is captured at wiring time so the alias still renders
    sensibly even if its ``info`` is later mutated.
    """

    primary_path = _node_path(primary_cls)

    def __repr__(self: Any) -> str:
        info = getattr(self, "info", None)
        kind = getattr(self, "_kind_label", "Reg")
        path = _info_lookup(info, "path", default=type(self).__name__)
        address = _info_lookup(info, "address", default=None)
        access = _info_lookup(info, "access", default="rw")

        addr_part = ""
        if isinstance(address, int):
            addr_part = f" @ 0x{address:08x}"
        elif address is not None:
            addr_part = f" @ {address}"

        access_label = _access_label(access)
        return f"<{kind} {path}{addr_part}  alias-of={primary_path}  {access_label}>"

    return __repr__


def apply_alias_relationship(
    alt_cls: type,
    primary_cls: type,
    *,
    kind: str | None = None,
) -> None:
    """Wire ``alt_cls`` as an alias of ``primary_cls``.

    Side effects on the classes (idempotent — safe to call twice):

    - ``alt_cls.target`` is set to ``primary_cls``.
    - ``alt_cls.is_alias`` is set to ``True``.
    - ``alt_cls.aliases`` is set to ``()`` (an alias has no further aliases).
    - ``primary_cls.aliases`` is updated to include ``alt_cls`` (de-duplicated).
    - ``primary_cls.is_alias`` is set to ``False`` if not already set.
    - ``primary_cls.target`` is set to ``primary_cls`` (canonical points at self).
    - ``alt_cls.__repr__`` is replaced with one that names the relationship.
    - When ``kind`` is given and the alt's ``info`` has no ``alias_kind``
      already, the kind is attached as ``info.alias_kind`` for backwards
      compatibility with metadata pipelines that don't pre-populate it.

    Parameters
    ----------
    alt_cls:
        The generated register class detected as an alias.
    primary_cls:
        The canonical register class at the same address.
    kind:
        Optional alias kind hint. Per the sketch §10 vocabulary:
        ``{"full", "sw_view", "hw_view", "scrambled"}``. Unit 4's ``Info``
        already exposes :attr:`alias_kind`; this module never redefines that
        enum and only uses ``kind`` to backfill missing metadata.
    """

    if alt_cls is primary_cls:
        raise ValueError(
            f"apply_alias_relationship: alt_cls and primary_cls must differ (got {alt_cls!r} for both)"
        )

    # Alias side. Aliases don't chain — an alias's own ``.aliases`` is empty.
    alt_cls.target = primary_cls
    alt_cls.is_alias = True
    alt_cls.aliases = ()
    if kind is not None:
        _ensure_info_alias_kind(alt_cls, kind)
    alt_cls.__repr__ = make_alias_repr(alt_cls, primary_cls)  # type: ignore[method-assign]

    # Primary side. Canonical points at itself so ``.target`` is always valid.
    if getattr(primary_cls, "target", None) is None:
        primary_cls.target = primary_cls
    if not hasattr(primary_cls, "is_alias"):
        primary_cls.is_alias = False
    existing = tuple(getattr(primary_cls, "aliases", ()) or ())
    if alt_cls not in existing:
        primary_cls.aliases = (*existing, alt_cls)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _node_path(cls: type) -> str:
    """Return a path-like label for ``cls``.

    Prefers ``info.path`` (set by Unit 4); falls back to the class name. Reads
    from the class object directly so the helper can be used at wiring time
    before any instance exists.
    """

    info = getattr(cls, "info", None)
    path = _info_lookup(info, "path", default=None)
    if isinstance(path, str) and path:
        return path
    return cls.__name__


def _info_lookup(info: Any, name: str, *, default: Any) -> object:
    """Read an attribute from an ``info`` namespace, dict, or ``None``."""

    if info is None:
        return default
    if isinstance(info, dict):
        return info.get(name, default)
    return getattr(info, name, default)


def _access_label(access: Any) -> str:
    """Render an access mode into the short repr token (``rw``, ``ro``, ...)."""

    if access is None:
        return "rw"
    # Unit 4's ``AccessMode`` enum members carry a ``.value`` already in the
    # canonical short form ("rw", "ro", "wo", "na"). Fall back to ``str()``
    # so duck-typed test fixtures work without depending on the enum.
    value = getattr(access, "value", None)
    if isinstance(value, str) and value:
        return value
    text = str(access).lower()
    # Strip ``AccessMode.RW``-style prefixes that ``str(enum_member)`` may
    # produce when the enum is not str-valued.
    if "." in text:
        text = text.rsplit(".", maxsplit=1)[-1]
    return text or "rw"


def _ensure_info_alias_kind(alt_cls: type, kind: str) -> None:
    """Best-effort: stamp ``info.alias_kind`` if Unit 4's slot is empty.

    Unit 4 owns the ``Info.alias_kind`` field. We only fill it when the
    exporter (Unit 23) hasn't already, so this helper is a safe no-op
    in production where metadata is fully populated upstream.
    """

    info = getattr(alt_cls, "info", None)
    if info is None:
        return
    existing = _info_lookup(info, "alias_kind", default=None)
    if existing is not None:
        return
    if isinstance(info, dict):
        info["alias_kind"] = kind
    else:
        try:
            info.alias_kind = kind
        except (AttributeError, TypeError):
            # Frozen dataclass or read-only namespace — the exporter is
            # responsible for populating ``alias_kind`` upstream.
            pass


# ---------------------------------------------------------------------------
# Auto-attach hooks — consume Unit 23's detection metadata via Unit 1's
# registry seams. Mirrors the canonical pattern in ``runtime/trace.py``.
# ---------------------------------------------------------------------------


def _coerce_alias_target(value: Any) -> tuple[type | None, str | None]:
    """Decode a ``metadata["alias"]`` / ``["alias_target"]`` payload.

    Sibling units (and a future template injection) may emit the alias
    pointer in a few shapes; we accept all of them so callers don't have
    to adapt:

    * a class object — the target itself.
    * a mapping with ``target``/``target_cls`` (and optional ``kind``).
    * any object that exposes a ``target_cls`` / ``target`` attribute.

    Returns ``(target_class, kind)`` so the caller can call
    :func:`apply_alias_relationship` directly. Either component may be
    ``None`` if the payload doesn't supply it.
    """

    if value is None:
        return None, None
    if isinstance(value, type):
        return value, None
    if isinstance(value, dict):
        target = value.get("target_cls") or value.get("target")
        kind = value.get("kind") or value.get("alias_kind")
        if isinstance(target, type):
            return target, kind if isinstance(kind, str) else None
        return None, kind if isinstance(kind, str) else None
    target = getattr(value, "target_cls", None) or getattr(value, "target", None)
    kind = getattr(value, "kind", None) or getattr(value, "alias_kind", None)
    if isinstance(target, type):
        return target, kind if isinstance(kind, str) else None
    return None, kind if isinstance(kind, str) else None


def _auto_attach_aliases_from_metadata(cls: type, metadata: dict) -> None:
    """Register-enhancement hook: wire alias relationships from metadata.

    Looks up ``metadata["alias"]`` first, then ``metadata["alias_target"]``
    (the two key names sibling tests use interchangeably). When the entry
    decodes to a target class, calls :func:`apply_alias_relationship`.

    Also sets ``cls.is_alias = False`` when the metadata explicitly marks
    the class as canonical, so downstream consumers see a populated flag
    on every register — not just the alt views.
    """

    if not isinstance(metadata, dict):
        return

    payload = metadata.get("alias")
    if payload is None:
        payload = metadata.get("alias_target")

    target, kind = _coerce_alias_target(payload)
    if target is not None and target is not cls:
        apply_alias_relationship(cls, target, kind=kind)
        return

    # No alias payload — but the metadata may still flag this class as
    # the canonical side of an alias relationship (so ``is_alias`` reads
    # as False on every reg, not just the aliased ones). Best-effort.
    if metadata.get("is_alias") is False and not hasattr(cls, "is_alias"):
        cls.is_alias = False


def _resolve_node_at_path(soc: Any, path: str) -> Any | None:
    """Walk dotted ``path`` from ``soc`` and return the live node.

    The exporter emits paths like ``top.uart.control_alt`` — the leading
    segment is the addrmap inst name (the SoC), so we drop it when it
    matches ``soc``'s top name, then descend through attributes. Returns
    ``None`` for any unresolved segment instead of raising; the caller
    treats a miss as "this alias isn't reachable on this tree shape".
    """

    if not path:
        return None
    parts = path.split(".")
    # Drop the leading addrmap segment if it matches the soc's own name —
    # generated trees expose children directly on the soc instance, not
    # under a duplicate ``top`` attribute.
    top_name = getattr(soc, "inst_name", None) or type(soc).__name__
    if parts and parts[0] in {top_name, top_name.lower(), "top"}:
        parts = parts[1:]
    node: Any = soc
    for part in parts:
        node = getattr(node, part, None)
        if node is None:
            return None
    return node


def _apply_alias_mapping(soc: Any, mapping: Iterable[tuple[str, str]]) -> int:
    """Apply every ``(alt_path, primary_path)`` pair to ``soc`` once.

    Resolves each path to a live node, takes its class, and calls
    :func:`apply_alias_relationship`. Returns the number of relationships
    successfully wired so callers (and tests) can confirm the walk
    found something.
    """

    wired = 0
    for alt_path, primary_path in mapping:
        alt_node = _resolve_node_at_path(soc, alt_path)
        primary_node = _resolve_node_at_path(soc, primary_path)
        if alt_node is None or primary_node is None:
            logger.debug(
                "alias auto-attach skipped: %s -> %s not reachable on soc",
                alt_path,
                primary_path,
            )
            continue
        alt_cls = type(alt_node)
        primary_cls = type(primary_node)
        if alt_cls is primary_cls:
            # Same class with two paths — typical when both are bound to
            # the same generated type (e.g. a re-export). Skip; wiring a
            # class against itself raises in :func:`apply_alias_relationship`.
            continue
        apply_alias_relationship(alt_cls, primary_cls)
        wired += 1
    return wired


def _auto_attach_aliases_from_module(soc: Any) -> int:
    """Post-create hook: pull alias metadata from ``<soc_module>.aliases``.

    The exporter plugin (Unit 23) writes a sibling ``aliases.py`` module
    next to the generated SoC with a top-level ``aliases`` dict mapping
    ``{alt_path: primary_path}``. We import it lazily so generators that
    don't emit feature-detection artefacts (older builds, hand-rolled
    fixtures) silently skip this step.

    Returns the number of relationships wired so the post-create chain
    can log it; returns ``0`` for any failure mode (module missing,
    mapping empty, paths unresolved) without raising — sibling units
    that don't emit ``aliases.py`` shouldn't fault the soc construction.

    The return value is informational; the registry's ``register_post_create``
    contract treats every hook as ``-> None``, so callers ignore it.
    """

    soc_module = getattr(type(soc), "__module__", None)
    if not soc_module or soc_module == "__main__":
        return 0

    candidates = (f"{soc_module}.aliases", f"{soc_module.rsplit('.', 1)[0]}.aliases")
    seen: set[str] = set()
    mapping: dict[str, str] = {}
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        candidate_mapping = getattr(mod, "aliases", None)
        if isinstance(candidate_mapping, dict) and candidate_mapping:
            mapping = {str(k): str(v) for k, v in candidate_mapping.items()}
            break

    if not mapping:
        return 0
    return _apply_alias_mapping(soc, mapping.items())


def _post_create_attach_aliases(soc: Any) -> None:
    """Adapter wrapping :func:`_auto_attach_aliases_from_module` for the registry.

    The post-create registry contract is ``Callable[[Any], None]`` (the
    hook chain ignores return values). The auto-attach helper still
    returns the wired count so callers and tests can confirm the walk
    found something — this thin adapter discards that count.
    """

    _auto_attach_aliases_from_module(soc)


# ---------------------------------------------------------------------------
# Registry wiring (sibling-dep: Unit 1's runtime/_registry).
#
# When the registry seam is present we register the auto-attach hooks so
# every ``MySoc.create()`` automatically wires alias relationships from
# Unit 23's emitted metadata. When the registry isn't present (this
# module can be imported in isolation), the import quietly fails and
# callers can still drive ``apply_alias_relationship`` by hand.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - depends on Unit 1 landing order
    from . import _registry  # type: ignore[attr-defined]
except ImportError:
    _registry = None  # type: ignore[assignment]

if _registry is not None and hasattr(_registry, "register_register_enhancement"):
    _registry.register_register_enhancement(_auto_attach_aliases_from_metadata)
if _registry is not None and hasattr(_registry, "register_post_create"):
    _registry.register_post_create(_auto_attach_aliases_from_module)
