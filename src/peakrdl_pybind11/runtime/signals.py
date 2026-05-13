"""RDL ``signal`` nodes as structural metadata on the generated SoC.

Implements §2 of ``docs/IDEAL_API_SKETCH.md``: every ``SignalNode`` in
the compiled RDL becomes an immutable :class:`Signal` dataclass attached
to its parent on the generated SoC tree. Signals carry no bus access —
they describe wires that the C++ binding doesn't touch (resets,
interrupts going *out* of the SoC, debug strobes, etc.).

Implementation seam
-------------------
Signals are Python-only. The exporter collects every ``SignalNode``,
the generated ``runtime.py`` populates a module-level registry keyed by
SoC class id, and a single :func:`register_post_create` hook walks the
registry at ``create()`` time and attaches a :class:`Signal` to each
parent node.

The seam mirrors :mod:`peakrdl_pybind11.runtime.transactions` (which
attaches ``soc.batch()``) and :mod:`peakrdl_pybind11.runtime.widgets`
(which attaches ``soc.tree()`` / ``soc.dump()``): a lone module-level
post-create hook that ``_try_setattr``s its leaf-attribute payload and
swallows pybind11's ``AttributeError`` for slotted classes silently.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import _registry

__all__ = [
    "Signal",
    "attach_to",
]

logger = logging.getLogger("peakrdl_pybind11.runtime.signals")


# ---------------------------------------------------------------------------
# Signal dataclass — frozen so a Signal can be cached, compared, or used
# as a dict key. Equality is by value: two Signals with the same name,
# path, width, and tags compare equal. That property powers the
# idempotency check in :func:`attach_to`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    """RDL ``signal`` metadata, attached to its parent node on the SoC tree.

    Attributes:
        name: The RDL ``inst_name`` of the signal.
        path: Dotted path within the SoC, e.g. ``"uart.tx_signal"``.
            The leading SoC-root segment is stripped by the post-create
            hook before constructing the Signal.
        width: Signal width in bits.
        lsb: Least-significant bit index within a wider bus. Defaults to 0.
        external: ``True`` for signals declared with the RDL ``external``
            qualifier — wires that cross the SoC boundary.
        description: Optional human description from the RDL ``desc``
            property.
        tags: User-defined properties (UDPs) declared on the signal in
            the RDL. The mapping is shaped like ``Info.tags`` — keys are
            UDP property names, values are whatever ``get_property``
            returned for that property.
    """

    name: str
    path: str
    width: int
    lsb: int = 0
    external: bool = False
    description: str | None = None
    tags: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Attach helper — walks a dotted path to the parent node and assigns the
# Signal as the leaf attribute. Walks raise AttributeError naturally if a
# parent segment is missing; only the leaf assignment goes through
# _try_setattr, which is allowed to swallow the rejection from a slotted
# pybind11 class.
# ---------------------------------------------------------------------------


def _try_setattr(obj: Any, name: str, value: Any) -> None:
    """``setattr`` that swallows pybind11's slot rejection.

    pybind11 classes generated without ``py::dynamic_attr()`` raise
    :class:`AttributeError` (or :class:`TypeError`) on ``setattr`` for
    unknown names. Mirrors ``transactions._try_setattr``.
    """

    try:
        setattr(obj, name, value)
    except (AttributeError, TypeError) as exc:
        logger.debug("could not attach signal %r to %r: %s", name, type(obj).__name__, exc)


def attach_to(soc: Any, path: str, signal: Signal) -> None:
    """Attach ``signal`` to the node reached via dotted ``path`` from ``soc``.

    ``path`` is treated as **relative** to ``soc``: the caller has
    already stripped the SoC-root segment.  ``attach_to(soc, "rst", sig)``
    sets ``soc.rst = sig``; ``attach_to(soc, "uart.tx_signal", sig)`` walks
    ``soc.uart`` and sets ``tx_signal`` on it.

    The walk uses plain :func:`getattr`, so a missing parent segment
    raises :class:`AttributeError` to the caller — signals must always
    have a real parent in the generated tree. The final assignment uses
    :func:`_try_setattr`: pybind11 classes without ``py::dynamic_attr()``
    silently reject the assignment, which is the right behaviour for raw
    C++ SoC objects.

    Idempotent on value: if the leaf attribute already holds a
    :class:`Signal` equal to ``signal`` (frozen dataclass ``__eq__`` is
    by value), the call is a no-op. This lets the post-create hook fire
    multiple times (re-imports, hot reload) without churning attributes.
    """

    if not path:
        raise ValueError("signal path must be a non-empty dotted name")

    segments = path.split(".")
    parent = soc
    for segment in segments[:-1]:
        # Plain getattr — missing parent raises AttributeError, which is
        # the contract the unit test ``test_missing_parent_path_raises``
        # pins down.
        parent = getattr(parent, segment)

    leaf = segments[-1]
    existing = getattr(parent, leaf, None)
    if isinstance(existing, Signal) and existing == signal:
        return
    _try_setattr(parent, leaf, signal)


# ---------------------------------------------------------------------------
# Per-SoC-class signal registry. The generated ``runtime.py`` populates
# this at module import; the post-create hook reads it and attaches the
# Signal instances each time a fresh SoC is created.
#
# Keyed by ``id(SocClass_t)`` rather than the class itself so a module
# reload that re-binds the class name doesn't strand the entries (the
# next ``create()`` constructs a different class object even though its
# qualified name is unchanged, and the new entries simply replace the
# old). Values are ``(stripped-path, metadata-dict)`` pairs so the hook
# constructs Signal instances at attach time rather than at import time
# — the dataclass is cheap to build and the deferred construction keeps
# the registry trivial to populate from Jinja.
# ---------------------------------------------------------------------------

_SIGNAL_REGISTRY: dict[int, list[tuple[str, dict[str, Any]]]] = {}


def register_signals(soc_class: type, entries: list[tuple[str, dict[str, Any]]]) -> None:
    """Record ``entries`` against ``soc_class`` for later attachment.

    Called once from each generated ``runtime.py`` at module import.
    Each entry is ``(path_with_root, metadata)`` where ``metadata``
    carries the fields :class:`Signal` needs. The root-stripping happens
    inside :func:`_attach_signals` because only the hook knows the
    actual ``soc.<root>`` name at create-time.
    """

    _SIGNAL_REGISTRY[id(soc_class)] = list(entries)


def _strip_root(path: str) -> str:
    """Drop the leading SoC-root segment from an RDL path.

    ``"simple_soc.uart.tx_signal"`` → ``"uart.tx_signal"``;
    ``"simple_soc.rst"`` → ``"rst"``;
    ``"rst"`` (already stripped) → ``"rst"``.
    """

    if "." not in path:
        return path
    _, _, rest = path.partition(".")
    return rest


@_registry.register_post_create
def _attach_signals(soc: Any) -> None:
    """Post-create hook: attach every registered :class:`Signal` to ``soc``.

    Looks up the per-SoC-class entries by ``id(type(soc))``. For each
    entry, strips the leading root segment from the RDL path, constructs
    a frozen :class:`Signal` from the metadata dict, and calls
    :func:`attach_to`.

    A missing parent segment surfaces as an :class:`AttributeError` from
    :func:`attach_to`; the registry seam (``_registry._fire``) catches
    and logs that for us, so one broken signal entry doesn't poison the
    rest of the post-create chain.
    """

    entries = _SIGNAL_REGISTRY.get(id(type(soc)))
    if not entries:
        return
    for path_with_root, metadata in entries:
        rel_path = _strip_root(path_with_root)
        signal = Signal(
            name=metadata["name"],
            path=rel_path,
            width=int(metadata.get("width", 1)),
            lsb=int(metadata.get("lsb", 0)),
            external=bool(metadata.get("external", False)),
            description=metadata.get("description"),
            tags=dict(metadata.get("tags") or {}),
        )
        attach_to(soc, rel_path, signal)
