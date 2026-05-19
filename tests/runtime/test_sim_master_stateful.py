"""Stateful (RuleBasedStateMachine) coverage for :class:`SimMaster`.

Goal: exercise *sequences* of reads and writes against a single register
whose four fields each carry a different side-effect token. The unit
tests in ``test_sim_master.py`` only ever check one operation at a time;
this machine probes how operations *compose* -- the interesting bugs
(if any) live in the interaction.

Register layout (single 32-bit register at address ``REG_ADDR``):

* bits  [7: 0] -- field ``rclr_a``    -- ``on_read = rclr``
* bits [15: 8] -- field ``woclr_b``   -- ``on_write = woclr``
* bits [23:16] -- field ``pulse_c``   -- ``singlepulse`` (width 8)
* bits [31:24] -- field ``plain_d``   -- no side effects

The shadow state mirrors :func:`RegisterSideEffectModel.apply_read` /
``apply_write`` *step for step* (see notes in
``src/peakrdl_pybind11/masters/_side_effect_model.py``); inventing a
"cleaner" model here would create false failures.

``SimMaster`` exposes no per-field read/write entry points (every access
runs through ``master.read(addr, width)`` / ``master.write(addr, value,
width)`` which fans out across **all** fields' effects). The brief
suggested per-field rules anyway -- we instead bias the random ``write``
value strategy toward edge patterns (0, all-1s, low/high nibbles) so the
machine still hits per-field-style transitions.

Settings: ``max_examples=100`` / ``stateful_step_count=30`` per the
brief; bump down if hot.
"""

from __future__ import annotations

from types import SimpleNamespace

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from peakrdl_pybind11.masters.sim import SimMaster

# ---------------------------------------------------------------------------
# Mock-SoC builders (copied from test_sim_master.py to avoid cross-file
# imports; same shape as the SimpleNamespace pattern used everywhere in
# tests/runtime/).
# ---------------------------------------------------------------------------


REG_ADDR = 0x100
REG_WIDTH_BYTES = 4
MASK32 = 0xFFFFFFFF


def _make_field(
    *,
    name: str,
    lsb: int,
    width: int,
    on_read: str | None = None,
    on_write: str | None = None,
    singlepulse: bool = False,
) -> SimpleNamespace:
    info = SimpleNamespace(
        name=name,
        lsb=lsb,
        width=width,
        on_read=on_read,
        on_write=on_write,
        singlepulse=singlepulse,
    )
    return SimpleNamespace(info=info, name=name)


def _make_register() -> SimpleNamespace:
    """Build the 4-field test register described in the module docstring."""
    rclr_a = _make_field(name="rclr_a", lsb=0, width=8, on_read="rclr")
    woclr_b = _make_field(name="woclr_b", lsb=8, width=8, on_write="woclr")
    pulse_c = _make_field(name="pulse_c", lsb=16, width=8, singlepulse=True)
    plain_d = _make_field(name="plain_d", lsb=24, width=8)
    reg = SimpleNamespace(
        info=SimpleNamespace(address=REG_ADDR, regwidth=32),
        read=lambda: 0,
        write=lambda v: None,
    )
    for fname, fobj in (
        ("rclr_a", rclr_a),
        ("woclr_b", woclr_b),
        ("pulse_c", pulse_c),
        ("plain_d", plain_d),
    ):
        setattr(reg, fname, fobj)
    return reg


def _make_soc(reg: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(walk=lambda kind=None: [reg])


# ---------------------------------------------------------------------------
# Shadow-state transitions (must mirror _side_effect_model.py exactly).
# ---------------------------------------------------------------------------

# Per-field masks. Width 8 each, packed into a 32-bit register.
_FIELDS = (
    # (name, lsb, width, on_read, on_write, singlepulse)
    ("rclr_a", 0, 8, "rclr", None, False),
    ("woclr_b", 8, 8, None, "woclr", False),
    ("pulse_c", 16, 8, None, None, True),
    ("plain_d", 24, 8, None, None, False),
)


def _field_mask(lsb: int, width: int) -> int:
    return ((1 << width) - 1) << lsb


def _apply_read_shadow(storage: int) -> tuple[int, int]:
    """Return ``(returned_value, new_storage)`` for the shadow.

    Mirrors :meth:`RegisterSideEffectModel.apply_read`: iterate fields in
    declared order, and let ``rclr`` clear its mask while ``rset`` would
    set it. Only ``rclr`` is present in this test.
    """
    returned = storage & MASK32
    new = storage & MASK32
    for _name, lsb, width, on_read, _on_write, _sp in _FIELDS:
        if on_read == "rclr":
            new &= ~_field_mask(lsb, width)
        elif on_read == "rset":
            new |= _field_mask(lsb, width)
    return returned, new & MASK32


def _apply_write_shadow(storage: int, written: int) -> int:
    """Return new storage after a whole-register write.

    Mirrors :func:`_compose_write` + the singlepulse post-pass exactly:

    1. Start from ``written``.
    2. For each modelled field (anything with on_read / on_write /
       singlepulse), carve out its mask and substitute the field's
       transformed bits computed from the *prior* storage.
    3. Clear all singlepulse fields' bits.
    """
    storage &= MASK32
    written &= MASK32
    new = written
    singlepulse_mask = 0
    for _name, lsb, width, on_read, on_write, sp in _FIELDS:
        if on_read is None and on_write is None and not sp:
            # Pass-through field -- contributes nothing to the model; its
            # bits are already in ``new`` from the ``written`` baseline.
            continue
        fmask = _field_mask(lsb, width)
        if fmask == 0:
            continue
        if sp:
            singlepulse_mask |= fmask
        # Compute the field's transformed bits.
        slice_written = (written >> lsb) & ((1 << width) - 1)
        current = (storage >> lsb) & ((1 << width) - 1)
        if on_write is None:
            transformed = slice_written
        elif on_write in ("woclr", "wclr"):
            transformed = current & ~slice_written
        elif on_write == "wzc":
            zero_bits = (~slice_written) & ((1 << width) - 1)
            transformed = current & ~zero_bits
        elif on_write in ("woset", "wset"):
            transformed = current | slice_written
        elif on_write == "wzs":
            zero_bits = (~slice_written) & ((1 << width) - 1)
            transformed = current | zero_bits
        else:
            # ``wuser`` and anything unknown -> pass-through.
            transformed = slice_written
        transformed &= (1 << width) - 1
        new = (new & ~fmask) | (transformed << lsb)
    if singlepulse_mask:
        new &= ~singlepulse_mask
    return new & MASK32


# ---------------------------------------------------------------------------
# Random-value strategy
# ---------------------------------------------------------------------------

# Edge patterns Hypothesis loves: 0, all-1s, low/high nibbles, alternating bits.
_EDGE_PATTERNS = [
    0x00000000,
    0xFFFFFFFF,
    0x0000FFFF,
    0xFFFF0000,
    0x00FF00FF,
    0xFF00FF00,
    0xAAAAAAAA,
    0x55555555,
    # Per-field-shaped patterns (each field gets all-1s alone)
    0x000000FF,  # rclr_a
    0x0000FF00,  # woclr_b
    0x00FF0000,  # pulse_c
    0xFF000000,  # plain_d
]

_write_value = st.one_of(
    st.sampled_from(_EDGE_PATTERNS),
    st.integers(min_value=0, max_value=MASK32),
)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class SimMasterSideEffectMachine(RuleBasedStateMachine):
    """Random sequences of reads / writes against the 4-field register.

    Invariant: after every rule, the master's storage at ``REG_ADDR``
    matches the shadow. The shadow is recomputed by hand using the same
    field-iteration order as ``RegisterSideEffectModel`` so the two are
    bit-for-bit comparable.
    """

    def __init__(self) -> None:
        super().__init__()
        self.master = SimMaster(soc=_make_soc(_make_register()))
        # Shadow state -- a plain 32-bit int -- mirrors master.memory[REG_ADDR].
        self.shadow = 0
        self.master.memory[REG_ADDR] = 0

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------
    @rule()
    def do_read(self) -> None:
        returned = self.master.read(REG_ADDR, REG_WIDTH_BYTES)
        expected_returned, new_shadow = _apply_read_shadow(self.shadow)
        assert returned == expected_returned, (
            f"read returned {returned:#010x}, shadow expected "
            f"{expected_returned:#010x}; pre-read shadow={self.shadow:#010x}"
        )
        self.shadow = new_shadow

    @rule(value=_write_value)
    def do_write(self, value: int) -> None:
        self.master.write(REG_ADDR, value, REG_WIDTH_BYTES)
        self.shadow = _apply_write_shadow(self.shadow, value)

    # ------------------------------------------------------------------
    # Invariant
    # ------------------------------------------------------------------
    @invariant()
    def shadow_matches_master(self) -> None:
        actual = self.master.memory.get(REG_ADDR, 0) & MASK32
        assert actual == self.shadow, (
            f"shadow mismatch: master={actual:#010x} shadow={self.shadow:#010x}"
        )


# Per the brief: ``max_examples=100``, ``stateful_step_count=30``.
SimMasterSideEffectMachine.TestCase.settings = settings(
    max_examples=100,
    stateful_step_count=30,
    deadline=None,
)

TestSimMasterSideEffects = SimMasterSideEffectMachine.TestCase
