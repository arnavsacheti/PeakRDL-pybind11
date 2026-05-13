"""Pure-data side-effect transformations for the behavioural simulator.

This module owns the *what-happens-to-bits* logic for the five families
of RDL field side effects that :class:`~peakrdl_pybind11.masters.sim.SimMaster`
currently honours:

==================  =========================================================
Property            Behaviour
==================  =========================================================
``onread = rclr``   Read returns the pre-read value; storage is cleared
                    afterwards (the field's bits zero out).
``onread = rset``   Read returns the pre-read value; storage is set to all-1s
                    in the field afterwards.
``onwrite = woclr`` Per-bit write-1-to-clear. The task and ``side_effects``
                    helpers also fold ``wclr`` and ``wzc`` here:
                    ``wclr`` -> per-bit (matches the project's convention,
                    not the strict SystemRDL spec which defines ``wclr`` as
                    "any write clears all bits"; documented here to avoid
                    surprise);
                    ``wzc`` -> per-bit *inverse* (write-0-to-clear).
``onwrite = woset`` Per-bit write-1-to-set. ``wset`` is treated the same
                    way (see the ``wclr`` note above);
                    ``wzs`` is the per-bit inverse (write-0-to-set).
``singlepulse``     Bit pulses high on write and self-clears immediately
                    afterwards. Implemented as a post-pass that zeros the
                    field's bits in storage after the regular write rule has
                    been applied.
==================  =========================================================

Out of scope for this round (treated as pass-through, never crash):
``sticky`` / ``stickybit``, ``hwclr`` / ``hwset``, ``ruser`` / ``wuser``,
``onread = ruser`` -- the simulator leaves storage alone and emits the
normal read/write semantics. A follow-up unit will wire these up.

The transformations are bit-arithmetic only and have no I/O; the bus
master (:class:`~peakrdl_pybind11.masters.sim.SimMaster`) calls them
before/after touching the underlying dict.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

__all__ = [
    "FieldSideEffect",
    "RegisterSideEffectModel",
    "build_models_for_soc",
    "field_side_effect_from_info",
]


def _normalize_token(value: Any) -> str | None:
    """Normalise an ``on_read``/``on_write`` token to a lowercase string.

    Accepts strings, ``AccessMode`` enum members (via ``.value``), and
    ``None``. Falsy / ``"none"`` becomes ``None`` so callers can compare
    with ``is None``.
    """
    if value is None:
        return None
    raw = getattr(value, "value", None)
    if isinstance(raw, str):
        token = raw
    elif isinstance(value, str):
        token = value
    else:
        # Best-effort: stringify enums/objects that aren't either of
        # the above.
        name = getattr(value, "name", None)
        token = name if isinstance(name, str) else str(value)
    token = token.strip().lower()
    if token in ("", "none"):
        return None
    return token


@dataclass(frozen=True, slots=True)
class FieldSideEffect:
    """All bits/behaviour metadata for one field's side-effect rules.

    Attributes mirror what the simulator actually needs (lsb, width,
    on_read, on_write, singlepulse). Anything richer -- the Python-side
    enhancement seam, the snapshot view -- continues to read from
    :class:`~peakrdl_pybind11.runtime.info.Info`.
    """

    lsb: int
    width: int
    on_read: str | None  # "rclr" / "rset" / "ruser" / None
    on_write: str | None  # "woclr" / "woset" / "wzc" / "wzs" / "wclr" / "wset" / "wuser" / None
    singlepulse: bool

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def mask(self) -> int:
        """Bitmask of this field within the register-width value."""
        if self.width <= 0:
            return 0
        return ((1 << self.width) - 1) << self.lsb

    # ------------------------------------------------------------------
    # Read transformation
    # ------------------------------------------------------------------
    def apply_read(self, register_value: int) -> int:
        """Return new ``register_value`` after this field's read side effect.

        Note: the *returned* value to the bus caller is always the
        pre-read storage. This function only updates the storage that
        will be visible on the *next* read. The caller is responsible
        for combining multiple fields' transformations into the final
        new-storage value.
        """
        token = self.on_read
        if token == "rclr":
            return register_value & ~self.mask()
        if token == "rset":
            return register_value | self.mask()
        # rclr/rset/ruser are the spec-defined read-side effects; the
        # first two mutate storage and are handled above. ``ruser`` is
        # out of scope -- treat as pass-through.
        return register_value

    # ------------------------------------------------------------------
    # Write transformation
    # ------------------------------------------------------------------
    def apply_write(self, register_value: int, written: int) -> int:
        """Return new ``register_value`` after this field's write rule.

        ``written`` is the **full register-width value** the caller asked
        to write -- the field-shifted slice is extracted internally.
        Bits *outside* this field's range are left untouched.

        The transformations:

        * No ``on_write`` token -> standard write: this field's bits are
          replaced with the corresponding bits of ``written``.
        * ``woclr``/``wclr`` -> per-bit write-1-to-clear.
        * ``wzc``           -> per-bit write-0-to-clear.
        * ``woset``/``wset`` -> per-bit write-1-to-set.
        * ``wzs``           -> per-bit write-0-to-set.
        * ``wuser``         -> pass-through (out of scope for now).

        Singlepulse clears are *not* applied here; the
        :class:`RegisterSideEffectModel` runs them as a post-pass after
        every field's write rule has had a turn, so storage is updated
        deterministically regardless of field ordering.
        """
        field_mask = self.mask()
        if field_mask == 0:
            return register_value
        # Slice the field-relative bits out of the full written value.
        slice_written = (written >> self.lsb) & ((1 << self.width) - 1)
        current_field_bits = (register_value >> self.lsb) & ((1 << self.width) - 1)
        token = self.on_write
        new_field_bits = _apply_write_token(token, current_field_bits, slice_written, self.width)
        # Splice the transformed bits back into the register-width result.
        return (register_value & ~field_mask) | ((new_field_bits & ((1 << self.width) - 1)) << self.lsb)


def _apply_write_token(token: str | None, current: int, written: int, width: int) -> int:
    """Field-relative transformation for one ``on_write`` token.

    All arguments are already field-shifted (LSB = 0). ``width`` is the
    field width in bits.
    """
    if token is None:
        # Plain write -- replace stored bits with the written slice.
        return written
    if token in ("woclr", "wclr"):
        # write-1-to-clear (per bit). Note: strict-spec ``wclr`` means
        # "any write clears all bits"; this project's convention (per
        # the task brief and the test suite) treats it as per-bit.
        return current & ~written
    if token == "wzc":
        # write-0-to-clear: bits where ``written`` is 0 get cleared,
        # bits where ``written`` is 1 stay as they were.
        mask = (1 << width) - 1
        zero_bits = (~written) & mask
        return current & ~zero_bits
    if token in ("woset", "wset"):
        # write-1-to-set (per bit).
        return current | written
    if token == "wzs":
        # write-0-to-set: bits where ``written`` is 0 get set,
        # bits where ``written`` is 1 stay as they were.
        mask = (1 << width) - 1
        zero_bits = (~written) & mask
        return current | zero_bits
    # ``wuser`` or anything else we don't model: treat as plain write
    # so the caller can still update the field. Recorded for posterity
    # in the module docstring under "out of scope".
    return written


class RegisterSideEffectModel:
    """All :class:`FieldSideEffect` entries for one register.

    A register with **no** side-effecting fields and no singlepulse
    bits has an empty model (``has_side_effects`` is ``False``); the
    simulator skips the model entirely on that address for the
    fast-path.
    """

    __slots__ = ("_fields", "_has_read_effects", "_has_write_effects", "_singlepulse_mask")

    def __init__(self, fields: Iterable[FieldSideEffect]) -> None:
        self._fields: tuple[FieldSideEffect, ...] = tuple(fields)
        # Pre-compute the singlepulse mask so apply_write's post-pass is
        # a single AND-NOT operation.
        sp = 0
        has_read = False
        has_write = False
        for f in self._fields:
            if f.singlepulse:
                sp |= f.mask()
            if f.on_read is not None:
                has_read = True
            if f.on_write is not None or f.singlepulse:
                has_write = True
        self._singlepulse_mask = sp
        self._has_read_effects = has_read
        # Write-side effects also include singlepulse (post-pass clear).
        self._has_write_effects = has_write or sp != 0

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------
    @property
    def fields(self) -> tuple[FieldSideEffect, ...]:
        return self._fields

    @property
    def has_read_effects(self) -> bool:
        return self._has_read_effects

    @property
    def has_write_effects(self) -> bool:
        return self._has_write_effects

    def __bool__(self) -> bool:
        # Truthy iff at least one field actually transforms storage.
        return self._has_read_effects or self._has_write_effects

    # ------------------------------------------------------------------
    # Read / write entry points
    # ------------------------------------------------------------------
    def apply_read(self, storage_value: int) -> tuple[int, int]:
        """Return ``(returned_value, new_storage_value)`` for a read.

        ``returned_value`` is the pre-side-effect storage -- what the
        bus master gives back to the caller. ``new_storage_value`` is
        the storage that the *next* read should see, after any
        ``rclr`` / ``rset`` updates.
        """
        if not self._has_read_effects:
            return storage_value, storage_value
        new_storage = storage_value
        for f in self._fields:
            if f.on_read is None:
                continue
            new_storage = f.apply_read(new_storage)
        return storage_value, new_storage

    def apply_write(self, storage_value: int, written: int) -> int:
        """Return new storage after every field's write rule + singlepulse pass.

        Algorithm:

        1. Start from ``written`` so unmodelled bits get plain-write
           semantics (a register with one ``woclr`` field still updates
           the rest of the register-width word normally).
        2. For each modelled field, carve out that field's mask and
           replace those bits with the field's transformation applied
           to the *prior* ``storage_value`` (so e.g. ``woclr`` consults
           the existing storage, not the just-overwritten word).
        3. Post-pass: clear singlepulse fields in storage so the next
           read sees them low.
        """
        if not self._has_write_effects:
            return written
        new_storage = _compose_write(self._fields, storage_value, written)
        if self._singlepulse_mask:
            new_storage &= ~self._singlepulse_mask
        return new_storage


def _compose_write(
    fields: tuple[FieldSideEffect, ...],
    storage: int,
    written: int,
) -> int:
    """Build the new register-width value by per-field substitution.

    Bits **outside** every modelled field's mask default to the plain
    written value (so a register with one ``woclr`` field still updates
    the rest of the word like a normal write, and a register with no
    fields at all -- which we short-circuit higher up -- would also just
    pass through).

    Bits **inside** a modelled field get that field's transformation
    applied to the prior ``storage``.
    """
    new = written
    for f in fields:
        m = f.mask()
        if m == 0:
            continue
        # Transformation acts on the storage's view of the field; mask
        # the result back into place so other fields' regions are
        # untouched.
        transformed = f.apply_write(storage, written)
        new = (new & ~m) | (transformed & m)
    return new


# ---------------------------------------------------------------------------
# Building models from an SoC tree
# ---------------------------------------------------------------------------


def field_side_effect_from_info(field: Any) -> FieldSideEffect | None:
    """Build a :class:`FieldSideEffect` from a field instance, if any.

    Returns ``None`` when the field has no modelled side effect (no
    ``on_read``, no ``on_write``, no ``singlepulse``) so the caller can
    skip it.

    The field is duck-typed -- we look for ``.info`` and read attributes
    off it. ``info.lsb`` is preferred, falling back to ``info.offset``.
    ``info.width`` is preferred, falling back to ``info.regwidth``.
    """
    info = getattr(field, "info", None)
    if info is None:
        return None
    on_read = _normalize_token(getattr(info, "on_read", None))
    on_write = _normalize_token(getattr(info, "on_write", None))
    singlepulse = bool(getattr(info, "singlepulse", False))
    # Treat ``ruser`` / ``wuser`` as pass-through for now: they are still
    # tracked so this round-trips through a model but they don't trigger
    # storage mutation in ``_apply_write_token`` / ``apply_read``.
    if on_read is None and on_write is None and not singlepulse:
        return None
    lsb = getattr(info, "lsb", None)
    if lsb is None:
        lsb = getattr(info, "offset", 0)
    width = getattr(info, "width", None)
    if width is None:
        width = getattr(info, "regwidth", 1)
    try:
        lsb_int = int(lsb)
        width_int = int(width)
    except (TypeError, ValueError):
        return None
    if width_int <= 0:
        return None
    return FieldSideEffect(
        lsb=lsb_int,
        width=width_int,
        on_read=on_read,
        on_write=on_write,
        singlepulse=singlepulse,
    )


def _iter_registers(soc: Any) -> Iterable[Any]:
    """Yield register-like nodes from ``soc``.

    Strategy (most-preferred first):

    1. ``soc.walk(kind="reg")`` -- the canonical surface from
       ``runtime/routing.py``. Try this first.
    2. ``soc.walk()`` -- broad walker; we then filter to register-shaped
       nodes ourselves.
    3. Duck-typed ``vars(soc)`` traversal -- last-resort fallback that
       lets tests use a hand-rolled SoC without implementing ``walk()``.
    """
    walk = getattr(soc, "walk", None)
    if callable(walk):
        try:
            yielded: Iterable[Any] | None = cast("Iterable[Any]", walk(kind="reg"))
        except TypeError:
            try:
                yielded = cast("Iterable[Any]", walk())
            except TypeError:
                yielded = None
        if yielded is not None:
            for node in yielded:
                if _looks_like_register(node):
                    yield node
            return
    # Fallback: pre-order DFS over instance dicts.
    visited: set[int] = set()
    stack: list[Any] = [soc]
    while stack:
        cur = stack.pop()
        key = id(cur)
        if key in visited:
            continue
        visited.add(key)
        if _looks_like_register(cur):
            yield cur
        try:
            children = vars(cur)
        except TypeError:
            continue
        for name, val in children.items():
            if name.startswith("_"):
                continue
            if name in ("parent", "master", "info"):
                continue
            if val is cur:
                continue
            if val is None or isinstance(val, (str, bytes, int, float, bool)):
                continue
            if isinstance(val, (list, tuple, dict, set, frozenset)):
                continue
            stack.append(val)


def _looks_like_register(node: Any) -> bool:
    """Duck-typed check: does ``node`` look like a register?

    A register has ``read`` and ``write`` callables and exposes an
    ``.info`` with an address. Mirrors the heuristics in
    ``runtime/routing._kind_for``.
    """
    if node is None:
        return False
    info = getattr(node, "info", None)
    if info is None:
        return False
    addr = getattr(info, "address", None)
    if not isinstance(addr, int):
        return False
    if not callable(getattr(node, "read", None)):
        return False
    if not callable(getattr(node, "write", None)):
        return False
    return True


def _iter_field_instances(register: Any) -> Iterable[Any]:
    """Yield field-instance children of ``register``.

    Field-level metadata (``on_read`` / ``on_write`` / ``singlepulse``)
    lives on field instance ``info`` attributes -- *not* in
    ``register.info.fields`` (which carries only ``name``/``path``/
    ``offset``/``regwidth`` per the runtime ``_info_factory`` shim).
    """
    try:
        members = vars(register)
    except TypeError:
        return
    for name, val in members.items():
        if name.startswith("_"):
            continue
        if name in ("parent", "master", "info"):
            continue
        if val is register:
            continue
        info = getattr(val, "info", None)
        if info is None:
            continue
        # A field has on_read/on_write/singlepulse OR an lsb/offset+width pair.
        # We accept anything with an info; field_side_effect_from_info
        # will return None for pass-through fields.
        yield val


def build_models_for_soc(soc: Any) -> dict[int, RegisterSideEffectModel]:
    """Walk ``soc`` and build a per-address side-effect model map.

    Registers whose fields are *all* pass-through (no ``on_read`` /
    ``on_write`` / ``singlepulse``) are omitted from the result so the
    simulator's hot path can skip the dict lookup entirely for them.
    """
    models: dict[int, RegisterSideEffectModel] = {}
    for reg in _iter_registers(soc):
        info = getattr(reg, "info", None)
        address = getattr(info, "address", None)
        if not isinstance(address, int):
            continue
        field_effects: list[FieldSideEffect] = []
        for field_obj in _iter_field_instances(reg):
            eff = field_side_effect_from_info(field_obj)
            if eff is not None:
                field_effects.append(eff)
        if not field_effects:
            continue
        model = RegisterSideEffectModel(field_effects)
        if bool(model):
            models[address] = model
    return models
