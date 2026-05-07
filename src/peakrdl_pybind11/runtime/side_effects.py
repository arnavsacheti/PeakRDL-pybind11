"""Side-effect helpers (sketch §11).

Verbs (``peek``, ``clear``, ``set``, ``pulse``, ``acknowledge``) dispatch on the
``info.on_read`` / ``info.on_write`` metadata attached to a field or register.
The functions accept either kind of target -- the only requirement is that the
target exposes ``.info.on_read`` and ``.info.on_write`` along with ``.read()``
and ``.write()`` callables.

The module also provides ``no_side_effects`` -- a thread-local context manager
that turns destructive reads into ``SideEffectError`` so debug dumps and
read-only inspectors can run without mutating hardware.

Sibling units (``errors`` -> Unit 2, ``info`` -> Unit 4, ``_registry`` ->
Unit 1) may not be present yet on the branch this module is built on; the
imports below fall back to local definitions so this file is usable in
isolation. When the siblings land, the imports prefer them transparently.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

# ---------------------------------------------------------------------------
# Sibling-dep imports with local fallbacks (Units 1, 2 may not exist yet).
# ---------------------------------------------------------------------------
try:  # pragma: no cover - prefer the real Unit 2 errors when present
    from ..errors import NotSupportedError, SideEffectError  # type: ignore[no-redef]
except ImportError:  # pragma: no cover - fallback shim

    class NotSupportedError(RuntimeError):
        """Raised when an operation is not supported by the master/field."""

    class SideEffectError(RuntimeError):
        """Raised when a destructive read happens inside ``no_side_effects``."""


try:  # pragma: no cover - prefer the real Unit 1 registry when present
    from .._registry import register_register_enhancement  # type: ignore[no-redef]
except ImportError:  # pragma: no cover - fallback shim (no-op decorator)

    def register_register_enhancement(fn: Callable[..., object]) -> Callable[..., object]:
        """No-op fallback for the enhancement registry seam."""
        return fn


__all__ = [
    "NotSupportedError",
    "SideEffectError",
    "acknowledge",
    "check_read_allowed",
    "clear",
    "no_side_effects",
    "peek",
    "pulse",
    "set_",
]

# Read-side effects that mutate hardware on a plain ``read()``. Centralized so
# ``peek`` / ``check_read_allowed`` agree on which effects need a peek path.
_DESTRUCTIVE_READ_EFFECTS = frozenset({"rclr", "rset", "ruser"})

# ---------------------------------------------------------------------------
# Thread-local guard used by ``no_side_effects`` and by enhanced read paths.
# ---------------------------------------------------------------------------
_state = threading.local()


def _guard_active() -> bool:
    """Return True if the calling thread is inside ``no_side_effects``."""
    return getattr(_state, "no_side_effects", False)


# ---------------------------------------------------------------------------
# Helpers for inspecting the duck-typed metadata.
# ---------------------------------------------------------------------------
def _norm(value: object) -> str:
    """Normalize an ``on_read`` / ``on_write`` value to a lowercase string.

    Unit 4 may eventually use enums; today's tests pass strings. ``None`` /
    ``"none"`` / falsy all collapse to the empty string so callers can compare
    cleanly.
    """
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if name is not None:
        value = name
    text = str(value).strip().lower()
    return "" if text in ("none", "") else text


def _info_path(target: object) -> str:
    """Best-effort path string for use in error messages."""
    info = getattr(target, "info", None)
    path = getattr(info, "path", None)
    if path:
        return str(path)
    name = getattr(target, "name", None) or getattr(info, "name", None)
    return str(name) if name else "<unknown>"


def _on_read(target: object) -> str:
    info = getattr(target, "info", None)
    return _norm(getattr(info, "on_read", None))


def _on_write(target: object) -> str:
    info = getattr(target, "info", None)
    return _norm(getattr(info, "on_write", None))


def _all_ones(target: object) -> int:
    """Return a bitmask of all-ones for a field/register (default ``1``).

    Falls back to ``1`` when no width metadata is available -- bare ``write(1)``
    is the documented behaviour for ``woclr`` / ``woset`` / ``singlepulse``.
    """
    info = getattr(target, "info", None)
    width = getattr(info, "width", None)
    if width is None:
        # Try regwidth (registers) as a secondary source.
        width = getattr(info, "regwidth", None)
    if isinstance(width, int) and width > 0:
        return (1 << width) - 1
    return 1


def _master_can_peek(target: object) -> bool:
    """Return True if the master serving ``target`` exposes a peek path.

    Resolution order:
      1. ``master.can_peek`` -- explicit capability flag wins. Lets a master
         that *defines* a ``peek`` method advertise that it actually only
         delegates to a destructive read on the current bus.
      2. Otherwise, the presence of a callable ``master.peek`` is taken as
         capability. Unit 1 may formalize this further later.
    """
    master = _resolve_master(target)
    if master is None:
        # No master attached -- best to refuse and let the user wire one up.
        return False
    can_peek = getattr(master, "can_peek", None)
    if can_peek is not None:
        return bool(can_peek)
    return callable(getattr(master, "peek", None))


def _resolve_master(target: object) -> object | None:
    """Locate the master associated with a field/register (or ``None``)."""
    for attr in ("master", "_master"):
        master = getattr(target, attr, None)
        if master is not None:
            return master
    parent = getattr(target, "parent", None) or getattr(target, "_parent", None)
    if parent is not None:
        return _resolve_master(parent)
    return None


# ---------------------------------------------------------------------------
# Hook for the enhanced ``.read()`` to consult the no-side-effects guard.
# ---------------------------------------------------------------------------
def check_read_allowed(target: object) -> None:
    """Raise ``SideEffectError`` if a destructive read is forbidden right now.

    Generated read paths (and the enhancement seam) are expected to call this
    before issuing the bus read whenever ``info.on_read`` is set. It is a
    no-op outside of a ``no_side_effects`` block.
    """
    if not _guard_active():
        return
    effect = _on_read(target)
    if effect in _DESTRUCTIVE_READ_EFFECTS:
        raise SideEffectError(
            f"read() of {_info_path(target)} would {effect} -- "
            "use peek() or remove the no_side_effects() guard"
        )


# ---------------------------------------------------------------------------
# Public verbs.
# ---------------------------------------------------------------------------
def peek(target: object) -> int | bool:
    """Read ``target`` without triggering its read-side effect.

    For an ``rclr`` field the underlying master must support a non-destructive
    read; otherwise ``NotSupportedError`` is raised -- the API does not pretend
    a peek exists when the bus literally cannot perform one.
    """
    on_read = _on_read(target)
    can_peek = _master_can_peek(target)
    if on_read in _DESTRUCTIVE_READ_EFFECTS and not can_peek:
        raise NotSupportedError(f"master cannot peek {on_read} ({_info_path(target)})")

    master = _resolve_master(target)
    if can_peek and master is not None and callable(getattr(master, "peek", None)):
        info = getattr(target, "info", None)
        addr = getattr(info, "address", None)
        if addr is not None:
            width = getattr(info, "regwidth", None) or getattr(info, "width", None) or 4
            return _slice_field(target, master.peek(addr, width))

    # No bus-level peek seam -- fall back to the target's own read(), which is
    # safe iff the field has no read-side effect (the early return above
    # already covers the destructive case).
    if not callable(getattr(target, "read", None)):
        raise NotSupportedError(f"peek not supported on {_info_path(target)}")
    return target.read()


def _slice_field(target: object, register_value: int) -> int | bool:
    """If ``target`` is a field, slice ``register_value`` to just its bits."""
    info = getattr(target, "info", None)
    lsb = getattr(info, "lsb", None)
    width = getattr(info, "width", None)
    if isinstance(lsb, int) and isinstance(width, int):
        masked = (register_value >> lsb) & ((1 << width) - 1)
        return bool(masked) if width == 1 else masked
    return register_value


def clear(target: object) -> None:
    """Clear ``target`` using whichever path the metadata declares.

    - ``onwrite = woclr``  -> write 1
    - ``onwrite = wclr``   -> write all-ones (any write clears, but all-ones
      is a deterministic choice that works for both fields and registers)
    - ``onwrite = wzc``    -> write 0
    - ``onread  = rclr``   -> issue a read (and discard)
    - otherwise            -> raise ``NotSupportedError``
    """
    on_write = _on_write(target)
    if on_write == "woclr":
        target.write(1)
        return
    if on_write == "wclr":
        target.write(_all_ones(target))
        return
    if on_write == "wzc":
        target.write(0)
        return
    if _on_read(target) == "rclr":
        # A destructive read IS the clear path here.
        target.read()
        return
    raise NotSupportedError(f"{_info_path(target)}: no clear path on this field")


def acknowledge(target: object) -> None:
    """Alias of :func:`clear` -- reads better in ISR-shaped code."""
    clear(target)


def set_(target: object) -> None:
    """Set ``target`` using whichever path the metadata declares.

    - ``onwrite = woset`` -> write 1
    - ``onwrite = wset``  -> write all-ones
    - ``onwrite = wzs``   -> write 0
    - otherwise           -> raise ``NotSupportedError``
    """
    on_write = _on_write(target)
    if on_write == "woset":
        target.write(1)
        return
    if on_write == "wset":
        target.write(_all_ones(target))
        return
    if on_write == "wzs":
        target.write(0)
        return
    raise NotSupportedError(f"{_info_path(target)}: no set path on this field")


def pulse(target: object) -> None:
    """Trigger a singlepulse field (write 1; hardware self-clears).

    Also accepts any field with a ``woset`` write effect for symmetry --
    sometimes the RDL spells the same hardware behaviour either way.
    """
    info = getattr(target, "info", None)
    is_singlepulse = (
        bool(getattr(info, "singlepulse", False)) or _norm(getattr(info, "kind", None)) == "singlepulse"
    )
    if is_singlepulse or _on_write(target) == "woset":
        target.write(1)
        return
    raise NotSupportedError(f"{_info_path(target)}: not a singlepulse field")


# ---------------------------------------------------------------------------
# Context manager.
# ---------------------------------------------------------------------------
@contextmanager
def no_side_effects(soc: object = None) -> Iterator[object]:
    """Forbid destructive reads (``rclr`` / ``rset``) for the calling thread.

    Inside the block, any ``check_read_allowed`` call on a field whose
    ``info.on_read`` is ``rclr`` / ``rset`` raises :class:`SideEffectError`.
    Generated read paths (and tests using the enhancement seam) are expected
    to call ``check_read_allowed`` before issuing their bus read.

    The ``soc`` argument is preserved for forward compatibility (some
    backends may want to widen the guard to also disable observation hooks);
    today it is unused.
    """
    prior = getattr(_state, "no_side_effects", False)
    _state.no_side_effects = True
    try:
        yield soc
    finally:
        _state.no_side_effects = prior


# ---------------------------------------------------------------------------
# Enhancement seam -- attach the verbs as methods to generated reg/field
# classes so users can write ``f.clear()`` / ``r.peek()`` directly.
# ---------------------------------------------------------------------------
@register_register_enhancement
def _enhance(cls: type, metadata: object = None) -> None:
    """Register/field enhancement that binds the verbs as methods.

    The signature mirrors Unit 1's seam: ``cls`` is the generated class,
    ``metadata`` the per-class info bundle (unused here -- the verbs read
    metadata off ``self.info`` at call time).
    """
    cls.peek = lambda self: peek(self)  # type: ignore[attr-defined]
    cls.clear = lambda self: clear(self)  # type: ignore[attr-defined]
    cls.set = lambda self: set_(self)  # type: ignore[attr-defined]
    cls.pulse = lambda self: pulse(self)  # type: ignore[attr-defined]
    cls.acknowledge = lambda self: acknowledge(self)  # type: ignore[attr-defined]
