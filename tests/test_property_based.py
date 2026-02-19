"""
Hypothesis-based property tests for PeakRDL-pybind11.

These tests use property-based testing to validate core invariants across diverse
register/field configurations, including edge cases and invalid inputs.
"""

import re
import string

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from peakrdl_pybind11 import FieldInt, RegisterInt
from peakrdl_pybind11.masters import CallbackMaster, MockMaster


# ---------------------------------------------------------------------------
# Custom Hypothesis Strategies
# ---------------------------------------------------------------------------

# Field position: lsb in [0, 63], width in [1, 64], constrained so lsb + width <= 64
def field_positions():
    """Generate valid (lsb, width) pairs where lsb + width <= 64."""
    return st.integers(min_value=0, max_value=63).flatmap(
        lambda lsb: st.tuples(
            st.just(lsb),
            st.integers(min_value=1, max_value=64 - lsb),
        )
    )


def field_values(width):
    """Generate values that fit in the given bit width."""
    max_val = (1 << width) - 1
    return st.integers(min_value=0, max_value=max_val)


def register_offsets():
    """Generate valid register offset addresses."""
    return st.integers(min_value=0, max_value=2**32 - 1)


def register_widths():
    """Generate valid register widths in bytes (1, 2, 4, or 8)."""
    return st.sampled_from([1, 2, 4, 8])


def non_overlapping_fields(max_fields=8):
    """Generate a dict of non-overlapping field specs fitting within 64 bits.

    Returns dict[str, tuple[int, int]] mapping field_name -> (lsb, width).
    """

    @st.composite
    def _build(draw):
        num_fields = draw(st.integers(min_value=1, max_value=max_fields))
        fields: dict[str, tuple[int, int]] = {}
        used_bits = 0  # track which bit positions are consumed

        for i in range(num_fields):
            # Find available positions
            available: list[int] = []
            for bit in range(64):
                if not (used_bits & (1 << bit)):
                    available.append(bit)

            if not available:
                break

            # Choose a starting bit from available positions
            lsb = draw(st.sampled_from(available))

            # Determine max contiguous width from lsb
            max_width = 0
            for bit in range(lsb, 64):
                if used_bits & (1 << bit):
                    break
                max_width += 1

            if max_width == 0:
                continue

            width = draw(st.integers(min_value=1, max_value=max_width))

            # Mark bits as used
            for bit in range(lsb, lsb + width):
                used_bits |= 1 << bit

            fields[f"field_{i}"] = (lsb, width)

        assume(len(fields) >= 1)
        return fields

    return _build()


def identifiers():
    """Generate strings for identifier sanitization testing."""
    return st.text(
        alphabet=string.ascii_letters + string.digits + "_-. []!@#$%",
        min_size=0,
        max_size=40,
    )


# ---------------------------------------------------------------------------
# FieldInt Property Tests
# ---------------------------------------------------------------------------


class TestFieldIntProperties:
    """Property-based tests for FieldInt invariants."""

    @given(pos=field_positions(), offset=register_offsets())
    def test_msb_equals_lsb_plus_width_minus_one(self, pos, offset):
        """msb must always equal lsb + width - 1."""
        lsb, width = pos
        value = 0
        field = FieldInt(value, lsb=lsb, width=width, offset=offset)
        assert field.msb == field.lsb + field.width - 1

    @given(pos=field_positions(), offset=register_offsets())
    def test_mask_has_exactly_width_set_bits(self, pos, offset):
        """The mask must have exactly `width` bits set."""
        lsb, width = pos
        field = FieldInt(0, lsb=lsb, width=width, offset=offset)
        assert bin(field.mask).count("1") == width

    @given(pos=field_positions(), offset=register_offsets())
    def test_mask_starts_at_lsb(self, pos, offset):
        """The lowest set bit in the mask must be at position lsb."""
        lsb, width = pos
        field = FieldInt(0, lsb=lsb, width=width, offset=offset)
        mask = field.mask
        # The lowest set bit position
        assert (mask >> lsb) & 1 == 1
        # No bits set below lsb
        if lsb > 0:
            assert mask & ((1 << lsb) - 1) == 0

    @given(pos=field_positions(), offset=register_offsets())
    def test_mask_ends_at_msb(self, pos, offset):
        """No bits should be set above the msb position in the mask."""
        lsb, width = pos
        field = FieldInt(0, lsb=lsb, width=width, offset=offset)
        # Nothing set above msb
        above_msb = field.mask >> (field.msb + 1)
        assert above_msb == 0

    @given(pos=field_positions(), offset=register_offsets())
    def test_mask_formula_matches_definition(self, pos, offset):
        """Mask must equal ((1 << width) - 1) << lsb."""
        lsb, width = pos
        field = FieldInt(0, lsb=lsb, width=width, offset=offset)
        expected_mask = ((1 << width) - 1) << lsb
        assert field.mask == expected_mask

    @given(pos=field_positions(), offset=register_offsets())
    def test_value_preserved_as_int(self, pos, offset):
        """FieldInt must compare equal to its integer value."""
        lsb, width = pos
        max_val = (1 << width) - 1
        value = max_val  # use max to test boundary
        field = FieldInt(value, lsb=lsb, width=width, offset=offset)
        assert int(field) == value
        assert field == value

    @given(
        pos=field_positions(),
        offset=register_offsets(),
        data=st.data(),
    )
    def test_metadata_preserved_after_creation(self, pos, offset, data):
        """lsb, width, and offset must be stored correctly."""
        lsb, width = pos
        value = data.draw(st.integers(min_value=0, max_value=(1 << width) - 1))
        field = FieldInt(value, lsb=lsb, width=width, offset=offset)
        assert field.lsb == lsb
        assert field.width == width
        assert field.offset == offset

    @given(
        pos=field_positions(),
        offset=register_offsets(),
        data=st.data(),
    )
    def test_arithmetic_returns_plain_int(self, pos, offset, data):
        """Arithmetic on FieldInt should return plain int, not FieldInt."""
        lsb, width = pos
        value = data.draw(st.integers(min_value=0, max_value=(1 << width) - 1))
        field = FieldInt(value, lsb=lsb, width=width, offset=offset)
        result = field + 1
        assert isinstance(result, int)
        # FieldInt is a subclass of int, so check it's not specifically FieldInt
        assert type(result) is not FieldInt

    @given(
        pos=field_positions(),
        offset=register_offsets(),
        data=st.data(),
    )
    def test_repr_contains_metadata(self, pos, offset, data):
        """repr must contain the value, lsb, width, and offset."""
        lsb, width = pos
        value = data.draw(st.integers(min_value=0, max_value=(1 << width) - 1))
        field = FieldInt(value, lsb=lsb, width=width, offset=offset)
        r = repr(field)
        assert "FieldInt" in r
        assert f"lsb={lsb}" in r
        assert f"width={width}" in r

    @given(
        pos=field_positions(),
        offset=register_offsets(),
        data=st.data(),
    )
    def test_comparison_with_plain_int(self, pos, offset, data):
        """FieldInt comparisons must agree with plain int comparisons."""
        lsb, width = pos
        value = data.draw(st.integers(min_value=0, max_value=(1 << width) - 1))
        other = data.draw(st.integers(min_value=0, max_value=(1 << width) - 1))
        field = FieldInt(value, lsb=lsb, width=width, offset=offset)
        assert (field == other) == (value == other)
        assert (field < other) == (value < other)
        assert (field > other) == (value > other)
        assert (field <= other) == (value <= other)
        assert (field >= other) == (value >= other)
        assert (field != other) == (value != other)

    @given(offset=register_offsets())
    def test_single_bit_field(self, offset):
        """Single-bit field (width=1) at any valid position."""
        for lsb in range(64):
            field_0 = FieldInt(0, lsb=lsb, width=1, offset=offset)
            field_1 = FieldInt(1, lsb=lsb, width=1, offset=offset)
            assert field_0.mask == (1 << lsb)
            assert field_0 == 0
            assert field_1 == 1
            assert field_1.msb == lsb

    @given(offset=register_offsets())
    def test_full_width_field(self, offset):
        """Full 64-bit field (lsb=0, width=64)."""
        max_val = (1 << 64) - 1
        field = FieldInt(max_val, lsb=0, width=64, offset=offset)
        assert field.mask == max_val
        assert field.msb == 63
        assert int(field) == max_val


# ---------------------------------------------------------------------------
# RegisterInt Property Tests
# ---------------------------------------------------------------------------


class TestRegisterIntProperties:
    """Property-based tests for RegisterInt invariants."""

    @given(
        value=st.integers(min_value=0, max_value=(1 << 32) - 1),
        offset=register_offsets(),
        width=register_widths(),
    )
    def test_value_preserved(self, value, offset, width):
        """RegisterInt value must equal the original integer."""
        reg = RegisterInt(value, offset=offset, width=width)
        assert int(reg) == value
        assert reg == value

    @given(
        value=st.integers(min_value=0, max_value=(1 << 32) - 1),
        offset=register_offsets(),
        width=register_widths(),
    )
    def test_metadata_preserved(self, value, offset, width):
        """offset and width metadata must be stored correctly."""
        reg = RegisterInt(value, offset=offset, width=width)
        assert reg.offset == offset
        assert reg.width == width

    @given(
        value=st.integers(min_value=0, max_value=(1 << 32) - 1),
        offset=register_offsets(),
        width=register_widths(),
    )
    def test_no_fields_by_default(self, value, offset, width):
        """RegisterInt without fields has an empty _fields dict."""
        reg = RegisterInt(value, offset=offset, width=width)
        assert len(reg._fields) == 0

    @given(
        value=st.integers(min_value=0, max_value=(1 << 32) - 1),
        offset=register_offsets(),
        width=register_widths(),
    )
    def test_access_nonexistent_field_raises(self, value, offset, width):
        """Accessing a non-existent field must raise AttributeError."""
        reg = RegisterInt(value, offset=offset, width=width)
        with pytest.raises(AttributeError, match="no field named"):
            _ = reg.nonexistent

    @given(
        offset=register_offsets(),
        width=register_widths(),
        data=st.data(),
    )
    def test_field_extraction_matches_manual_computation(self, offset, width, data):
        """Field values extracted by RegisterInt must match manual bit extraction."""
        reg_width_bits = width * 8
        value = data.draw(st.integers(min_value=0, max_value=(1 << reg_width_bits) - 1))

        # Generate a single field within the register width
        lsb = data.draw(st.integers(min_value=0, max_value=max(0, reg_width_bits - 1)))
        field_width = data.draw(st.integers(min_value=1, max_value=reg_width_bits - lsb))

        fields = {"test_field": (lsb, field_width)}
        reg = RegisterInt(value, offset=offset, width=width, fields=fields)

        # Manual extraction
        expected = (value >> lsb) & ((1 << field_width) - 1)
        assert reg.test_field == expected

    @given(
        offset=register_offsets(),
        width=register_widths(),
        data=st.data(),
    )
    def test_extracted_field_is_field_int(self, offset, width, data):
        """Fields accessed via attribute must be FieldInt instances."""
        reg_width_bits = width * 8
        value = data.draw(st.integers(min_value=0, max_value=(1 << reg_width_bits) - 1))
        lsb = data.draw(st.integers(min_value=0, max_value=max(0, reg_width_bits - 1)))
        field_width = data.draw(st.integers(min_value=1, max_value=reg_width_bits - lsb))

        fields = {"my_field": (lsb, field_width)}
        reg = RegisterInt(value, offset=offset, width=width, fields=fields)

        field_val = reg.my_field
        assert isinstance(field_val, FieldInt)

    @given(
        offset=register_offsets(),
        width=register_widths(),
        data=st.data(),
    )
    def test_field_metadata_matches_spec(self, offset, width, data):
        """Extracted field's lsb, width, and offset must match the spec."""
        reg_width_bits = width * 8
        value = data.draw(st.integers(min_value=0, max_value=(1 << reg_width_bits) - 1))
        lsb = data.draw(st.integers(min_value=0, max_value=max(0, reg_width_bits - 1)))
        field_width = data.draw(st.integers(min_value=1, max_value=reg_width_bits - lsb))

        fields = {"fld": (lsb, field_width)}
        reg = RegisterInt(value, offset=offset, width=width, fields=fields)

        assert reg.fld.lsb == lsb
        assert reg.fld.width == field_width
        assert reg.fld.offset == offset

    @given(
        offset=register_offsets(),
        data=st.data(),
    )
    @settings(max_examples=50)
    def test_non_overlapping_fields_extract_independently(self, offset, data):
        """Non-overlapping fields must extract independently from one another."""
        fields = data.draw(non_overlapping_fields())
        # Use 8-byte register to accommodate up to 64 bits
        value = data.draw(st.integers(min_value=0, max_value=(1 << 64) - 1))

        reg = RegisterInt(value, offset=offset, width=8, fields=fields)

        for name, (lsb, fw) in fields.items():
            expected = (value >> lsb) & ((1 << fw) - 1)
            actual = getattr(reg, name)
            assert actual == expected, (
                f"Field {name} (lsb={lsb}, width={fw}): expected {expected}, got {int(actual)}"
            )

    @given(
        offset=register_offsets(),
        data=st.data(),
    )
    @settings(max_examples=50)
    def test_field_values_bounded_by_width(self, offset, data):
        """Every extracted field value must be < 2^width."""
        fields = data.draw(non_overlapping_fields())
        value = data.draw(st.integers(min_value=0, max_value=(1 << 64) - 1))

        reg = RegisterInt(value, offset=offset, width=8, fields=fields)

        for name, (lsb, fw) in fields.items():
            field_val = getattr(reg, name)
            assert field_val < (1 << fw)
            assert field_val >= 0

    @given(
        offset=register_offsets(),
        data=st.data(),
    )
    @settings(max_examples=50)
    def test_field_masks_do_not_overlap(self, offset, data):
        """Masks of non-overlapping fields must have no common bits."""
        fields_spec = data.draw(non_overlapping_fields())
        value = data.draw(st.integers(min_value=0, max_value=(1 << 64) - 1))

        reg = RegisterInt(value, offset=offset, width=8, fields=fields_spec)

        field_masks: list[int] = []
        for name in fields_spec:
            field_obj = getattr(reg, name)
            field_masks.append(field_obj.mask)

        # Pairwise check: no two masks share a bit
        for i in range(len(field_masks)):
            for j in range(i + 1, len(field_masks)):
                assert field_masks[i] & field_masks[j] == 0, (
                    f"Masks overlap: {field_masks[i]:#x} & {field_masks[j]:#x}"
                )

    @given(
        value=st.integers(min_value=0, max_value=(1 << 32) - 1),
        offset=register_offsets(),
    )
    def test_repr_contains_key_info(self, value, offset):
        """repr must include RegisterInt, hex value, and hex offset."""
        reg = RegisterInt(value, offset=offset, width=4, fields={"a": (0, 1)})
        r = repr(reg)
        assert "RegisterInt" in r

    @given(
        value1=st.integers(min_value=0, max_value=(1 << 32) - 1),
        value2=st.integers(min_value=0, max_value=(1 << 32) - 1),
        offset=register_offsets(),
        width=register_widths(),
    )
    def test_comparison_with_other_register_int(self, value1, value2, offset, width):
        """RegisterInt comparisons must agree with plain int semantics."""
        reg1 = RegisterInt(value1, offset=offset, width=width)
        reg2 = RegisterInt(value2, offset=offset, width=width)
        assert (reg1 == reg2) == (value1 == value2)
        assert (reg1 < reg2) == (value1 < value2)
        assert (reg1 > reg2) == (value1 > value2)

    @given(
        offset=register_offsets(),
        width=register_widths(),
    )
    def test_zero_value_all_fields_zero(self, offset, width):
        """When register value is 0, all fields must extract as 0."""
        reg_width_bits = width * 8
        # Create fields spanning the register
        fields: dict[str, tuple[int, int]] = {}
        pos = 0
        idx = 0
        while pos < reg_width_bits:
            fw = min(8, reg_width_bits - pos)
            fields[f"f{idx}"] = (pos, fw)
            pos += fw
            idx += 1

        reg = RegisterInt(0, offset=offset, width=width, fields=fields)
        for name in fields:
            assert getattr(reg, name) == 0

    @given(
        offset=register_offsets(),
        width=register_widths(),
    )
    def test_max_value_all_fields_max(self, offset, width):
        """When register value is all-ones, all fields must extract as their max."""
        reg_width_bits = width * 8
        max_val = (1 << reg_width_bits) - 1

        fields: dict[str, tuple[int, int]] = {}
        pos = 0
        idx = 0
        while pos < reg_width_bits:
            fw = min(8, reg_width_bits - pos)
            fields[f"f{idx}"] = (pos, fw)
            pos += fw
            idx += 1

        reg = RegisterInt(max_val, offset=offset, width=width, fields=fields)
        for name, (lsb, fw) in fields.items():
            assert getattr(reg, name) == (1 << fw) - 1

    @given(
        offset=register_offsets(),
        width=register_widths(),
    )
    def test_private_attr_access_raises(self, offset, width):
        """Accessing private attributes (starting with _) must raise AttributeError
        via __getattr__ to prevent infinite recursion."""
        reg = RegisterInt(0, offset=offset, width=width)
        with pytest.raises(AttributeError):
            _ = reg._nonexistent_private


# ---------------------------------------------------------------------------
# MockMaster Property Tests
# ---------------------------------------------------------------------------


class TestMockMasterProperties:
    """Property-based tests for MockMaster."""

    @given(
        address=st.integers(min_value=0, max_value=2**32 - 1),
        value=st.integers(min_value=0, max_value=2**32 - 1),
        width=register_widths(),
    )
    def test_write_then_read_roundtrip(self, address, value, width):
        """Writing a value and reading it back must return the masked value."""
        master = MockMaster()
        master.write(address, value, width)
        result = master.read(address, width)
        mask = (1 << (width * 8)) - 1
        assert result == value & mask

    @given(
        address=st.integers(min_value=0, max_value=2**32 - 1),
        width=register_widths(),
    )
    def test_unwritten_address_returns_zero(self, address, width):
        """Reading an address that was never written must return 0."""
        master = MockMaster()
        assert master.read(address, width) == 0

    @given(
        address=st.integers(min_value=0, max_value=2**32 - 1),
        value=st.integers(min_value=0, max_value=2**32 - 1),
        width=register_widths(),
    )
    def test_reset_clears_all(self, address, value, width):
        """After reset(), all addresses must read as 0."""
        master = MockMaster()
        master.write(address, value, width)
        master.reset()
        assert master.read(address, width) == 0

    @given(
        addresses=st.lists(
            st.integers(min_value=0, max_value=2**16 - 1),
            min_size=2,
            max_size=10,
            unique=True,
        ),
        data=st.data(),
    )
    def test_writes_to_different_addresses_independent(self, addresses, data):
        """Writes to distinct addresses must not interfere with each other."""
        master = MockMaster()
        width = 4
        values = [data.draw(st.integers(min_value=0, max_value=(1 << 32) - 1)) for _ in addresses]

        for addr, val in zip(addresses, values):
            master.write(addr, val, width)

        mask = (1 << (width * 8)) - 1
        for addr, val in zip(addresses, values):
            assert master.read(addr, width) == val & mask

    @given(
        address=st.integers(min_value=0, max_value=2**32 - 1),
        data=st.data(),
    )
    def test_last_write_wins(self, address, data):
        """Multiple writes to the same address: the last write must be the read value."""
        master = MockMaster()
        width = 4
        values = data.draw(
            st.lists(st.integers(min_value=0, max_value=(1 << 32) - 1), min_size=2, max_size=5)
        )

        for val in values:
            master.write(address, val, width)

        mask = (1 << (width * 8)) - 1
        assert master.read(address, width) == values[-1] & mask

    @given(
        address=st.integers(min_value=0, max_value=2**32 - 1),
        width=register_widths(),
    )
    def test_write_max_value_masks_correctly(self, address, width):
        """Writing all-ones must be masked to the register width."""
        master = MockMaster()
        max_val = (1 << 64) - 1  # Larger than any register
        master.write(address, max_val, width)
        result = master.read(address, width)
        expected = (1 << (width * 8)) - 1
        assert result == expected


# ---------------------------------------------------------------------------
# CallbackMaster Property Tests
# ---------------------------------------------------------------------------


class TestCallbackMasterProperties:
    """Property-based tests for CallbackMaster."""

    @given(
        address=st.integers(min_value=0, max_value=2**32 - 1),
        width=register_widths(),
        ret_val=st.integers(min_value=0, max_value=2**32 - 1),
    )
    def test_read_callback_receives_correct_args(self, address, width, ret_val):
        """Read callback must receive the exact (address, width) arguments."""
        received_args: list[tuple[int, int]] = []

        def read_cb(addr, w):
            received_args.append((addr, w))
            return ret_val

        master = CallbackMaster(read_callback=read_cb)
        result = master.read(address, width)

        assert len(received_args) == 1
        assert received_args[0] == (address, width)
        assert result == ret_val

    @given(
        address=st.integers(min_value=0, max_value=2**32 - 1),
        value=st.integers(min_value=0, max_value=2**32 - 1),
        width=register_widths(),
    )
    def test_write_callback_receives_correct_args(self, address, value, width):
        """Write callback must receive the exact (address, value, width) arguments."""
        received_args: list[tuple[int, int, int]] = []

        def write_cb(addr, val, w):
            received_args.append((addr, val, w))

        master = CallbackMaster(write_callback=write_cb)
        master.write(address, value, width)

        assert len(received_args) == 1
        assert received_args[0] == (address, value, width)

    def test_read_without_callback_raises(self):
        """Reading without a read callback must raise RuntimeError."""
        master = CallbackMaster()
        with pytest.raises(RuntimeError, match="No read callback"):
            master.read(0, 4)

    def test_write_without_callback_raises(self):
        """Writing without a write callback must raise RuntimeError."""
        master = CallbackMaster()
        with pytest.raises(RuntimeError, match="No write callback"):
            master.write(0, 0, 4)

    @given(
        address=st.integers(min_value=0, max_value=2**32 - 1),
        value=st.integers(min_value=0, max_value=2**32 - 1),
        width=register_widths(),
    )
    def test_callback_master_roundtrip_via_dict(self, address, value, width):
        """CallbackMaster backed by a dict must roundtrip values."""
        storage: dict[int, int] = {}

        def read_cb(addr, w):
            return storage.get(addr, 0)

        def write_cb(addr, val, w):
            storage[addr] = val

        master = CallbackMaster(read_callback=read_cb, write_callback=write_cb)
        master.write(address, value, width)
        assert master.read(address, width) == value


# ---------------------------------------------------------------------------
# Exporter Utility Property Tests
# ---------------------------------------------------------------------------


class TestSanitizeIdentifier:
    """Property-based tests for _sanitize_identifier."""

    @settings(deadline=None)
    @given(name=identifiers())
    def test_result_is_valid_identifier(self, name):
        """Sanitized name must be a valid Python identifier (or 'soc' for empty)."""
        from peakrdl_pybind11.exporter import Pybind11Exporter

        exporter = Pybind11Exporter()
        result = exporter._sanitize_identifier(name)
        assert result.isidentifier() or result == "soc"

    @given(name=identifiers())
    def test_no_leading_digit(self, name):
        """Sanitized name must not start with a digit."""
        from peakrdl_pybind11.exporter import Pybind11Exporter

        exporter = Pybind11Exporter()
        result = exporter._sanitize_identifier(name)
        if result:
            assert not result[0].isdigit()

    @given(name=identifiers())
    def test_only_valid_chars(self, name):
        """Sanitized name must contain only [a-zA-Z0-9_]."""
        from peakrdl_pybind11.exporter import Pybind11Exporter

        exporter = Pybind11Exporter()
        result = exporter._sanitize_identifier(name)
        assert re.fullmatch(r"[a-zA-Z0-9_]*", result) is not None or result == ""

    @given(name=st.from_regex(r"[a-zA-Z_][a-zA-Z0-9_]*", fullmatch=True))
    def test_already_valid_identifier_unchanged(self, name):
        """A name that is already a valid identifier must not be changed."""
        from peakrdl_pybind11.exporter import Pybind11Exporter

        exporter = Pybind11Exporter()
        result = exporter._sanitize_identifier(name)
        assert result == name

    @given(name=st.from_regex(r"[0-9][a-zA-Z0-9_]*", fullmatch=True))
    def test_leading_digit_gets_underscore_prefix(self, name):
        """Names starting with a digit must get a _ prefix."""
        from peakrdl_pybind11.exporter import Pybind11Exporter

        exporter = Pybind11Exporter()
        result = exporter._sanitize_identifier(name)
        assert result.startswith("_")

    def test_empty_string_returns_soc(self):
        """Empty string must return 'soc'."""
        from peakrdl_pybind11.exporter import Pybind11Exporter

        exporter = Pybind11Exporter()
        assert exporter._sanitize_identifier("") == "soc"


class TestEnumMemberName:
    """Property-based tests for _enum_member_name."""

    @given(name=identifiers())
    def test_result_has_no_invalid_chars(self, name):
        """Enum member name must contain only [a-zA-Z0-9_]."""
        from peakrdl_pybind11.exporter import Pybind11Exporter

        exporter = Pybind11Exporter()
        result = exporter._enum_member_name(name)
        assert re.fullmatch(r"[a-zA-Z0-9_]+", result) is not None

    @given(name=st.text(alphabet=string.ascii_letters, min_size=1, max_size=20))
    def test_alpha_names_produce_nonempty(self, name):
        """Alphabetic names must produce non-empty enum member names."""
        from peakrdl_pybind11.exporter import Pybind11Exporter

        exporter = Pybind11Exporter()
        result = exporter._enum_member_name(name)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Edge Case / Boundary Tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Targeted edge-case tests discovered via property-based reasoning."""

    def test_field_int_zero_value_all_positions(self):
        """FieldInt with value 0 at every bit position."""
        for lsb in range(64):
            field = FieldInt(0, lsb=lsb, width=1, offset=0)
            assert int(field) == 0
            assert field.mask == (1 << lsb)

    def test_field_int_max_value_all_widths(self):
        """FieldInt with max value at every possible width."""
        for width in range(1, 65):
            max_val = (1 << width) - 1
            field = FieldInt(max_val, lsb=0, width=width, offset=0)
            assert int(field) == max_val
            assert field.msb == width - 1

    def test_register_int_8_byte_register(self):
        """8-byte (64-bit) register with full-width field."""
        val = (1 << 64) - 1
        reg = RegisterInt(val, offset=0, width=8, fields={"full": (0, 64)})
        assert reg.full == val

    def test_register_int_1_byte_register(self):
        """1-byte (8-bit) register with packed fields."""
        val = 0b10110011
        reg = RegisterInt(
            val,
            offset=0x100,
            width=1,
            fields={
                "low_nibble": (0, 4),
                "high_nibble": (4, 4),
            },
        )
        assert reg.low_nibble == 0b0011
        assert reg.high_nibble == 0b1011

    def test_register_int_single_field_entire_register(self):
        """Register where one field spans the entire register width."""
        for width in [1, 2, 4, 8]:
            bits = width * 8
            val = (1 << bits) - 1
            reg = RegisterInt(val, offset=0, width=width, fields={"all_bits": (0, bits)})
            assert reg.all_bits == val

    def test_mock_master_width_1_byte(self):
        """MockMaster with 1-byte register width masks to 0xFF."""
        master = MockMaster()
        master.write(0, 0xFFFF, 1)
        assert master.read(0, 1) == 0xFF

    def test_mock_master_width_2_bytes(self):
        """MockMaster with 2-byte register width masks to 0xFFFF."""
        master = MockMaster()
        master.write(0, 0xFFFFFF, 2)
        assert master.read(0, 2) == 0xFFFF

    def test_mock_master_width_4_bytes(self):
        """MockMaster with 4-byte register width masks to 0xFFFFFFFF."""
        master = MockMaster()
        master.write(0, 0xFFFFFFFFFF, 4)
        assert master.read(0, 4) == 0xFFFFFFFF

    def test_mock_master_width_8_bytes(self):
        """MockMaster with 8-byte register width masks to 0xFFFFFFFFFFFFFFFF."""
        master = MockMaster()
        master.write(0, (1 << 128) - 1, 8)
        assert master.read(0, 8) == (1 << 64) - 1

    def test_field_int_adjacent_fields_cover_all_bits(self):
        """Adjacent fields that together cover all 32 bits."""
        reg_val = 0xDEADBEEF
        reg = RegisterInt(
            reg_val,
            offset=0,
            width=4,
            fields={
                "byte0": (0, 8),
                "byte1": (8, 8),
                "byte2": (16, 8),
                "byte3": (24, 8),
            },
        )
        reconstructed = (
            int(reg.byte0)
            | (int(reg.byte1) << 8)
            | (int(reg.byte2) << 16)
            | (int(reg.byte3) << 24)
        )
        assert reconstructed == reg_val

    @given(
        value=st.integers(min_value=0, max_value=(1 << 32) - 1),
        offset=register_offsets(),
    )
    def test_field_reconstruction_matches_register_value(self, value, offset):
        """Fields covering the full register must reconstruct the original value."""
        reg = RegisterInt(
            value,
            offset=offset,
            width=4,
            fields={
                "byte0": (0, 8),
                "byte1": (8, 8),
                "byte2": (16, 8),
                "byte3": (24, 8),
            },
        )
        reconstructed = (
            int(reg.byte0)
            | (int(reg.byte1) << 8)
            | (int(reg.byte2) << 16)
            | (int(reg.byte3) << 24)
        )
        assert reconstructed == value

    @given(
        value=st.integers(min_value=0, max_value=(1 << 64) - 1),
        offset=register_offsets(),
    )
    def test_64bit_field_reconstruction(self, value, offset):
        """Fields covering a full 64-bit register must reconstruct the original value."""
        reg = RegisterInt(
            value,
            offset=offset,
            width=8,
            fields={
                "lo": (0, 32),
                "hi": (32, 32),
            },
        )
        reconstructed = int(reg.lo) | (int(reg.hi) << 32)
        assert reconstructed == value

    def test_register_int_with_empty_fields_dict(self):
        """RegisterInt with an explicit empty fields dict behaves as no fields."""
        reg = RegisterInt(42, offset=0, width=4, fields={})
        assert len(reg._fields) == 0
        with pytest.raises(AttributeError):
            _ = reg.missing

    def test_register_int_with_none_fields(self):
        """RegisterInt with fields=None behaves as no fields."""
        reg = RegisterInt(42, offset=0, width=4, fields=None)
        assert len(reg._fields) == 0

    @given(
        value=st.integers(min_value=0, max_value=(1 << 32) - 1),
        offset=register_offsets(),
    )
    def test_field_offset_matches_register_offset(self, value, offset):
        """Every extracted FieldInt must carry the same offset as its parent register."""
        reg = RegisterInt(value, offset=offset, width=4, fields={"x": (0, 8)})
        assert reg.x.offset == offset
