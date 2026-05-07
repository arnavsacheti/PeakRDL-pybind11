"""Tests for ``peakrdl_pybind11.runtime.bits`` (Unit 6 — ``field.bits[i]``).

The tests use hand-rolled mock register/field/master classes so the runtime
module can be exercised without compiling any C++. The mocks model the
shape Unit 1 will produce for generated classes:

* ``MockMaster`` records every ``read``/``write`` against an in-memory dict.
* ``MockRegister`` exposes ``read_raw`` / ``write_raw`` exactly as the
  generated registers do (raw int in, raw int out — no readback on write).
* ``MockField`` exposes ``lsb`` / ``info.width`` and forwards
  ``read_raw`` / ``write_raw`` through the parent register so the proxies'
  cost accounting matches the production binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field

import numpy as np
import pytest

from peakrdl_pybind11.runtime import _registry
from peakrdl_pybind11.runtime.bits import (
    BitProxy,
    BitsAccessor,
    BitsRangeProxy,
    attach_bits_accessor,
)

# ---------------------------------------------------------------------------
# Mock master / register / field — minimal stand-ins for the generated classes.
# ---------------------------------------------------------------------------


@dataclass
class MockMaster:
    """In-memory bus that records every transaction."""

    storage: dict[int, int] = dc_field(default_factory=dict)
    reads: list[tuple[int, int]] = dc_field(default_factory=list)
    writes: list[tuple[int, int, int]] = dc_field(default_factory=list)

    def read(self, address: int, width: int = 4) -> int:
        self.reads.append((address, width))
        return self.storage.get(address, 0)

    def write(self, address: int, value: int, width: int = 4) -> None:
        self.writes.append((address, value, width))
        self.storage[address] = value & ((1 << (8 * width)) - 1)


class MockRegister:
    """Fake register matching the post-Unit 1 surface."""

    def __init__(self, master: MockMaster, address: int, width: int = 4) -> None:
        self.master = master
        self.address = address
        self.width = width  # bytes

    def read_raw(self) -> int:
        return self.master.read(self.address, self.width)

    def write_raw(self, value: int) -> None:
        self.master.write(self.address, int(value), self.width)


@dataclass
class FieldInfo:
    """Tiny stand-in for the Unit 4 ``info`` namespace."""

    width: int


class MockField:
    """Fake multi-bit field bound to a parent ``MockRegister``."""

    def __init__(
        self,
        parent: MockRegister,
        lsb: int,
        width: int,
        name: str = "data",
    ) -> None:
        self.parent = parent
        self.lsb = lsb
        self.info = FieldInfo(width=width)
        self.name = name

    def read_raw(self) -> int:
        register_value = self.parent.read_raw()
        return (register_value >> self.lsb) & ((1 << self.info.width) - 1)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def setup() -> tuple[MockMaster, MockRegister, MockField]:
    """Build a 32-bit register at 0x100 with a 16-bit field at [15:0]."""
    master = MockMaster()
    reg = MockRegister(master, address=0x100, width=4)
    field = MockField(parent=reg, lsb=0, width=16, name="direction")
    return master, reg, field


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_single_bit_read_returns_bool(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0x0020  # bit 5 set

    accessor = BitsAccessor(field)
    proxy = accessor[5]
    assert isinstance(proxy, BitProxy)

    result = proxy.read()
    assert isinstance(result, bool)
    assert result is True


def test_single_bit_read_returns_false_for_clear_bit(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0x0020  # only bit 5 set

    accessor = BitsAccessor(field)
    assert accessor[0].read() is False
    assert accessor[4].read() is False
    assert accessor[6].read() is False


def test_single_bit_write_does_one_read_one_write(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0x00FF  # bit 5 currently set among others

    accessor = BitsAccessor(field)
    accessor[5].write(0)

    assert len(master.reads) == 1
    assert len(master.writes) == 1
    addr, value, _width = master.writes[0]
    assert addr == 0x100
    # Bit 5 cleared, the rest of 0x00FF preserved.
    assert value == 0x00FF & ~(1 << 5)
    assert value == 0x00DF


def test_single_bit_write_sets_bit_to_one(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0x0000

    accessor = BitsAccessor(field)
    accessor[5].write(1)

    addr, value, _width = master.writes[0]
    assert addr == 0x100
    assert value == 1 << 5


def test_single_bit_write_respects_field_lsb() -> None:
    """A field offset to a non-zero lsb must shift the bit index correctly."""
    master = MockMaster()
    reg = MockRegister(master, address=0x200, width=4)
    field = MockField(parent=reg, lsb=8, width=16, name="upper")
    master.storage[0x200] = 0x0000

    BitsAccessor(field)[2].write(1)

    addr, value, _width = master.writes[0]
    assert addr == 0x200
    # bit 2 of the field == bit 8 + 2 == bit 10 of the register.
    assert value == 1 << (8 + 2)


def test_slice_read_returns_ndarray_of_bool(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0x0000

    accessor = BitsAccessor(field)
    range_proxy = accessor[0:4]
    assert isinstance(range_proxy, BitsRangeProxy)

    result = range_proxy.read()
    assert isinstance(result, np.ndarray)
    assert result.dtype == bool
    assert result.shape == (4,)
    np.testing.assert_array_equal(result, np.array([False, False, False, False]))


def test_slice_read_picks_up_set_bits(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0b1010  # bits 1 and 3 set

    result = BitsAccessor(field)[0:4].read()
    np.testing.assert_array_equal(result, np.array([False, True, False, True]))


def test_full_slice_read_covers_field_width(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0xFF00

    result = BitsAccessor(field)[:].read()
    assert result.shape == (16,)
    expected = np.array([(0xFF00 >> i) & 1 for i in range(16)], dtype=bool)
    np.testing.assert_array_equal(result, expected)


def test_full_slice_write_with_int_bitmask(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0x0000

    BitsAccessor(field)[:].write(0xFF00)

    assert len(master.reads) == 1
    assert len(master.writes) == 1
    addr, value, _width = master.writes[0]
    assert addr == 0x100
    # Field at lsb=0, slice is [0:16); bitmask 0xFF00 maps directly.
    assert value == 0xFF00


def test_full_slice_write_preserves_bits_outside_field() -> None:
    """Writing through the field slice must not stomp on bits outside it."""
    master = MockMaster()
    reg = MockRegister(master, address=0x300, width=4)
    field = MockField(parent=reg, lsb=0, width=16, name="lower")
    master.storage[0x300] = 0xAAAA_5555  # upper half should survive

    BitsAccessor(field)[:].write(0xFF00)

    _addr, value, _width = master.writes[0]
    assert value == 0xAAAA_FF00


def test_partial_slice_write_bitmask(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    """``bits[8:16].write(0xFF)`` flips the upper byte of the field."""
    master, _reg, field = setup
    master.storage[0x100] = 0x0000

    BitsAccessor(field)[8:16].write(0xFF)

    _addr, value, _width = master.writes[0]
    # Slice starts at bit 8 of the field, eight bits wide; LSB of 0xFF lands
    # at bit 8 of the field == bit 8 of the register (lsb=0).
    assert value == 0xFF00


def test_slice_write_with_ndarray(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0x0000

    BitsAccessor(field)[0:4].write(np.array([True, False, True, False]))

    _addr, value, _width = master.writes[0]
    # bits 0 and 2 set → 0b0101 == 0x5.
    assert value == 0x5


def test_slice_write_with_list(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0x0000

    BitsAccessor(field)[0:4].write([0, 1, 1, 0])

    _addr, value, _width = master.writes[0]
    assert value == 0b0110


def test_setitem_int_assigns_single_bit(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0x00FF

    accessor = BitsAccessor(field)
    accessor[5] = 0  # sugar for accessor[5].write(0)

    _addr, value, _width = master.writes[0]
    assert value == 0x00DF


def test_setitem_slice_assigns_bulk(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0x0000

    accessor = BitsAccessor(field)
    accessor[0:4] = 0b1010

    _addr, value, _width = master.writes[0]
    assert value == 0b1010


def test_iter_yields_bit_proxies(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    _master, _reg, field = setup
    proxies = list(BitsAccessor(field))
    assert len(proxies) == 16
    assert all(isinstance(p, BitProxy) for p in proxies)
    assert [p.index for p in proxies] == list(range(16))


def test_negative_index(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 1 << 15  # MSB of 16-bit field
    assert BitsAccessor(field)[-1].read() is True
    assert BitsAccessor(field)[-1].index == 15


def test_index_out_of_range_raises(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    _master, _reg, field = setup
    accessor = BitsAccessor(field)
    with pytest.raises(IndexError):
        accessor[16]
    with pytest.raises(IndexError):
        accessor[-17]


def test_non_int_index_raises(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    _master, _reg, field = setup
    accessor = BitsAccessor(field)
    with pytest.raises(TypeError):
        accessor["not an int"]  # type: ignore[index]


def test_slice_with_step_raises(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    _master, _reg, field = setup
    accessor = BitsAccessor(field)
    with pytest.raises(ValueError):
        accessor[0:8:2]


def test_bool_and_int_coercion(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 1 << 5
    proxy = BitsAccessor(field)[5]
    assert bool(proxy) is True
    assert int(proxy) == 1


def test_iter_range_proxy(setup: tuple[MockMaster, MockRegister, MockField]) -> None:
    master, _reg, field = setup
    master.storage[0x100] = 0b0101
    bits = list(BitsAccessor(field)[0:4])
    assert bits == [True, False, True, False]


def test_attach_bits_accessor_attaches_property() -> None:
    """``attach_bits_accessor`` adds a ``bits`` property to multi-bit classes."""

    class FakeField:
        info = FieldInfo(width=16)
        lsb = 0

        def __init__(self, parent: MockRegister) -> None:
            self.parent = parent

        def read_raw(self) -> int:
            value = self.parent.read_raw()
            return (value >> self.lsb) & ((1 << self.info.width) - 1)

    attach_bits_accessor(FakeField)

    master = MockMaster()
    reg = MockRegister(master, address=0x100)
    instance = FakeField(parent=reg)

    master.storage[0x100] = 1 << 7
    assert instance.bits[7].read() is True


def test_attach_bits_accessor_skips_single_bit_fields() -> None:
    """Single-bit fields don't get a ``bits`` namespace."""

    class FakeBitField:
        info = FieldInfo(width=1)
        lsb = 0

    attach_bits_accessor(FakeBitField)
    assert not hasattr(FakeBitField, "bits")


def test_attach_bits_accessor_idempotent() -> None:
    """Calling twice is a no-op."""

    class FakeField:
        info = FieldInfo(width=8)
        lsb = 0

    attach_bits_accessor(FakeField)
    first = FakeField.bits
    attach_bits_accessor(FakeField)
    assert FakeField.bits is first


def test_registry_seam_invokes_attach() -> None:
    """The bits module registers itself via ``register_field_enhancement``.

    Driving ``apply_enhancements`` with no register-side callbacks should
    still attach ``bits`` to every multi-bit field class.
    """

    class FakeField:
        info = FieldInfo(width=4)
        lsb = 0

    _registry.apply_enhancements(register_classes={}, field_classes=[FakeField])
    assert hasattr(FakeField, "bits")
