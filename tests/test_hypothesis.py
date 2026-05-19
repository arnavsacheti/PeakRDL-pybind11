"""
Property-based tests using Hypothesis for PeakRDL-pybind11.
"""

import re

from hypothesis import assume, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    rule,
)

from peakrdl_pybind11 import FieldInt, RegisterInt
from peakrdl_pybind11.exporter import _RESERVED_WORDS
from peakrdl_pybind11.masters import MockMaster
from peakrdl_pybind11.masters.base import AccessOp

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Hardware registers are typically 8-64 bits wide, but FieldInt is a plain int
# subclass with no documented upper bound on lsb/width. We keep values realistic
# (up to 64-bit registers) while still exercising edge cases.
field_lsb = st.integers(min_value=0, max_value=63)
field_width = st.integers(min_value=1, max_value=64)

# Register widths used in practice (bytes): 1, 2, 4, 8
register_width_bytes = st.sampled_from([1, 2, 4, 8])


@st.composite
def non_overlapping_fields(draw: st.DrawFn) -> tuple[int, int, dict[str, tuple[int, int]]]:
    """Generate a register value, register width in bytes, and a dict of non-overlapping fields.

    Returns (value, width_bytes, fields) where fields maps name -> (lsb, bit_width).
    The fields are guaranteed not to overlap and to fit within the register width.
    """
    width_bytes = draw(register_width_bytes)
    total_bits = width_bytes * 8

    # Generate a value that fits in the register
    value = draw(st.integers(min_value=0, max_value=(1 << total_bits) - 1))

    # Partition the register into non-overlapping fields by choosing split points
    # We generate between 1 and min(6, total_bits) fields.
    max_fields = min(6, total_bits)
    num_fields = draw(st.integers(min_value=1, max_value=max_fields))

    # Choose (num_fields - 1) unique split points in [1, total_bits - 1]
    if total_bits == 1:
        # Only one possible field: bit 0 with width 1
        boundaries = [0, 1]
    else:
        splits = sorted(
            draw(
                st.lists(
                    st.integers(min_value=1, max_value=total_bits - 1),
                    min_size=num_fields - 1,
                    max_size=num_fields - 1,
                    unique=True,
                )
            )
        )
        boundaries = [0, *splits, total_bits]

    fields: dict[str, tuple[int, int]] = {}
    for i in range(len(boundaries) - 1):
        lsb = boundaries[i]
        w = boundaries[i + 1] - boundaries[i]
        fields[f"f{i}"] = (lsb, w)

    return value, width_bytes, fields


# ---------------------------------------------------------------------------
# FieldInt mask and msb mathematical invariants
# ---------------------------------------------------------------------------


class TestFieldIntProperties:
    """Property-based tests for FieldInt metadata calculations."""

    @given(
        value=st.integers(min_value=0, max_value=(1 << 64) - 1),
        lsb=st.integers(min_value=0, max_value=63),
        width=st.integers(min_value=1, max_value=64),
        offset=st.integers(min_value=0, max_value=(1 << 32) - 1),
    )
    def test_mask_has_correct_popcount(self, value: int, lsb: int, width: int, offset: int) -> None:
        """The mask should have exactly `width` bits set."""
        field = FieldInt(value, lsb=lsb, width=width, offset=offset)
        assert bin(field.mask).count("1") == width

    @given(
        value=st.integers(min_value=0, max_value=(1 << 64) - 1),
        lsb=st.integers(min_value=0, max_value=63),
        width=st.integers(min_value=1, max_value=64),
        offset=st.integers(min_value=0, max_value=(1 << 32) - 1),
    )
    def test_mask_starts_at_lsb(self, value: int, lsb: int, width: int, offset: int) -> None:
        """Shifting the mask right by `lsb` should give a contiguous run of `width` ones."""
        field = FieldInt(value, lsb=lsb, width=width, offset=offset)
        assert field.mask >> lsb == (1 << width) - 1

    @given(
        value=st.integers(min_value=0, max_value=(1 << 64) - 1),
        lsb=st.integers(min_value=0, max_value=63),
        width=st.integers(min_value=1, max_value=64),
        offset=st.integers(min_value=0, max_value=(1 << 32) - 1),
    )
    def test_msb_equals_lsb_plus_width_minus_one(self, value: int, lsb: int, width: int, offset: int) -> None:
        """msb should always equal lsb + width - 1."""
        field = FieldInt(value, lsb=lsb, width=width, offset=offset)
        assert field.msb == lsb + width - 1

    @given(
        value=st.integers(min_value=0, max_value=(1 << 64) - 1),
        lsb=st.integers(min_value=0, max_value=63),
        width=st.integers(min_value=1, max_value=64),
        offset=st.integers(min_value=0, max_value=(1 << 32) - 1),
    )
    def test_mask_no_bits_below_lsb(self, value: int, lsb: int, width: int, offset: int) -> None:
        """No bits below position `lsb` should be set in the mask."""
        field = FieldInt(value, lsb=lsb, width=width, offset=offset)
        if lsb > 0:
            assert field.mask & ((1 << lsb) - 1) == 0


# ---------------------------------------------------------------------------
# RegisterInt field extraction round-trip
# ---------------------------------------------------------------------------


class TestRegisterIntFieldExtraction:
    """Property-based tests for RegisterInt field value extraction."""

    @given(data=non_overlapping_fields())
    def test_field_values_match_bitwise_extraction(
        self, data: tuple[int, int, dict[str, tuple[int, int]]]
    ) -> None:
        """Each field's integer value should equal the bits extracted from the register value."""
        value, width_bytes, fields = data
        reg = RegisterInt(value, offset=0, width=width_bytes, fields=fields)

        for name, (lsb, field_width) in fields.items():
            expected = (value >> lsb) & ((1 << field_width) - 1)
            actual = int(getattr(reg, name))
            assert actual == expected, (
                f"Field {name}: expected {expected:#x}, got {actual:#x} "
                f"(value={value:#x}, lsb={lsb}, width={field_width})"
            )

    @given(data=non_overlapping_fields())
    def test_fields_reconstruct_register_value(
        self, data: tuple[int, int, dict[str, tuple[int, int]]]
    ) -> None:
        """Non-overlapping fields that cover all bits should reconstruct the original value."""
        value, width_bytes, fields = data
        reg = RegisterInt(value, offset=0, width=width_bytes, fields=fields)

        reconstructed = 0
        for name, (lsb, _field_width) in fields.items():
            field_val = int(getattr(reg, name))
            reconstructed |= field_val << lsb

        # Our strategy generates fields covering all bits of the register
        total_bits = width_bytes * 8
        mask = (1 << total_bits) - 1
        assert reconstructed == (value & mask)

    @given(data=non_overlapping_fields())
    def test_field_metadata_preserved(self, data: tuple[int, int, dict[str, tuple[int, int]]]) -> None:
        """Field metadata (lsb, width, offset) should be faithfully preserved."""
        value, width_bytes, fields = data
        offset = 0x4000
        reg = RegisterInt(value, offset=offset, width=width_bytes, fields=fields)

        for name, (lsb, field_width) in fields.items():
            field = getattr(reg, name)
            assert field.lsb == lsb
            assert field.width == field_width
            assert field.offset == offset


# ---------------------------------------------------------------------------
# MockMaster write/read round-trip
# ---------------------------------------------------------------------------


class TestMockMasterRoundTrip:
    """Property-based tests for MockMaster write/read round-trip."""

    @given(
        address=st.integers(min_value=0, max_value=(1 << 32) - 1),
        value=st.integers(min_value=0, max_value=(1 << 64) - 1),
        width=register_width_bytes,
    )
    def test_write_read_roundtrip(self, address: int, value: int, width: int) -> None:
        """Reading back a written value returns the value masked to the register width."""
        master = MockMaster()
        master.write(address, value, width)
        result = master.read(address, width)

        mask = (1 << (width * 8)) - 1
        assert result == value & mask

    @given(
        address=st.integers(min_value=0, max_value=(1 << 32) - 1),
        width=register_width_bytes,
    )
    def test_read_unwritten_returns_zero(self, address: int, width: int) -> None:
        """Reading an address that was never written returns 0."""
        master = MockMaster()
        assert master.read(address, width) == 0

    @given(
        addr1=st.integers(min_value=0, max_value=(1 << 16) - 1),
        addr2=st.integers(min_value=0, max_value=(1 << 16) - 1),
        val1=st.integers(min_value=0, max_value=(1 << 32) - 1),
        val2=st.integers(min_value=0, max_value=(1 << 32) - 1),
        width=register_width_bytes,
    )
    def test_writes_to_different_addresses_are_independent(
        self, addr1: int, addr2: int, val1: int, val2: int, width: int
    ) -> None:
        """Writing to one address does not affect the value at a different address."""
        from hypothesis import assume

        assume(addr1 != addr2)
        master = MockMaster()
        master.write(addr1, val1, width)
        master.write(addr2, val2, width)

        mask = (1 << (width * 8)) - 1
        assert master.read(addr1, width) == val1 & mask
        assert master.read(addr2, width) == val2 & mask


# ---------------------------------------------------------------------------
# _sanitize_identifier: valid output and idempotence
# ---------------------------------------------------------------------------


class TestSanitizeIdentifier:
    """Property-based tests for the identifier sanitization logic."""

    VALID_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

    @staticmethod
    def _sanitize(name: str) -> str:
        """Standalone reimplementation of _sanitize_identifier for testing.

        Matches the logic in Pybind11Exporter._sanitize_identifier exactly.
        """
        from peakrdl_pybind11 import Pybind11Exporter

        exporter = Pybind11Exporter()
        return exporter._sanitize_identifier(name)

    @settings(deadline=None)
    @given(name=st.text(min_size=0, max_size=200))
    def test_output_is_always_valid_identifier(self, name: str) -> None:
        """Sanitized output must always be a valid Python/C++ identifier."""
        result = self._sanitize(name)
        assert self.VALID_IDENTIFIER_RE.match(result), (
            f"Sanitize({name!r}) produced invalid identifier: {result!r}"
        )

    @given(name=st.text(min_size=0, max_size=200))
    def test_sanitize_is_idempotent(self, name: str) -> None:
        """Applying sanitize twice should give the same result as applying it once."""
        once = self._sanitize(name)
        twice = self._sanitize(once)
        assert once == twice

    @given(
        name=st.from_regex(r"[a-zA-Z_][a-zA-Z0-9_]*", fullmatch=True)
        .filter(lambda s: 1 <= len(s) <= 100)
        # The sanitizer deliberately appends `_` to Python/C++ reserved words
        # (e.g. `class`, `do`, `template`) so the generated code parses in
        # both languages. Filter those out -- this test covers the
        # non-reserved case; the reserved case is covered by
        # test_known_bugs.test_issue_34_*.
        .filter(lambda s: s not in _RESERVED_WORDS)
    )
    def test_valid_identifiers_are_unchanged(self, name: str) -> None:
        """Non-reserved valid identifiers should pass through unchanged."""
        result = self._sanitize(name)
        assert result == name


# ---------------------------------------------------------------------------
# S1 -- MockMaster random-op state machine
# ---------------------------------------------------------------------------


def _mask_for_width(width: int) -> int:
    return (1 << (width * 8)) - 1


# Address pool the state machine writes to. Reads against "never written"
# addresses come from a disjoint pool so the corresponding invariant never
# becomes vacuous as Hypothesis fills in addresses.
_WRITE_ADDR_MIN = 0
_WRITE_ADDR_MAX = 0xFFFF
_UNWRITTEN_POOL = tuple(range(0x100000, 0x100010))


class MockMasterStateMachine(RuleBasedStateMachine):
    """Random-op state machine exercising :class:`MockMaster`.

    Rules drive single and batched reads/writes; the shadow dict mirrors the
    masking semantics of ``MockMaster.write`` (mask at write time).
    Invariants check (a) every written address still reads back what the
    shadow expects (under arbitrary read widths) and (b) addresses in a
    disjoint pool that the test never touches still read zero.
    """

    written_addrs: Bundle[int] = Bundle("written_addrs")

    def __init__(self) -> None:
        super().__init__()
        self.master = MockMaster()
        # shadow[addr] = value-masked-at-write-time (mirrors MockMaster)
        self.shadow: dict[int, int] = {}

    # ---- single-op rules ------------------------------------------------

    @rule(
        target=written_addrs,
        addr=st.integers(min_value=_WRITE_ADDR_MIN, max_value=_WRITE_ADDR_MAX),
        val=st.integers(min_value=0, max_value=(1 << 32) - 1),
        width=register_width_bytes,
    )
    def do_write(self, addr: int, val: int, width: int) -> int:
        masked = val & _mask_for_width(width)
        self.master.write(addr, val, width)
        self.shadow[addr] = masked
        return addr

    @rule(
        addr=st.integers(min_value=_WRITE_ADDR_MIN, max_value=_WRITE_ADDR_MAX),
        width=register_width_bytes,
    )
    def do_read(self, addr: int, width: int) -> None:
        got = self.master.read(addr, width)
        expected = self.shadow.get(addr, 0) & _mask_for_width(width)
        assert got == expected, f"read({addr:#x}, {width}) -> {got:#x}; expected {expected:#x}"

    # Re-read addresses we previously wrote -- catches "cache corruption"
    # style regressions where an unrelated op clobbers a stored value.
    @rule(addr=written_addrs, width=register_width_bytes)
    def reread_written(self, addr: int, width: int) -> None:
        got = self.master.read(addr, width)
        expected = self.shadow.get(addr, 0) & _mask_for_width(width)
        assert got == expected, f"reread({addr:#x}, {width}) -> {got:#x}; expected {expected:#x}"

    # ---- batched-op rules ----------------------------------------------

    @rule(
        ops=st.lists(
            st.tuples(
                st.integers(min_value=_WRITE_ADDR_MIN, max_value=_WRITE_ADDR_MAX),
                st.integers(min_value=0, max_value=(1 << 32) - 1),
                register_width_bytes,
            ),
            min_size=1,
            max_size=5,
        )
    )
    def do_write_many(self, ops: list[tuple[int, int, int]]) -> None:
        access_ops = [AccessOp(address=a, value=v, width=w) for a, v, w in ops]
        self.master.write_many(access_ops)
        for a, v, w in ops:
            self.shadow[a] = v & _mask_for_width(w)

    @rule(
        ops=st.lists(
            st.tuples(
                st.integers(min_value=_WRITE_ADDR_MIN, max_value=_WRITE_ADDR_MAX),
                register_width_bytes,
            ),
            min_size=1,
            max_size=5,
        )
    )
    def do_read_many(self, ops: list[tuple[int, int]]) -> None:
        access_ops = [AccessOp(address=a, width=w) for a, w in ops]
        got_list = self.master.read_many(access_ops)
        assert len(got_list) == len(ops)
        for (a, w), got in zip(ops, got_list, strict=True):
            expected = self.shadow.get(a, 0) & _mask_for_width(w)
            assert got == expected, f"read_many[{a:#x},{w}] -> {got:#x}; expected {expected:#x}"

    # ---- invariants -----------------------------------------------------

    @invariant()
    def shadow_matches_master_for_written_addrs(self) -> None:
        """Every written address still reads back the shadow value (masked)."""
        # Use width=4 as the canonical full-width read (covers all 32-bit
        # values written by the rules). The width-vs-write-width interaction
        # is exercised by do_read/do_read_many themselves.
        for addr, stored in self.shadow.items():
            got = self.master.read(addr, 4)
            expected = stored & _mask_for_width(4)
            assert got == expected, f"invariant: read({addr:#x}, 4) -> {got:#x}; expected {expected:#x}"

    @invariant()
    def unwritten_pool_reads_zero(self) -> None:
        """A disjoint pool the test never touches always reads zero."""
        for addr in _UNWRITTEN_POOL:
            for w in (1, 2, 4, 8):
                got = self.master.read(addr, w)
                assert got == 0, f"invariant: unwritten read({addr:#x}, {w}) -> {got:#x}; expected 0"


MockMasterStateMachine.TestCase.settings = settings(
    max_examples=200,
    stateful_step_count=50,
    deadline=None,
)
TestMockMasterStateMachine = MockMasterStateMachine.TestCase


# ---------------------------------------------------------------------------
# P5 -- _sanitize_identifier injectivity property (Shape 1)
# ---------------------------------------------------------------------------


# Historical collision under the alphabet {L, N, _}:
#   _sanitize_identifier("class")  -> "class_"
#   _sanitize_identifier("class_") -> "class_"
# Both inputs are valid Python identifiers (string-shape-wise; "class" is a
# valid IDENTIFIER even though it's a keyword), so the sanitizer was not
# injective on this space. The collision was induced by the reserved-word
# trailing underscore: any reserved word `R` collided with `R + "_"`.
#
# Fixed by switching the reserved-word disambiguator from "_" to "_kw"
# (no Python/C++ keyword ends in "_kw", so `keyword` -> `keyword_kw` cannot
# itself coincide with a stem-keyword output). The strategy is scoped to
# the ASCII alphabet the sanitizer actually targets — non-ASCII letters
# collapse to `_` by design (the regex `[a-zA-Z0-9_]` filters them out),
# which is a separate property orthogonal to the keyword-disambiguation
# fix exercised here.
class TestSanitizeInjectivity:
    """Bounded injectivity over the valid-identifier alphabet."""

    @staticmethod
    def _sanitize(name: str) -> str:
        from peakrdl_pybind11 import Pybind11Exporter

        return Pybind11Exporter()._sanitize_identifier(name)

    @settings(deadline=None, max_examples=300)
    @given(
        a=st.from_regex(r"[a-zA-Z_][a-zA-Z0-9_]*", fullmatch=True).filter(lambda s: 1 <= len(s) <= 20),
        b=st.from_regex(r"[a-zA-Z_][a-zA-Z0-9_]*", fullmatch=True).filter(lambda s: 1 <= len(s) <= 20),
    )
    def test_sanitize_injective_on_valid_identifiers(self, a: str, b: str) -> None:
        """Two distinct valid-identifier inputs must not collide."""
        assume(a != b)
        assert self._sanitize(a) != self._sanitize(b), (
            f"Collision: sanitize({a!r}) == sanitize({b!r}) == {self._sanitize(a)!r}"
        )
