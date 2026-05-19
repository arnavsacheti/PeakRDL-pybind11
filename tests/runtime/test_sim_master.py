"""Tests for :class:`peakrdl_pybind11.masters.sim.SimMaster`.

These are pure-Python: every register / field / SoC stand-in is a
``SimpleNamespace`` (mirroring the pattern in
``tests/runtime/test_side_effects.py``). The SoC fakes expose a
``walk(kind=...)`` method that yields registers in insertion order; each
register has an ``info`` with ``address`` and ``regwidth`` plus field
instances attached as attributes, each with its own ``info``
(``on_read`` / ``on_write`` / ``singlepulse`` / ``lsb`` / ``width``).

Coverage of the five modelled side-effect families:

* ``rclr``  -- destructive read returns pre-read value, clears storage.
* ``rset``  -- destructive read returns pre-read value, sets storage.
* ``woclr`` / ``wclr`` / ``wzc`` -- per-bit clear semantics.
* ``woset`` / ``wset`` / ``wzs`` -- per-bit set semantics.
* ``singlepulse`` -- post-write self-clear so the next read sees zero.

Plus regression cases for back-compat, attach-after-construction,
unmodelled-but-recognised tokens (``ruser`` / ``wuser`` / sticky),
multi-field compound registers, and pure pass-through registers.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from peakrdl_pybind11.masters import AccessOp
from peakrdl_pybind11.masters.sim import SimMaster


# ---------------------------------------------------------------------------
# Fake SoC / register / field builders.
# ---------------------------------------------------------------------------


def _make_field(
    *,
    name: str = "f",
    lsb: int = 0,
    width: int = 8,
    on_read: str | None = None,
    on_write: str | None = None,
    singlepulse: bool = False,
) -> SimpleNamespace:
    """Build a field instance with the minimum metadata the simulator needs."""
    info = SimpleNamespace(
        name=name,
        lsb=lsb,
        width=width,
        on_read=on_read,
        on_write=on_write,
        singlepulse=singlepulse,
    )
    return SimpleNamespace(info=info, name=name)


def _make_register(
    *,
    address: int,
    regwidth: int = 32,
    fields: dict[str, SimpleNamespace] | None = None,
) -> SimpleNamespace:
    """Build a register instance with an ``info`` and field-instance attrs.

    The register is callable for ``read``/``write`` so the duck-typed
    ``_looks_like_register`` check in the model builder accepts it.
    """
    reg = SimpleNamespace(
        info=SimpleNamespace(address=address, regwidth=regwidth),
        read=lambda: 0,
        write=lambda v: None,
    )
    if fields:
        for fname, field_obj in fields.items():
            setattr(reg, fname, field_obj)
    return reg


def _make_soc(*registers: SimpleNamespace) -> SimpleNamespace:
    """Build an SoC whose ``walk(kind=...)`` returns the given registers."""

    def walk(kind: str | None = None) -> list[SimpleNamespace]:
        # We ignore ``kind`` -- the model builder filters via
        # ``_looks_like_register`` anyway. Returning the same list for
        # any kind makes the fake simpler to reason about.
        return list(registers)

    return SimpleNamespace(walk=walk)


# ---------------------------------------------------------------------------
# Read-side effects: rclr, rset
# ---------------------------------------------------------------------------


class TestReadSideEffects:
    def test_rclr_first_read_returns_pre_value_then_clears(self) -> None:
        field = _make_field(lsb=0, width=16, on_read="rclr")
        reg = _make_register(address=0x100, fields={"data": field})
        master = SimMaster(soc=_make_soc(reg))
        master.memory[0x100] = 0xABCD

        # First read returns the pre-read value.
        assert master.read(0x100, 4) == 0xABCD
        # Storage has cleared the field's bits.
        assert master.memory[0x100] == 0x0000
        # Second read returns 0.
        assert master.read(0x100, 4) == 0x0000

    def test_rset_first_read_returns_pre_value_then_sets(self) -> None:
        field = _make_field(lsb=0, width=8, on_read="rset")
        reg = _make_register(address=0x200, fields={"data": field})
        master = SimMaster(soc=_make_soc(reg))
        master.memory[0x200] = 0x00

        assert master.read(0x200, 4) == 0x00
        # Storage now has the field's bits all-1s.
        assert master.memory[0x200] == 0xFF
        # The next read returns the all-1s mask.
        assert master.read(0x200, 4) == 0xFF


# ---------------------------------------------------------------------------
# Write-side effects: woclr / woset / wzc / wzs
# ---------------------------------------------------------------------------


# Map each on_write token to its expected formula on a 32-bit field
# (lsb=0, width=32). ``prior`` is the current storage; ``written`` is the
# value the caller asked to write; the lambda returns the expected new
# storage. All formulas are masked to 32 bits because Python's ``~`` is
# unbounded -- the simulator's _apply_write_token clamps to the field
# width, which for width=32 is 0xFFFFFFFF.
#
# Semantics confirmed against the prior hand-written tests at this
# location (woclr/wclr/woset/wset/wzc/wzs) and against
# ``_apply_write_token`` in src/peakrdl_pybind11/masters/_side_effect_model.py.
_WRITE_TOKEN_FORMULA = {
    "woclr": lambda prior, written: (prior & ~written) & 0xFFFFFFFF,
    "wclr": lambda prior, written: (prior & ~written) & 0xFFFFFFFF,
    "wzc": lambda prior, written: (prior & written) & 0xFFFFFFFF,
    "woset": lambda prior, written: (prior | written) & 0xFFFFFFFF,
    "wset": lambda prior, written: (prior | written) & 0xFFFFFFFF,
    "wzs": lambda prior, written: (prior | ((~written) & 0xFFFFFFFF)) & 0xFFFFFFFF,
}


class TestWriteSideEffects:
    """Consolidated property-based coverage of the six per-bit
    write-side-effect tokens.

    Replaced six hand-picked tests (one per token) with a single
    ``@given`` test sampling over the token plus randomised prior /
    written values. Each token's formula was cross-checked against the
    formerly-individual unit tests and against
    ``_apply_write_token``."""

    @given(
        token=st.sampled_from(list(_WRITE_TOKEN_FORMULA.keys())),
        prior=st.integers(min_value=0, max_value=2**32 - 1),
        written=st.integers(min_value=0, max_value=2**32 - 1),
    )
    def test_write_side_effect_matches_token_formula(
        self, token: str, prior: int, written: int
    ) -> None:
        field = _make_field(lsb=0, width=32, on_write=token, name="flags")
        # Use a *unique* address per call -- Hypothesis re-runs the body
        # many times within a single test; sharing a master across runs
        # would let prior state leak. SimMaster construction is cheap.
        reg = _make_register(address=0x300, fields={"flags": field})
        master = SimMaster(soc=_make_soc(reg))
        master.memory[0x300] = prior

        master.write(0x300, written, 4)

        expected = _WRITE_TOKEN_FORMULA[token](prior, written)
        assert master.memory[0x300] == expected, (
            f"token={token} prior={prior:#010x} written={written:#010x} "
            f"expected={expected:#010x} got={master.memory[0x300]:#010x}"
        )


# ---------------------------------------------------------------------------
# Singlepulse
# ---------------------------------------------------------------------------


class TestSinglepulse:
    def test_singlepulse_self_clears_after_write(self) -> None:
        field = _make_field(lsb=0, width=1, singlepulse=True)
        reg = _make_register(address=0x400, fields={"trigger": field})
        master = SimMaster(soc=_make_soc(reg))

        master.write(0x400, 0x1, 4)
        # Storage is back to zero immediately after the write.
        assert master.memory[0x400] == 0x0
        # And a subsequent read confirms the bit is low.
        assert master.read(0x400, 4) == 0x0

    def test_singlepulse_clears_only_its_own_bits(self) -> None:
        """A singlepulse field doesn't disturb neighbouring bits."""
        pulse_field = _make_field(lsb=0, width=1, singlepulse=True, name="go")
        # A second field with no side effect, occupying bits [7:1].
        data_field = _make_field(lsb=1, width=7, name="data")
        reg = _make_register(
            address=0x410,
            fields={"go": pulse_field, "data": data_field},
        )
        master = SimMaster(soc=_make_soc(reg))

        master.write(0x410, 0b1010_1011, 4)
        # bit0 (pulse) clears; bits [7:1] keep their written value (0101_0101 == 0x55).
        assert master.memory[0x410] == 0b1010_1010


# ---------------------------------------------------------------------------
# Compound register: multiple side-effecting fields + unrelated bits.
# ---------------------------------------------------------------------------


class TestCompoundRegister:
    def test_mixed_fields_each_get_their_own_rule(self) -> None:
        """One register with rclr, woclr, woset, and an untouched plain
        field. Verify each obeys its own rule and no rule disturbs the
        plain field."""
        rclr_field = _make_field(lsb=0, width=4, on_read="rclr", name="rclr_lo")
        woclr_field = _make_field(lsb=4, width=4, on_write="woclr", name="w1c_hi")
        woset_field = _make_field(lsb=8, width=4, on_write="woset", name="w1s_b8")
        plain_field = _make_field(lsb=12, width=4, name="plain")
        reg = _make_register(
            address=0x500,
            fields={
                "rclr_lo": rclr_field,
                "w1c_hi": woclr_field,
                "w1s_b8": woset_field,
                "plain": plain_field,
            },
        )
        master = SimMaster(soc=_make_soc(reg))
        # Pre-load: rclr_lo=0xA, w1c_hi=0xF, w1s_b8=0x0, plain=0x3.
        master.memory[0x500] = (0x3 << 12) | (0x0 << 8) | (0xF << 4) | 0xA

        # Write: clear all four w1c_hi bits; set lower two w1s_b8 bits;
        # write 0xC into plain (replaces); rclr_lo bits in `value` are
        # ignored at write time because rclr is a read-side effect.
        write_value = (0xC << 12) | (0x3 << 8) | (0xF << 4) | 0x0
        master.write(0x500, write_value, 4)

        stored = master.memory[0x500]
        # rclr_lo: no on_write, plain replacement -> takes `0x0` from write_value.
        assert (stored >> 0) & 0xF == 0x0
        # w1c_hi: 0xF & ~0xF = 0x0.
        assert (stored >> 4) & 0xF == 0x0
        # w1s_b8: 0x0 | 0x3 = 0x3.
        assert (stored >> 8) & 0xF == 0x3
        # plain: replaced with 0xC.
        assert (stored >> 12) & 0xF == 0xC

        # Now exercise the read-side effect: the rclr_lo bits should be
        # cleared after the first read. Pre-set them again.
        master.memory[0x500] = (0xC << 12) | (0x3 << 8) | (0x0 << 4) | 0xA
        returned = master.read(0x500, 4)
        # Returned value is the pre-read storage; rclr_lo reads as 0xA.
        assert returned & 0xF == 0xA
        # Storage afterwards has rclr_lo cleared; everyone else
        # untouched.
        post = master.memory[0x500]
        assert post & 0xF == 0x0
        assert (post >> 12) & 0xF == 0xC
        assert (post >> 8) & 0xF == 0x3
        assert (post >> 4) & 0xF == 0x0


# ---------------------------------------------------------------------------
# Back-compat: behaves like MockMaster when no SoC / no side effects.
# ---------------------------------------------------------------------------


class TestBackCompat:
    def test_no_soc_behaves_like_mock_master(self) -> None:
        master = SimMaster()
        master.write(0x10, 0xDEADBEEF, 4)
        assert master.read(0x10, 4) == 0xDEADBEEF
        # Second read returns same value (no rclr in play).
        assert master.read(0x10, 4) == 0xDEADBEEF

    def test_state_seed_preserves_values(self) -> None:
        master = SimMaster({0x4: 0x1234})
        assert master.read(0x4, 4) == 0x1234

    def test_attach_soc_switches_to_side_effect_mode(self) -> None:
        """A SimMaster built without ``soc=`` is a plain MockMaster
        until ``attach_soc`` is called."""
        field = _make_field(lsb=0, width=16, on_read="rclr")
        reg = _make_register(address=0x800, fields={"data": field})

        master = SimMaster()
        master.memory[0x800] = 0xABCD
        # No SoC yet: rclr is *not* honoured.
        assert master.read(0x800, 4) == 0xABCD
        assert master.read(0x800, 4) == 0xABCD
        # Wire up the SoC and retry.
        master.attach_soc(_make_soc(reg))
        master.memory[0x800] = 0xABCD  # restore
        assert master.read(0x800, 4) == 0xABCD
        assert master.read(0x800, 4) == 0x0000

    def test_register_with_no_side_effects_is_pass_through(self) -> None:
        plain_field = _make_field(lsb=0, width=8, name="plain")
        reg = _make_register(address=0x900, fields={"plain": plain_field})
        master = SimMaster(soc=_make_soc(reg))

        master.write(0x900, 0x42, 4)
        # No side effects, so plain write.
        assert master.read(0x900, 4) == 0x42
        # The plain register isn't even in the model map.
        assert 0x900 not in master._models


# ---------------------------------------------------------------------------
# Untreated tokens (sticky / hwclr / hwset / ruser / wuser) must not crash.
# ---------------------------------------------------------------------------


class TestUntreatedTokensArePassThrough:
    """``sticky`` / ``hwclr`` / ``hwset`` / ``ruser`` / ``wuser`` are
    explicitly **not yet implemented**. They appear in this test as a
    reminder for the follow-up: today they are silently treated as
    pass-through (no transformation applied) and accesses must succeed."""

    def test_ruser_token_is_pass_through(self) -> None:
        field = _make_field(lsb=0, width=8, on_read="ruser")
        reg = _make_register(address=0xA00, fields={"data": field})
        master = SimMaster(soc=_make_soc(reg))
        master.memory[0xA00] = 0x55
        # First read returns the stored value...
        assert master.read(0xA00, 4) == 0x55
        # ...and storage is unchanged (no rclr/rset transformation).
        assert master.memory[0xA00] == 0x55

    def test_wuser_token_is_pass_through(self) -> None:
        field = _make_field(lsb=0, width=8, on_write="wuser")
        reg = _make_register(address=0xA10, fields={"data": field})
        master = SimMaster(soc=_make_soc(reg))
        master.memory[0xA10] = 0xAA

        master.write(0xA10, 0x33, 4)
        # No transformation -> standard write semantics.
        assert master.memory[0xA10] == 0x33

    def test_sticky_field_metadata_does_not_crash(self) -> None:
        """A sticky field has no ``on_read``/``on_write``/``singlepulse``
        token in this build -- the simulator simply doesn't model it and
        accesses pass through."""
        # Build the info manually so we can include a sticky-like flag
        # the simulator should ignore.
        info = SimpleNamespace(
            name="event",
            lsb=0,
            width=8,
            on_read=None,
            on_write=None,
            singlepulse=False,
            sticky=True,  # extra attr; ignored.
        )
        field = SimpleNamespace(info=info, name="event")
        reg = _make_register(address=0xA20, fields={"event": field})
        master = SimMaster(soc=_make_soc(reg))

        master.write(0xA20, 0x77, 4)
        assert master.read(0xA20, 4) == 0x77


# ---------------------------------------------------------------------------
# Batched access path runs the per-op side-effect transformation.
# ---------------------------------------------------------------------------


class TestBatchedOps:
    def test_read_many_honours_rclr(self) -> None:
        field = _make_field(lsb=0, width=16, on_read="rclr")
        reg = _make_register(address=0xB00, fields={"data": field})
        master = SimMaster(soc=_make_soc(reg))
        master.memory[0xB00] = 0xFEED

        # Two reads of the same address: first sees 0xFEED, second 0.
        ops = [AccessOp(address=0xB00, width=4), AccessOp(address=0xB00, width=4)]
        results = master.read_many(ops)
        assert results == [0xFEED, 0x0000]

    def test_write_many_honours_singlepulse(self) -> None:
        field = _make_field(lsb=0, width=1, singlepulse=True)
        reg = _make_register(address=0xB10, fields={"go": field})
        master = SimMaster(soc=_make_soc(reg))

        master.write_many([AccessOp(address=0xB10, value=1, width=4)])
        assert master.memory[0xB10] == 0


# ---------------------------------------------------------------------------
# Fallback walk: SoC without an explicit ``walk()`` method.
# ---------------------------------------------------------------------------


def test_build_models_falls_back_to_duck_walk() -> None:
    """When the SoC doesn't expose ``walk()``, the model builder
    falls back to a duck-typed traversal over ``vars()``."""
    field = _make_field(lsb=0, width=8, on_read="rclr")
    reg = _make_register(address=0xC00, fields={"data": field})
    # SoC fake without a walk method -- just the register as an attribute.
    soc = SimpleNamespace(uart=reg)
    master = SimMaster(soc=soc)
    master.memory[0xC00] = 0x42

    assert master.read(0xC00, 4) == 0x42
    assert master.read(0xC00, 4) == 0x00


# ---------------------------------------------------------------------------
# AccessMode enum / case-insensitive token handling.
# ---------------------------------------------------------------------------


def test_token_case_insensitive() -> None:
    """Uppercase ``RCLR`` should normalise to ``rclr``."""

    class FakeEnum:
        value = "RCLR"

    field_info = SimpleNamespace(
        name="data",
        lsb=0,
        width=8,
        on_read=FakeEnum(),
        on_write=None,
        singlepulse=False,
    )
    field: Any = SimpleNamespace(info=field_info, name="data")
    reg = _make_register(address=0xD00)
    setattr(reg, "data", field)
    master = SimMaster(soc=_make_soc(reg))
    master.memory[0xD00] = 0x99

    assert master.read(0xD00, 4) == 0x99
    assert master.memory[0xD00] == 0
