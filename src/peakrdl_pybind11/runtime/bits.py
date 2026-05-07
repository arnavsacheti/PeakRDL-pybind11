"""Bit-level access proxies for multi-bit fields (sketch §3.2).

Adds a ``bits`` namespace on every multi-bit field instance::

    field.bits[5].read()         # bool, single-bit
    field.bits[5].write(1)       # 1 read + 1 write RMW on parent register
    field.bits[0:8].read()       # ndarray[bool] of length 8
    field.bits[:].write(0xFF00)  # bitmask broadcast, single RMW

Costs in bus transactions:

* ``bits[i].read()`` and ``bits[a:b].read()`` reuse the field's existing
  ``read_raw`` (or ``read``) which already costs 1 register read.
* ``bits[i].write(v)`` and ``bits[a:b].write(v)`` cost 1 read + 1 write on
  the *register* (not the field). Going through the parent register lets a
  single-bit flip cost the same as an explicit RMW; routing through
  ``field.write`` would double-read the register.

NumPy is a hard runtime dependency (sketch §22.2). Slice reads always
return ``numpy.ndarray(dtype=bool)``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol, runtime_checkable

import numpy as np

from ._registry import register_field_enhancement

# ---------------------------------------------------------------------------
# Duck-typed protocols. Generated bindings expose these names; the proxies
# only require this surface so they remain testable with hand-rolled mocks.
# ---------------------------------------------------------------------------


@runtime_checkable
class _RegisterLike(Protocol):
    """Minimal protocol for the parent register of a multi-bit field."""

    def read_raw(self) -> int: ...
    def write_raw(self, value: int) -> None: ...


@runtime_checkable
class _FieldLike(Protocol):
    """Minimal protocol for a multi-bit field instance."""

    lsb: int

    def read_raw(self) -> int: ...


# ``int | bool | ndarray | sequence`` covers everything bits[].write accepts.
BitsWriteValue = int | bool | np.ndarray | Sequence[int | bool]


class BitProxy:
    """Single-bit handle returned by ``field.bits[i]``.

    ``i`` is a *field-local* index (``0`` is the field's least significant
    bit). The proxy keeps a reference to the parent field so writes can use
    the field's enclosing register for a 1-read + 1-write RMW.
    """

    __slots__ = ("_field", "_index")

    def __init__(self, field: _FieldLike, index: int) -> None:
        self._field = field
        self._index = index

    @property
    def index(self) -> int:
        """The field-local bit index."""
        return self._index

    def read(self) -> bool:
        """Return the current value of this bit as ``bool``.

        Costs one register read (the field's existing read).
        """
        value = _read_field_value(self._field)
        return bool((value >> self._index) & 1)

    def write(self, value: bool | int) -> None:
        """RMW a single bit on the parent register.

        ``value`` is normalised to 0/1 — any truthy value sets the bit, any
        falsy value clears it. Costs one register read + one register write.
        """
        bit = 1 if value else 0
        _rmw_field_slice(
            self._field, start=self._index, length=1, bits=bit
        )

    def __bool__(self) -> bool:
        return self.read()

    def __int__(self) -> int:
        return int(self.read())

    def __repr__(self) -> str:
        return f"BitProxy(field={_field_name(self._field)!r}, index={self._index})"


class BitsRangeProxy:
    """Slice handle returned by ``field.bits[a:b]`` (or ``field.bits[:]``).

    ``read()`` returns an ``ndarray(dtype=bool)`` of length ``stop - start``.
    ``write(value)`` accepts either an integer bitmask (LSB → field-local
    index 0) or any ndarray/sequence of bool/int values whose length matches
    the slice.
    """

    __slots__ = ("_field", "_start", "_stop")

    def __init__(self, field: _FieldLike, start: int, stop: int) -> None:
        if stop < start:
            raise ValueError(
                f"bits[{start}:{stop}] has negative length on field "
                f"{_field_name(field)!r}"
            )
        self._field = field
        self._start = start
        self._stop = stop

    @property
    def start(self) -> int:
        return self._start

    @property
    def stop(self) -> int:
        return self._stop

    def __len__(self) -> int:
        return self._stop - self._start

    def read(self) -> np.ndarray:
        """Return ``ndarray[bool]`` of length ``stop - start``."""
        length = self._stop - self._start
        if length == 0:
            return np.empty(0, dtype=bool)
        value = _read_field_value(self._field)
        slice_value = (value >> self._start) & ((1 << length) - 1)
        # Round up to bytes, unpack, then trim to the exact slice length.
        byte_count = (length + 7) // 8
        as_bytes = slice_value.to_bytes(byte_count, byteorder="little")
        return np.unpackbits(
            np.frombuffer(as_bytes, dtype=np.uint8), bitorder="little"
        )[:length].astype(bool)

    def write(self, value: BitsWriteValue) -> None:
        """RMW the covered slice on the parent register.

        ``value`` is one of:

        * ``int`` — interpreted as a bitmask whose LSB lands at the slice
          start (i.e. ``value & 1`` writes to ``bits[start]``,
          ``(value >> 1) & 1`` writes to ``bits[start + 1]``, …).
        * an array-like of length ``stop - start`` of bool/int values.

        Costs one register read + one register write regardless of slice
        length.
        """
        length = self._stop - self._start
        if length == 0:
            return
        _rmw_field_slice(
            self._field,
            start=self._start,
            length=length,
            bits=_bits_to_int(value, length),
        )

    def __iter__(self) -> Iterator[bool]:
        for value in self.read():
            yield bool(value)

    def __repr__(self) -> str:
        return (
            f"BitsRangeProxy(field={_field_name(self._field)!r}, "
            f"start={self._start}, stop={self._stop})"
        )


class BitsAccessor:
    """``field.bits`` namespace.

    Indexing with an ``int`` returns a :class:`BitProxy`; slicing returns a
    :class:`BitsRangeProxy`. Both are cheap handles — they don't read the
    bus at construction time.
    """

    __slots__ = ("_field",)

    def __init__(self, field: _FieldLike) -> None:
        self._field = field

    def __getitem__(self, key: int | slice) -> BitProxy | BitsRangeProxy:
        width = _field_width(self._field)
        if isinstance(key, slice):
            start, stop, step = key.indices(width)
            if step != 1:
                raise ValueError(
                    f"bits[{key.start}:{key.stop}:{key.step}] step must be 1"
                )
            return BitsRangeProxy(self._field, start, stop)
        if isinstance(key, bool) or not isinstance(key, int):
            raise TypeError(
                f"bits index must be int or slice, got {type(key).__name__}"
            )
        index = key if key >= 0 else width + key
        if not 0 <= index < width:
            raise IndexError(
                f"bits[{key}] out of range for field width {width}"
            )
        return BitProxy(self._field, index)

    def __setitem__(self, key: int | slice, value: BitsWriteValue) -> None:
        # Both proxy.write() implementations already accept truthy/falsy
        # values for the single-bit case, so no extra coercion is needed.
        self[key].write(value)  # type: ignore[arg-type]

    def __len__(self) -> int:
        return _field_width(self._field)

    def __iter__(self) -> Iterator[BitProxy]:
        width = _field_width(self._field)
        for i in range(width):
            yield BitProxy(self._field, i)

    def __repr__(self) -> str:
        return (
            f"BitsAccessor(field={_field_name(self._field)!r}, "
            f"width={_field_width(self._field)})"
        )


# ---------------------------------------------------------------------------
# Wiring — attach a ``bits`` property to multi-bit field classes.
# ---------------------------------------------------------------------------

_BITS_ATTR_FLAG = "__peakrdl_bits_installed__"


def attach_bits_accessor(field_cls: type) -> None:
    """Attach a ``bits`` property to ``field_cls`` if its width > 1.

    Idempotent: classes already enhanced are skipped. Width is taken from
    ``field_cls.info.width`` if present (Unit 4 contract), falling back to a
    bare ``width`` attribute on the class so the helper is usable before
    Unit 4 lands.
    """
    if getattr(field_cls, _BITS_ATTR_FLAG, False):
        return
    width = _lookup_width(field_cls)
    if width is None or width <= 1:
        # Single-bit fields don't get a ``bits`` namespace — ``field.read()``
        # already returns a bool there.
        return

    def _bits_property(self: _FieldLike) -> BitsAccessor:
        return BitsAccessor(self)

    field_cls.bits = property(_bits_property)
    setattr(field_cls, _BITS_ATTR_FLAG, True)


# Register the per-field enhancement so it runs automatically once Unit 1's
# ``apply_enhancements`` walks the generated field classes.
register_field_enhancement(attach_bits_accessor)


# ---------------------------------------------------------------------------
# Internal helpers — kept module-private so the proxies stay tiny.
# ---------------------------------------------------------------------------


def _rmw_field_slice(
    field: _FieldLike, *, start: int, length: int, bits: int
) -> int:
    """Apply ``bits`` to ``field[start:start + length]`` via a register RMW.

    Returns the value that was written. The mask covers exactly the slice
    in the parent register; bits outside the slice survive unchanged.
    """
    reg = _parent_register(field)
    register_shift = int(field.lsb) + start
    register_mask = ((1 << length) - 1) << register_shift
    register_value = (bits << register_shift) & register_mask

    current = int(reg.read_raw())
    new_value = (current & ~register_mask) | register_value
    reg.write_raw(new_value)
    return new_value


def _read_field_value(field: _FieldLike) -> int:
    """Return the field's current value as a plain ``int``.

    Prefers ``read_raw`` (Unit 6 of the API overhaul work, already merged)
    so we never accidentally pay for a ``FieldInt`` allocation here.
    """
    raw = getattr(field, "read_raw", None)
    if raw is not None:
        return int(raw())
    return int(field.read())  # type: ignore[attr-defined]


def _parent_register(field: _FieldLike) -> _RegisterLike:
    """Return the register object that owns ``field``.

    The generated bindings expose this via ``field.parent`` or the dunder
    attribute ``field._parent`` (depending on the C++ class layout). We try
    a couple of names so the proxies work against both real and mock
    fields.
    """
    for attr in ("parent", "_parent", "register", "reg"):
        candidate = getattr(field, attr, None)
        if candidate is not None:
            return candidate
    raise AttributeError(
        f"field {_field_name(field)!r} has no register handle "
        "(expected one of: parent, _parent, register, reg)"
    )


def _lookup_width(obj: object) -> int | None:
    """Read ``info.width`` (Unit 4 contract) or fall back to ``width``.

    Returns ``None`` if no usable width is found. ``width`` values that are
    properties or callables — as they are on a class object before
    instantiation — are skipped so this helper works for both instances and
    classes.
    """
    info = getattr(obj, "info", None)
    if info is not None:
        width = getattr(info, "width", None)
        if isinstance(width, int):
            return width
    width = getattr(obj, "width", None)
    if isinstance(width, int):
        return width
    return None


def _field_width(field: _FieldLike) -> int:
    width = _lookup_width(field)
    if width is None:
        raise AttributeError(
            f"field {_field_name(field)!r} has no width metadata"
        )
    return width


def _field_name(field: _FieldLike) -> str:
    name = getattr(field, "name", None)
    if name:
        return str(name)
    return type(field).__name__


def _bits_to_int(value: BitsWriteValue, length: int) -> int:
    """Normalise the ``write`` argument to a ``length``-bit packed integer."""
    if isinstance(value, (bool, np.bool_)):
        return 1 if value else 0
    if isinstance(value, (int, np.integer)):
        return int(value) & ((1 << length) - 1)
    arr = np.asarray(value)
    if arr.ndim != 1 or arr.shape[0] != length:
        raise ValueError(
            f"bits write expected length-{length} array or int bitmask, "
            f"got shape {arr.shape}"
        )
    # numpy.packbits gives us back the bit pattern as bytes; we then read
    # it out as a little-endian integer. Avoids a Python-level loop and
    # works for arbitrary slice widths (registers can be 64+ bits wide).
    packed = np.packbits(arr.astype(bool), bitorder="little").tobytes()
    return int.from_bytes(packed, byteorder="little")
