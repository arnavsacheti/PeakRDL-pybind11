"""Tests for ``runtime.values`` (sketch §3.2)."""

from __future__ import annotations

import json
import pickle
from enum import IntEnum

import pytest

from peakrdl_pybind11.runtime.values import (
    FieldValue,
    RegisterValue,
    build,
)


# A small enum used by several tests; module-level so pickle can find it.
class BaudRate(IntEnum):
    BAUD_9600 = 0
    BAUD_19200 = 1
    BAUD_115200 = 2


class Parity(IntEnum):
    NONE = 0
    EVEN = 1
    ODD = 2


# Field metadata reused by tests.
UART_FIELDS = {
    "enable": {"lsb": 0, "width": 1, "description": "Enable UART"},
    "baudrate": {
        "lsb": 1,
        "width": 3,
        "encode": BaudRate,
        "description": "Baudrate selection",
    },
    "parity": {
        "lsb": 4,
        "width": 2,
        "encode": Parity,
        "description": "Parity mode",
    },
}


# --------------------------------------------------------------------------- #
#  RegisterValue construction & invariants
# --------------------------------------------------------------------------- #


class TestRegisterValueBasics:
    def test_int_compatible(self) -> None:
        v = RegisterValue(0x42, address=0x4000_1000, width=32, fields=UART_FIELDS)
        assert v == 0x42
        assert int(v) == 0x42
        assert isinstance(v, int)

    def test_truncates_to_width(self) -> None:
        v = RegisterValue(0xFFFF_FFFF_FF, width=32)
        assert int(v) == 0xFFFF_FFFF

    def test_metadata_properties(self) -> None:
        v = RegisterValue(
            0x22,
            address=0x4000_1000,
            width=32,
            fields=UART_FIELDS,
            name="UartControl",
            path="uart[0].control",
        )
        assert v.address == 0x4000_1000
        assert v.width == 32
        assert v.name == "UartControl"
        assert v.path == "uart[0].control"
        assert "enable" in v.fields

    def test_tuple_field_form_normalized(self) -> None:
        # Legacy "tuple of (lsb, width)" form should be accepted.
        v = RegisterValue(0x5, fields={"enable": (0, 1), "baudrate": (1, 3)})
        assert int(v.enable) == 1
        assert int(v.baudrate) == 2


# --------------------------------------------------------------------------- #
#  Hash / equality (§22.1: immutable & hashable)
# --------------------------------------------------------------------------- #


class TestHashAndEquality:
    def test_is_hashable(self) -> None:
        v = RegisterValue(0x42, fields=UART_FIELDS)
        # Should not raise.
        h = hash(v)
        assert h == hash(0x42)

    def test_identical_values_hash_equal(self) -> None:
        v1 = RegisterValue(0x42, address=0x1000, fields=UART_FIELDS)
        v2 = RegisterValue(0x42, address=0x1000, fields=UART_FIELDS)
        assert hash(v1) == hash(v2)
        assert v1 == v2

    def test_usable_as_dict_key(self) -> None:
        v = RegisterValue(0x22, fields=UART_FIELDS)
        d = {v: "seen"}
        assert d[RegisterValue(0x22, fields=UART_FIELDS)] == "seen"


# --------------------------------------------------------------------------- #
#  Field access
# --------------------------------------------------------------------------- #


class TestFieldAccess:
    def test_attribute_access_returns_field_value(self) -> None:
        v = RegisterValue(0x22, fields=UART_FIELDS, path="uart[0].control")
        # 0x22 = 0b100010 → enable=0, baudrate=0b001=1, parity=0b10=2
        assert isinstance(v.enable, FieldValue)
        assert int(v.enable) == 0
        assert int(v.baudrate) == 1
        assert int(v.parity) == 2

    def test_attribute_access_propagates_register_path(self) -> None:
        v = RegisterValue(0x22, fields=UART_FIELDS, path="uart[0].control")
        assert v.enable.register_path == "uart[0].control"
        assert v.enable.name == "enable"

    def test_unknown_field_raises_with_did_you_mean(self) -> None:
        v = RegisterValue(0x22, fields=UART_FIELDS, path="uart[0].control")
        with pytest.raises(AttributeError) as excinfo:
            _ = v.enabel  # typo
        msg = str(excinfo.value)
        assert "enabel" in msg
        assert "enable" in msg

    def test_item_by_field_name(self) -> None:
        v = RegisterValue(0x22, fields=UART_FIELDS)
        assert int(v["baudrate"]) == 1

    def test_item_by_field_name_unknown(self) -> None:
        v = RegisterValue(0x22, fields=UART_FIELDS)
        with pytest.raises(KeyError) as excinfo:
            _ = v["enbale"]
        assert "enable" in str(excinfo.value)

    def test_bit_index_access(self) -> None:
        v = RegisterValue(0b1010, width=8)
        assert v[1] == 1
        assert v[0] == 0
        assert v[3] == 1

    def test_bit_slice_access(self) -> None:
        v = RegisterValue(0xAB, width=8)
        # 0xAB = 0b1010_1011
        assert v[0:4] == 0xB
        assert v[4:8] == 0xA

    def test_unknown_index_type(self) -> None:
        v = RegisterValue(0x22, fields=UART_FIELDS)
        with pytest.raises(TypeError):
            _ = v[1.5]


# --------------------------------------------------------------------------- #
#  .replace(**fields)
# --------------------------------------------------------------------------- #


class TestReplace:
    def test_replace_returns_new_value(self) -> None:
        v = RegisterValue(0x00, fields=UART_FIELDS)
        v2 = v.replace(enable=1)
        # New value has bit set; original is unchanged.
        assert int(v2) == 0x01
        assert int(v) == 0x00
        # Different objects.
        assert v is not v2

    def test_replace_preserves_metadata(self) -> None:
        v = RegisterValue(
            0x00,
            address=0x4000_1000,
            width=32,
            fields=UART_FIELDS,
            name="UartControl",
            path="uart[0].control",
        )
        v2 = v.replace(enable=1, baudrate=2)
        assert v2.address == 0x4000_1000
        assert v2.width == 32
        assert v2.name == "UartControl"
        assert v2.path == "uart[0].control"
        assert "enable" in v2.fields

    def test_replace_with_intenum(self) -> None:
        v = RegisterValue(0x00, fields=UART_FIELDS)
        v2 = v.replace(baudrate=BaudRate.BAUD_115200)
        assert int(v2.baudrate) == 2

    def test_replace_unknown_field_raises_with_did_you_mean(self) -> None:
        v = RegisterValue(0x00, fields=UART_FIELDS)
        with pytest.raises(KeyError) as excinfo:
            v.replace(enabel=1)
        msg = str(excinfo.value)
        assert "enabel" in msg
        assert "enable" in msg

    def test_replace_out_of_range_raises(self) -> None:
        v = RegisterValue(0x00, fields=UART_FIELDS)
        # baudrate is 3 bits — 0..7
        with pytest.raises(ValueError):
            v.replace(baudrate=8)

    def test_replace_clears_then_sets(self) -> None:
        # Start with parity=ODD (2) then set to EVEN (1) — must clear bits.
        v = RegisterValue(0b10_0001, fields=UART_FIELDS)
        # bits 5:4 = 0b10 = ODD, enable=1
        assert int(v.parity) == 2
        v2 = v.replace(parity=1)
        assert int(v2.parity) == 1
        assert int(v2.enable) == 1


# --------------------------------------------------------------------------- #
#  .hex / .bin / .table formatting
# --------------------------------------------------------------------------- #


class TestFormatting:
    def test_hex_default_grouping(self) -> None:
        v = RegisterValue(0x22, width=32, fields=UART_FIELDS)
        assert v.hex() == "0x0000_0022"

    def test_hex_custom_group(self) -> None:
        v = RegisterValue(0xDEADBEEF, width=32)
        assert v.hex(group=2) == "0xde_ad_be_ef"
        assert v.hex(group=8) == "0xdeadbeef"
        assert v.hex(group=4) == "0xdead_beef"

    def test_hex_no_grouping(self) -> None:
        v = RegisterValue(0x22, width=32)
        assert v.hex(group=0) == "0x00000022"

    def test_bin_groups_8(self) -> None:
        v = RegisterValue(0x22, width=32, fields=UART_FIELDS)
        # 0x22 = 0b00000000_00000000_00000000_00100010
        out = v.bin(group=8)
        assert out == "0b00000000_00000000_00000000_00100010"

    def test_bin_with_field_boundaries(self) -> None:
        v = RegisterValue(0x22, width=8, fields=UART_FIELDS)
        out = v.bin(fields=True)
        # We only assert the boundary marker is present and the leading "0b"
        # is correct — the exact column layout is implementation-dependent.
        assert out.startswith("0b")
        assert "|" in out

    def test_bin_no_grouping(self) -> None:
        v = RegisterValue(0x5, width=8)
        assert v.bin(group=0) == "0b00000101"

    def test_table_contains_field_names(self) -> None:
        v = RegisterValue(
            0x22,
            address=0x4000_1000,
            width=32,
            fields=UART_FIELDS,
            name="UartControl",
            path="uart[0].control",
        )
        out = v.table()
        assert "enable" in out
        assert "baudrate" in out
        assert "parity" in out
        assert "BaudRate.BAUD_19200" in out
        assert "Parity.ODD" in out
        assert "uart[0].control" in out

    def test_table_handles_no_fields(self) -> None:
        v = RegisterValue(0x42, width=32)
        out = v.table()
        assert "no fields" in out


# --------------------------------------------------------------------------- #
#  __repr__
# --------------------------------------------------------------------------- #


class TestRepr:
    def test_register_repr(self) -> None:
        v = RegisterValue(0x22, fields=UART_FIELDS, name="UartControl")
        out = repr(v)
        assert "UartControl" in out
        assert "0x22" in out or "22" in out
        # Decoded enum in summary
        assert "BAUD_19200" in out

    def test_register_repr_no_fields(self) -> None:
        v = RegisterValue(0x42, name="Generic")
        out = repr(v)
        assert "Generic" in out
        assert "42" in out


# --------------------------------------------------------------------------- #
#  Pickle round-trip
# --------------------------------------------------------------------------- #


class TestPickle:
    def test_pickle_round_trip_register(self) -> None:
        v = RegisterValue(
            0x22,
            address=0x4000_1000,
            width=32,
            fields=UART_FIELDS,
            name="UartControl",
            path="uart[0].control",
            description="UART control register",
        )
        v2 = pickle.loads(pickle.dumps(v))
        assert v2 == v
        assert v2.address == v.address
        assert v2.width == v.width
        assert v2.name == v.name
        assert v2.path == v.path
        assert v2.description == v.description
        assert int(v2.baudrate) == int(v.baudrate)
        # Encode class survives module-resolved.
        assert v2.fields["baudrate"]["encode"] is BaudRate

    def test_pickle_round_trip_field(self) -> None:
        f = FieldValue(2, lsb=1, width=3, name="baudrate", encode=BaudRate)
        f2 = pickle.loads(pickle.dumps(f))
        assert f2 == f
        assert f2.lsb == 1
        assert f2.width == 3
        assert f2.name == "baudrate"
        assert f2.encode is BaudRate


# --------------------------------------------------------------------------- #
#  JSON round-trip via to_dict/from_dict
# --------------------------------------------------------------------------- #


class TestJsonRoundTrip:
    def test_register_to_dict_from_dict(self) -> None:
        v = RegisterValue(
            0x22,
            address=0x4000_1000,
            width=32,
            fields=UART_FIELDS,
            name="UartControl",
            path="uart[0].control",
        )
        as_dict = v.to_dict()
        # Must be JSON-serializable.
        encoded = json.dumps(as_dict)
        round_tripped = RegisterValue.from_dict(json.loads(encoded))
        assert round_tripped == v
        assert round_tripped.address == v.address
        assert round_tripped.width == v.width
        assert round_tripped.fields["baudrate"]["encode"] is BaudRate

    def test_register_from_json_helper(self) -> None:
        v = RegisterValue(0x05, fields={"enable": (0, 1), "mode": (1, 2)})
        assert RegisterValue.from_json(v.to_json()) == v

    def test_field_to_dict_from_dict(self) -> None:
        f = FieldValue(2, lsb=1, width=3, name="baudrate", encode=BaudRate)
        as_dict = f.to_dict()
        encoded = json.dumps(as_dict)
        round_tripped = FieldValue.from_dict(json.loads(encoded))
        assert round_tripped == f
        assert round_tripped.encode is BaudRate


# --------------------------------------------------------------------------- #
#  FieldValue
# --------------------------------------------------------------------------- #


class TestFieldValue:
    def test_int_compat(self) -> None:
        f = FieldValue(5, lsb=2, width=4, name="foo")
        assert f == 5
        assert int(f) == 5

    def test_truncates_to_width(self) -> None:
        f = FieldValue(0x1F, lsb=0, width=3)
        # 0x1F & 0b111 == 0b111
        assert int(f) == 0b111

    def test_msb_and_mask(self) -> None:
        f = FieldValue(3, lsb=4, width=3)
        assert f.lsb == 4
        assert f.msb == 6
        # mask is the parent-aligned mask
        assert f.mask == (0b111 << 4)

    def test_bool_one_bit(self) -> None:
        true = FieldValue(1, lsb=0, width=1)
        false = FieldValue(0, lsb=0, width=1)
        assert bool(true) is True
        assert bool(false) is False
        # Direct truthiness in conditionals.
        assert true
        assert not false

    def test_bool_multi_bit(self) -> None:
        nonzero = FieldValue(2, lsb=0, width=3)
        zero = FieldValue(0, lsb=0, width=3)
        assert bool(nonzero) is True
        assert bool(zero) is False

    def test_repr_with_encode(self) -> None:
        f = FieldValue(2, lsb=1, width=3, name="baudrate", encode=BaudRate)
        out = repr(f)
        assert "BaudRate" in out
        assert "BAUD_115200" in out
        assert "(2)" in out

    def test_repr_without_encode(self) -> None:
        f = FieldValue(0x5, lsb=2, width=4)
        out = repr(f)
        assert "FieldValue" in out
        assert "0x5" in out

    def test_repr_value_not_in_encode(self) -> None:
        # Value not represented in the IntEnum → falls back to plain repr
        f = FieldValue(7, lsb=0, width=3, encode=BaudRate)
        out = repr(f)
        # 7 isn't a BaudRate member.
        assert "FieldValue" in out

    def test_invalid_width_raises(self) -> None:
        with pytest.raises(ValueError):
            FieldValue(0, lsb=0, width=0)

    def test_decoded_helper(self) -> None:
        f = FieldValue(2, lsb=0, width=3, encode=BaudRate)
        assert f.decoded() == BaudRate.BAUD_115200
        # Out-of-range falls back to int.
        f2 = FieldValue(7, lsb=0, width=3, encode=BaudRate)
        assert f2.decoded() == 7


# --------------------------------------------------------------------------- #
#  build() — compose-then-write
# --------------------------------------------------------------------------- #


class _FakeRegisterClass:
    """A minimal register descriptor — duck-typed for ``build()``."""

    address = 0x4000_1000
    width = 32
    reset = 0x4  # default has parity=NONE, baudrate=2 (BAUD_115200)
    name = "UartControl"
    path = "uart[0].control"
    fields = UART_FIELDS


class TestBuild:
    def test_build_classmethod_with_descriptor(self) -> None:
        v = RegisterValue.build(_FakeRegisterClass, enable=1)
        assert isinstance(v, RegisterValue)
        assert v.address == 0x4000_1000
        assert v.width == 32
        # Reset (0x4) | enable bit
        assert int(v.enable) == 1
        # baudrate from reset (0x4 → bits 1..3 = 0b010 = 2)
        assert int(v.baudrate) == 2

    def test_module_level_build(self) -> None:
        v = build(_FakeRegisterClass, enable=1, baudrate=BaudRate.BAUD_19200)
        assert int(v.enable) == 1
        assert int(v.baudrate) == 1

    def test_build_without_descriptor(self) -> None:
        v = RegisterValue.build(
            None,
            address=0x4000_1000,
            width=32,
            fields=UART_FIELDS,
            enable=1,
        )
        assert int(v.enable) == 1
        assert v.address == 0x4000_1000

    def test_build_unknown_field_raises_with_did_you_mean(self) -> None:
        with pytest.raises(KeyError) as excinfo:
            RegisterValue.build(_FakeRegisterClass, enabel=1)
        assert "enable" in str(excinfo.value)

    def test_build_zero_when_no_descriptor_no_reset(self) -> None:
        v = RegisterValue.build(None, fields={"enable": (0, 1)})
        assert int(v) == 0
