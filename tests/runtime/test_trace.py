"""Tests for Unit 20 — recording, replay, sim, and scoped tracing.

Covers:

* :class:`RecordingMaster` capturing reads + writes.
* :class:`ReplayMaster` (strict + loose) replaying a saved log.
* :class:`ReplayMismatchError` on mis-ordered ops.
* ``soc.trace()`` capture via the ``attach_trace`` integration helper.
* :class:`SimMaster` initialising from a state dict and serving reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from peakrdl_pybind11.masters import (
    AccessOp,
    MockMaster,
    RecordingMaster,
    ReplayMaster,
    ReplayMismatchError,
    SimMaster,
)
from peakrdl_pybind11.runtime import Trace, attach_trace

# ---------------------------------------------------------------------------
# RecordingMaster
# ---------------------------------------------------------------------------


class TestRecordingMaster:
    def test_logs_reads_and_writes(self) -> None:
        inner = MockMaster()
        rec = RecordingMaster(inner)

        rec.write(0x0, 0xAA, 4)
        rec.write(0x4, 0xBB, 4)
        rec.read(0x0, 4)
        rec.read(0x4, 4)
        rec.read(0x8, 4)

        assert len(rec.events) == 5
        ops = [e["op"] for e in rec.events]
        assert ops == ["write", "write", "read", "read", "read"]
        # Recorded reads return the underlying mock's view.
        assert rec.events[2]["value"] == 0xAA
        assert rec.events[3]["value"] == 0xBB
        assert rec.events[4]["value"] == 0
        # Every event has the documented schema.
        for e in rec.events:
            for key in ("op", "address", "value", "width", "timestamp"):
                assert key in e
            assert isinstance(e["timestamp"], float)

    def test_save_writes_valid_json(self, tmp_path: Path) -> None:
        rec = RecordingMaster(MockMaster())
        for addr in (0x0, 0x4, 0x8):
            rec.write(addr, addr * 0x11, 4)
        for addr in (0x0, 0x8):
            rec.read(addr, 4)

        path = tmp_path / "trace.json"
        rec.save(path)

        loaded = json.loads(path.read_text())
        assert isinstance(loaded, list)
        assert len(loaded) == len(rec.events)
        assert loaded[0]["op"] == "write"
        assert loaded[0]["address"] == 0x0

    def test_save_ndjson_round_trips(self, tmp_path: Path) -> None:
        rec = RecordingMaster(MockMaster())
        rec.write(0x10, 0x42, 4)
        rec.read(0x10, 4)

        path = tmp_path / "trace.ndjson"
        rec.save(path)

        lines = [
            json.loads(line)
            for line in path.read_text().splitlines()
            if line.strip()
        ]
        assert len(lines) == 2
        assert lines[0]["address"] == 0x10
        assert lines[0]["op"] == "write"

    def test_streaming_file_appends_per_op(self, tmp_path: Path) -> None:
        path = tmp_path / "stream.ndjson"
        rec = RecordingMaster(MockMaster(), file=path)
        try:
            rec.write(0x100, 0xCAFE, 4)
            rec.read(0x100, 4)
        finally:
            rec.close()

        events = [
            json.loads(line)
            for line in path.read_text().splitlines()
            if line.strip()
        ]
        assert [e["op"] for e in events] == ["write", "read"]
        assert events[0]["value"] == 0xCAFE
        # Streaming events match in-memory events.
        assert events == list(rec.events)

    def test_read_many_records_each_op(self) -> None:
        inner = MockMaster()
        inner.write(0x0, 0x1, 4)
        inner.write(0x4, 0x2, 4)
        inner.write(0x8, 0x3, 4)

        rec = RecordingMaster(inner)
        ops = [AccessOp(0x0, 0, 4), AccessOp(0x4, 0, 4), AccessOp(0x8, 0, 4)]
        values = rec.read_many(ops)

        assert values == [0x1, 0x2, 0x3]
        assert len(rec.events) == 3
        assert all(e["op"] == "read" for e in rec.events)


# ---------------------------------------------------------------------------
# ReplayMaster
# ---------------------------------------------------------------------------


class TestReplayMaster:
    def _record(self, tmp_path: Path) -> Path:
        rec = RecordingMaster(MockMaster())
        rec.write(0x0, 0xDEAD, 4)
        rec.write(0x4, 0xBEEF, 4)
        rec.read(0x0, 4)
        rec.read(0x4, 4)
        path = tmp_path / "trace.json"
        rec.save(path)
        return path

    def test_replays_reads(self, tmp_path: Path) -> None:
        path = self._record(tmp_path)
        replay = ReplayMaster.from_file(path)

        replay.write(0x0, 0xDEAD, 4)
        replay.write(0x4, 0xBEEF, 4)
        assert replay.read(0x0, 4) == 0xDEAD
        assert replay.read(0x4, 4) == 0xBEEF

    def test_strict_mismatch_raises(self, tmp_path: Path) -> None:
        path = self._record(tmp_path)
        replay = ReplayMaster.from_file(path)

        # Wrong address on the first write.
        with pytest.raises(ReplayMismatchError):
            replay.write(0x100, 0xDEAD, 4)

    def test_strict_op_swap_raises(self, tmp_path: Path) -> None:
        path = self._record(tmp_path)
        replay = ReplayMaster.from_file(path)

        # Recording starts with a write; calling read first must raise.
        with pytest.raises(ReplayMismatchError):
            replay.read(0x0, 4)

    def test_strict_write_value_mismatch_raises(self, tmp_path: Path) -> None:
        path = self._record(tmp_path)
        replay = ReplayMaster.from_file(path)

        # Same op + address but different value — strict mode must catch this.
        with pytest.raises(ReplayMismatchError):
            replay.write(0x0, 0xCAFE, 4)

    def test_strict_exhaustion_raises(self, tmp_path: Path) -> None:
        path = self._record(tmp_path)
        replay = ReplayMaster.from_file(path)
        replay.write(0x0, 0xDEAD, 4)
        replay.write(0x4, 0xBEEF, 4)
        replay.read(0x0, 4)
        replay.read(0x4, 4)
        # No more events left.
        with pytest.raises(ReplayMismatchError):
            replay.read(0x0, 4)

    def test_loose_mode_serves_matching_reads(self, tmp_path: Path) -> None:
        path = self._record(tmp_path)
        replay = ReplayMaster.from_file(path, strict=False)

        # Skip the writes entirely; loose mode only cares about reads.
        assert replay.read(0x0, 4) == 0xDEAD
        assert replay.read(0x4, 4) == 0xBEEF

    def test_loose_mode_unknown_address_returns_zero(self, tmp_path: Path) -> None:
        path = self._record(tmp_path)
        replay = ReplayMaster.from_file(path, strict=False)
        # Address never read in the recording — loose mode tolerates it.
        assert replay.read(0xFFFF, 4) == 0

    def test_ndjson_round_trip(self, tmp_path: Path) -> None:
        rec = RecordingMaster(MockMaster())
        rec.write(0x10, 0x42, 4)
        rec.read(0x10, 4)
        path = tmp_path / "trace.ndjson"
        rec.save(path)

        replay = ReplayMaster.from_file(path)
        replay.write(0x10, 0x42, 4)
        assert replay.read(0x10, 4) == 0x42


# ---------------------------------------------------------------------------
# soc.trace()
# ---------------------------------------------------------------------------


class _FakeSoC:
    """Minimal stand-in for a generated SoC.

    Real SoCs are produced by the C++ binding; this tiny shim is
    enough to exercise the ``attach_trace`` integration without
    pulling cmake into the unit-test suite.
    """

    def __init__(self, master: MockMaster) -> None:
        self.master = master

    def attach_master(self, master: object) -> None:
        self.master = master  # type: ignore[assignment]


class TestSocTrace:
    def test_with_block_captures_events(self) -> None:
        soc = _FakeSoC(MockMaster())
        attach_trace(soc)

        with soc.trace() as t:
            soc.master.write(0x0, 0xAA, 4)
            soc.master.read(0x0, 4)
            soc.master.read(0x4, 4)

        assert isinstance(t, Trace)
        assert len(t) == 3
        assert t.events[0]["op"] == "write"
        assert t.events[1]["op"] == "read"
        assert t.events[1]["value"] == 0xAA
        assert t.events[2]["value"] == 0

    def test_master_restored_on_exit(self) -> None:
        original = MockMaster()
        soc = _FakeSoC(original)
        attach_trace(soc)

        with soc.trace():
            assert soc.master is not original
            assert isinstance(soc.master, RecordingMaster)
        assert soc.master is original

    def test_master_restored_on_exception(self) -> None:
        original = MockMaster()
        soc = _FakeSoC(original)
        attach_trace(soc)

        with pytest.raises(RuntimeError, match="boom"):
            with soc.trace():
                raise RuntimeError("boom")
        assert soc.master is original

    def test_save_round_trips(self, tmp_path: Path) -> None:
        soc = _FakeSoC(MockMaster())
        attach_trace(soc)

        with soc.trace() as t:
            soc.master.write(0x10, 0x42, 4)
            soc.master.read(0x10, 4)

        path = tmp_path / "session.json"
        t.save(path)

        loaded = json.loads(path.read_text())
        assert len(loaded) == 2
        assert loaded[0]["address"] == 0x10
        assert loaded[1]["op"] == "read"
        assert loaded[1]["value"] == 0x42

    def test_str_pretty_prints(self) -> None:
        soc = _FakeSoC(MockMaster())
        attach_trace(soc)

        with soc.trace() as t:
            soc.master.write(0x100, 0xABCD, 4)
            soc.master.read(0x100, 4)

        text = str(t)
        # Header line summarises transaction count and total bytes.
        assert "2 transactions" in text
        assert "8 bytes" in text
        # Each event renders one line. Address shown in 0x... form.
        assert "0x00000100" in text
        # Read line shows the value with an arrow.
        assert "rd" in text and "0x0000abcd" in text
        # Write line shows the value.
        assert "wr" in text

    def test_idempotent_attach(self) -> None:
        soc = _FakeSoC(MockMaster())
        attach_trace(soc)
        first = soc.trace
        attach_trace(soc)
        assert soc.trace is first


# ---------------------------------------------------------------------------
# SimMaster
# ---------------------------------------------------------------------------


class TestSimMaster:
    def test_initialises_from_state(self) -> None:
        sim = SimMaster({0x0: 0xAA, 0x4: 0xBB})
        assert sim.read(0x0, 4) == 0xAA
        assert sim.read(0x4, 4) == 0xBB
        # Untouched addresses return 0 (mirrors MockMaster).
        assert sim.read(0x100, 4) == 0

    def test_state_is_copied(self) -> None:
        seed = {0x0: 0xAA}
        sim = SimMaster(seed)
        seed[0x0] = 0xFF
        # Mutation of the input dict must not leak into the master.
        assert sim.read(0x0, 4) == 0xAA

    def test_writes_update_state(self) -> None:
        sim = SimMaster()
        sim.write(0x10, 0x42, 4)
        assert sim.read(0x10, 4) == 0x42

    def test_width_masking(self) -> None:
        sim = SimMaster()
        sim.write(0x0, 0xDEADBEEF, 2)
        # Only the low 2 bytes survive a 2-byte write.
        assert sim.read(0x0, 2) == 0xBEEF

    def test_read_many_and_write_many(self) -> None:
        sim = SimMaster()
        sim.write_many([
            AccessOp(0x0, 0xAA, 4),
            AccessOp(0x4, 0xBB, 4),
        ])
        values = sim.read_many([AccessOp(0x0, 0, 4), AccessOp(0x4, 0, 4)])
        assert values == [0xAA, 0xBB]

    def test_reset(self) -> None:
        sim = SimMaster({0x0: 0xAA})
        sim.reset()
        assert sim.read(0x0, 4) == 0
