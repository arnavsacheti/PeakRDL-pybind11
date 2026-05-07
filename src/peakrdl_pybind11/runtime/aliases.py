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
(Unit 23). The runtime only consumes the metadata flag ``is_alias=True``
plus a pointer to the target primary class, and wires both sides.

The seam used to wire the relationship is :func:`register_register_enhancement`,
which mirrors Unit 1's eventual ``runtime/_registry.py``. Until Unit 1 lands,
a minimal local stub lives here so this module can be imported and tested in
isolation.
"""

from __future__ import annotations

from collections.abc import Callable

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

    def __repr__(self: object) -> str:
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
    alt_cls.__repr__ = make_alias_repr(alt_cls, primary_cls)

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


def _info_lookup(info: object, name: str, *, default: object) -> object:
    """Read an attribute from an ``info`` namespace, dict, or ``None``."""

    if info is None:
        return default
    if isinstance(info, dict):
        return info.get(name, default)
    return getattr(info, name, default)


def _access_label(access: object) -> str:
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
