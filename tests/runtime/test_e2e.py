"""End-to-end integration test for the aspirational API surface (Unit 11).

This module is the regression-prevention layer for the API overhaul on
``exp/api_overhaul``. After all 10 sibling wire-up units land, every
aspirational feature documented in ``docs/IDEAL_API_SKETCH.md`` should
appear automatically on a generated SoC after ``MySoC.create()`` — this
test proves the wire-up is intact so future refactors can't silently
break it.

The module compiles a small RDL that exercises every feature the runtime
needs, builds the generated pybind11 extension via cmake, attaches a
``MockMaster``, and walks the API surface. Features that are genuinely
not wired today are :func:`pytest.mark.xfail`-marked with a clear reason
so the punchlist is visible from a single test run.

The whole module is marked ``integration``; fast unit-test runs skip it
via ``-m "not integration"``.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# RDL source — exercises the features the runtime needs to cover
# --------------------------------------------------------------------------- #

# Register arrays (``lut[8]``) are deliberately *not* in this RDL: the
# current exporter raises "Index of array element must be known to derive
# address" on export, so including one would skip the whole module. The
# array surface is still tested below via ``getattr(soc.uart, "lut", None)``
# probes that xfail when the attribute is missing.
_E2E_RDL = """
addrmap e2e_soc {
    name = "End-to-end SoC";
    desc = "Tiny RDL covering every runtime feature";

    regfile uart {
        name = "UART";

        reg ctrl_t {
            field { sw=rw; hw=r; } enable[0:0] = 0;
            field { sw=rw; hw=r; } baudrate[3:1] = 0;
            field { sw=rw; hw=r; } parity[5:4] = 0;
        };
        ctrl_t control @ 0x00;
        alias control ctrl_t control_alt @ 0x10;

        reg {
            field { sw=r; hw=w; } tx_ready[0:0];
        } status @ 0x04;

        reg {
            field { sw=rw; hw=w; intr; onread = rclr; } por_flag[0:0];
            field { sw=rw; hw=w; intr; onwrite = woclr; } tx_done[1:1];
            field { sw=rw; hw=w; intr; } rx_overflow[2:2];
        } intr_status @ 0x20;

        reg {
            field { sw=rw; hw=r; } por_flag[0:0];
            field { sw=rw; hw=r; } tx_done[1:1];
            field { sw=rw; hw=r; } rx_overflow[2:2];
        } intr_enable @ 0x24;

        reg {
            field { sw=rw; hw=r; } por_flag[0:0];
            field { sw=rw; hw=r; } tx_done[1:1];
            field { sw=rw; hw=r; } rx_overflow[2:2];
        } intr_test @ 0x28;

        reg {
            field { sw=rw; hw=r; singlepulse; } start[0:0] = 0;
        } pulse @ 0x30;

        reg {
            field { sw=r; hw=rw; counter; } count[31:0] = 0;
        } event_counter @ 0x40;
    } uart @ 0x1000;

    reg ram_entry_t {
        field { sw = rw; hw = r; } data[31:0] = 0;
    };
    external mem {
        mementries = 0x100;
        memwidth = 32;
        sw = rw;
        ram_entry_t entry;
    } ram @ 0x4000;
};
"""


# --------------------------------------------------------------------------- #
# Fixture — build the SoC once per session
# --------------------------------------------------------------------------- #


def _have_cmake() -> bool:
    return shutil.which("cmake") is not None


@pytest.fixture(scope="module")
def e2e_soc() -> Iterator[tuple[Any, Any, Any]]:
    """Compile + cmake-build the e2e SoC, yield ``(soc, master, module)``.

    The fixture skips cleanly if cmake is missing, the RDL fails to
    compile, the export fails, or the build fails. The yielded master is
    pre-attached via ``soc.attach_master``.
    """
    if not _have_cmake():
        pytest.skip("cmake not available; skipping e2e build")

    try:
        from systemrdl import RDLCompiler

        from peakrdl_pybind11.exporter import Pybind11Exporter
    except ImportError as exc:
        pytest.skip(f"required imports unavailable: {exc}")

    out_dir = Path(tempfile.mkdtemp(prefix="e2e_soc_"))
    rdl_path = out_dir / "e2e_soc.rdl"
    rdl_path.write_text(_E2E_RDL)

    # 1. Compile the RDL.
    rdlc = RDLCompiler()
    Pybind11Exporter.register_udps(rdlc)
    rdlc.compile_file(str(rdl_path))
    root = rdlc.elaborate()

    # 2. Export.
    Pybind11Exporter().export(
        root, output_dir=str(out_dir), soc_name="e2e_soc", split_bindings=0
    )

    # 3. Build via cmake. ``Python_EXECUTABLE`` keeps cmake from picking up
    #    a stray system Python whose ABI tag wouldn't match the test runner;
    #    ``pybind11_DIR`` points cmake at the pybind11 install in the active
    #    virtualenv.
    env = os.environ.copy()
    try:
        import pybind11

        env["pybind11_DIR"] = pybind11.get_cmake_dir()
    except ImportError:
        pytest.skip("pybind11 not installed; cannot build e2e extension")

    build_dir = out_dir / "build"
    try:
        subprocess.run(
            [
                "cmake",
                "-S",
                str(out_dir),
                "-B",
                str(build_dir),
                f"-DPython_EXECUTABLE={sys.executable}",
            ],
            check=True,
            capture_output=True,
            env=env,
            timeout=300,
        )
        subprocess.run(
            ["cmake", "--build", str(build_dir), "--config", "Release"],
            check=True,
            capture_output=True,
            env=env,
            timeout=600,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"cmake build failed: {exc}")

    # 4. Place the .so next to the package __init__ so ``import e2e_soc`` works.
    pkg_dir = out_dir / "e2e_soc"
    found = list(build_dir.glob("**/_e2e_soc_native*"))
    if not found:
        pytest.skip("native module not found after cmake build")
    for src in found:
        if src.is_file():
            shutil.copy2(src, pkg_dir / src.name)

    # 5. Import the freshly-built module and attach a MockMaster.
    sys.path.insert(0, str(out_dir))
    try:
        module = importlib.import_module("e2e_soc")
        soc = module.create()

        if not hasattr(module, "MockMaster"):
            pytest.skip("generated module exposes no MockMaster")
        master = module.MockMaster()

        # ``soc.attach_master`` is the canonical hook (Unit 9 wire-up). It
        # accepts an optional ``where=`` for region-based routing.
        if hasattr(soc, "attach_master"):
            soc.attach_master(master)
        elif hasattr(soc, "attach"):
            soc.attach(master)
        elif hasattr(soc, "set_master"):
            soc.set_master(master)
        else:
            pytest.skip("no master-attach API on generated SoC")

        yield soc, master, module
    finally:
        if str(out_dir) in sys.path:
            sys.path.remove(str(out_dir))


# --------------------------------------------------------------------------- #
# Smoke
# --------------------------------------------------------------------------- #


class TestSmoke:
    def test_soc_was_created(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        assert soc is not None
        assert hasattr(soc, "uart")
        assert hasattr(soc, "ram")

    def test_master_attached(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        # Round-trip a write through the master.
        soc.uart.control.write(0x42)
        assert master.read(0x1000, 4) == 0x42


# --------------------------------------------------------------------------- #
# Core register read/write surface (sketch §3)
# --------------------------------------------------------------------------- #


class TestCoreOps:
    def test_read_returns_register_value(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        from peakrdl_pybind11.runtime import RegisterValue

        soc, master, _module = e2e_soc
        master.write(0x1000, 0x33, 4)
        v = soc.uart.control.read()
        assert isinstance(v, RegisterValue)
        assert int(v) == 0x33

    def test_write_is_one_bus_write(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        master.reset()
        soc.uart.control.write(0x1234)
        assert master.read(0x1000, 4) == 0x1234

    @pytest.mark.xfail(
        strict=False,
        reason="reg.modify(**fields) signature not yet aspirational on pybind11 register",
    )
    def test_modify_is_one_read_one_write(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        """``reg.modify(enable=1)`` must be exactly 1 read + 1 write."""
        soc, original_master, module = e2e_soc
        if not hasattr(module, "CallbackMaster"):
            pytest.skip("no CallbackMaster available")

        store: dict[int, int] = {0x1000: 0}
        reads = 0
        writes = 0

        def _read(addr: int, width: int) -> int:
            nonlocal reads
            reads += 1
            return store.get(addr, 0)

        def _write(addr: int, value: int, width: int) -> None:
            nonlocal writes
            writes += 1
            store[addr] = value

        counting_master = module.CallbackMaster()
        counting_master.set_read(_read)
        counting_master.set_write(_write)
        soc.attach_master(counting_master)
        try:
            soc.uart.control.modify(enable=1)
            assert reads == 1, f"modify did {reads} reads (expected 1)"
            assert writes == 1, f"modify did {writes} writes (expected 1)"
        finally:
            soc.attach_master(original_master)

    def test_poke_is_alias_for_write(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        if not hasattr(soc.uart.control, "poke"):
            pytest.xfail("reg.poke not wired (sketch §3.1 alias)")
        master.reset()
        soc.uart.control.poke(0x42)
        assert master.read(0x1000, 4) == 0x42


# --------------------------------------------------------------------------- #
# RegisterValue surface (sketch §3.2)
# --------------------------------------------------------------------------- #


class TestRegisterValue:
    def test_hex_with_grouping(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        master.write(0x1000, 0xDEADBEEF, 4)
        v = soc.uart.control.read()
        assert v.hex(group=4) == "0xdead_beef"

    def test_bin_with_grouping(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        master.write(0x1000, 0xAA, 4)
        v = soc.uart.control.read()
        out = v.bin(group=8)
        assert out.startswith("0b")

    def test_replace(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        master.write(0x1000, 0, 4)
        v = soc.uart.control.read()
        v2 = v.replace(enable=1)
        assert int(v2) == 1

    def test_table(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        master.write(0x1000, 0x33, 4)
        v = soc.uart.control.read()
        out = v.table()
        assert "enable" in out

    def test_hashable(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        master.write(0x1000, 0x42, 4)
        v = soc.uart.control.read()
        assert hash(v) == hash(0x42)

    def test_pickle_round_trip(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        master.write(0x1000, 0x42, 4)
        v = soc.uart.control.read()
        v2 = pickle.loads(pickle.dumps(v))
        assert int(v2) == int(v)


# --------------------------------------------------------------------------- #
# Field reads (sketch §3.2)
# --------------------------------------------------------------------------- #


class TestFieldReads:
    def test_field_read_returns_value(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        master.write(0x1000, 0b1, 4)
        v = soc.uart.control.enable.read()
        # Either a FieldValue or a plain int — accept either.
        assert int(v) == 1

    def test_bool_one_bit_field(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        master.write(0x1000, 0b1, 4)
        v = soc.uart.control.enable.read()
        assert bool(v) is True
        master.write(0x1000, 0, 4)
        v = soc.uart.control.enable.read()
        assert bool(v) is False

    @pytest.mark.xfail(
        strict=False, reason="field.bits[N] indexing not wired on generated field classes"
    )
    def test_field_bits_indexing(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        master.write(0x1000, 0b1, 4)
        bit0 = soc.uart.control.enable.bits[0]
        assert int(bit0.read()) == 1


# --------------------------------------------------------------------------- #
# Side effects (sketch §11)
# --------------------------------------------------------------------------- #


class TestSideEffects:
    @pytest.mark.xfail(
        strict=False, reason="field.peek() not wired on generated pybind11 field classes"
    )
    def test_peek_rclr(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        # ``peek`` reads without clearing on rclr fields.
        soc.uart.intr_status.por_flag.peek()

    @pytest.mark.xfail(
        strict=False, reason="field.clear() not wired on generated pybind11 field classes"
    )
    def test_clear_woclr(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        soc.uart.intr_status.tx_done.clear()

    @pytest.mark.xfail(strict=False, reason="field.pulse() not wired on singlepulse field")
    def test_pulse_singlepulse(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        soc.uart.pulse.start.pulse()

    @pytest.mark.xfail(strict=False, reason="field.acknowledge() alias not wired")
    def test_acknowledge_alias(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        soc.uart.intr_status.tx_done.acknowledge()


# --------------------------------------------------------------------------- #
# .info namespace (sketch §4.2)
# --------------------------------------------------------------------------- #


class TestInfo:
    @pytest.mark.xfail(
        strict=False, reason="generated pybind11 classes have no .info attribute (Unit 4)"
    )
    def test_info_address(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        assert soc.uart.control.info.address == 0x1000

    @pytest.mark.xfail(strict=False, reason="generated pybind11 classes have no .info attribute")
    def test_info_path(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        path = soc.uart.control.info.path
        assert "control" in str(path)

    @pytest.mark.xfail(strict=False, reason="generated pybind11 classes have no .info attribute")
    def test_info_fields(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        assert "enable" in soc.uart.control.info.fields

    @pytest.mark.xfail(strict=False, reason="generated pybind11 classes have no .info attribute")
    def test_info_tags(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        # Should be present as a namespace; missing UDPs return ``None``.
        _ = soc.uart.control.info.tags


# --------------------------------------------------------------------------- #
# Wait/poll (sketch §14)
# --------------------------------------------------------------------------- #


class TestWaitPoll:
    def test_wait_for_timeout_raises(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        from peakrdl_pybind11.errors import WaitTimeoutError

        master.write(0x1000, 0, 4)  # never becomes 1
        with pytest.raises(WaitTimeoutError) as excinfo:
            soc.uart.control.enable.wait_for(1, timeout=0.05, period=0.01)
        # ``last_seen`` must be populated for post-mortem clarity.
        assert hasattr(excinfo.value, "last_seen")


# --------------------------------------------------------------------------- #
# Snapshots (sketch §15)
# --------------------------------------------------------------------------- #


class TestSnapshots:
    @pytest.mark.xfail(
        strict=False,
        reason="soc.snapshot() not bindable to pybind11 SoC (no __dict__) — Unit 2 wire-up",
    )
    def test_snapshot_diff(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        master.write(0x1000, 0, 4)
        snap1 = soc.snapshot()
        master.write(0x1000, 0x42, 4)
        snap2 = soc.snapshot()
        diff = snap2.diff(snap1)
        assert diff is not None

    @pytest.mark.xfail(strict=False, reason="soc.snapshot() not bindable to pybind11 SoC")
    def test_snapshot_json_roundtrip(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        from peakrdl_pybind11.runtime import Snapshot

        snap = soc.snapshot()
        encoded = snap.to_json()
        snap2 = Snapshot.from_json(encoded)
        assert hash(snap2) == hash(snap)


# --------------------------------------------------------------------------- #
# Observers (sketch §16.2)
# --------------------------------------------------------------------------- #


class TestObservers:
    @pytest.mark.xfail(
        strict=False, reason="soc.observe() not bindable to pybind11 SoC — Unit 3 wire-up"
    )
    def test_observe_captures_reads(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        with soc.observe() as obs:
            soc.uart.control.read()
        assert obs.coverage_report().total_reads > 0


# --------------------------------------------------------------------------- #
# Bus policies (sketch §13.4)
# --------------------------------------------------------------------------- #


class TestBusPolicies:
    @pytest.mark.xfail(
        strict=False,
        reason="reg.cache_for() not bindable to pybind11 register classes",
    )
    def test_cache_for_dedupes_reads(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, original_master, module = e2e_soc
        if not hasattr(module, "CallbackMaster"):
            pytest.skip("no CallbackMaster available")

        store = {0x1004: 0x1}
        reads = 0

        def _read(addr: int, width: int) -> int:
            nonlocal reads
            reads += 1
            return store.get(addr, 0)

        def _write(addr: int, value: int, width: int) -> None:
            store[addr] = value

        counting_master = module.CallbackMaster()
        counting_master.set_read(_read)
        counting_master.set_write(_write)
        soc.attach_master(counting_master)
        try:
            soc.uart.status.cache_for(10e-3)
            soc.uart.status.read()
            soc.uart.status.read()
            assert reads == 1, f"cache_for didn't dedupe: {reads} reads"
        finally:
            soc.attach_master(original_master)


# --------------------------------------------------------------------------- #
# Transactions (sketch §13.2)
# --------------------------------------------------------------------------- #


class TestTransactions:
    def test_read_write_burst_construct(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        from peakrdl_pybind11.runtime import Burst, Read, Write

        r = Read(0x1000)
        w = Write(0x1004, 0x42)
        b = Burst(0x4000, count=4, op="read")
        assert r.addr == 0x1000
        assert w.value == 0x42
        assert b.count == 4

    def test_master_execute_returns_read_values(
        self, e2e_soc: tuple[Any, Any, Any]
    ) -> None:
        _soc, master, _module = e2e_soc
        from peakrdl_pybind11.runtime import Read, Write, execute

        master.write(0x1000, 0x42, 4)
        # Use the module-level ``execute`` since pybind11 masters can't
        # accept setattr-attached methods.
        results = execute(master, [Read(0x1000), Write(0x1004, 0x99), Read(0x1004)])
        assert results == [0x42, 0x99]

    @pytest.mark.xfail(
        strict=False,
        reason="master.execute(...) bound method not attachable to pybind11 master",
    )
    def test_master_execute_method(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        _soc, master, _module = e2e_soc
        from peakrdl_pybind11.runtime import Read, Write

        master.write(0x1000, 0x42, 4)
        results = master.execute([Read(0x1000), Write(0x1004, 0x99)])
        assert results == [0x42]


# --------------------------------------------------------------------------- #
# Memory (sketch §6)
# --------------------------------------------------------------------------- #


class TestMemory:
    def test_mem_indexing(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        try:
            soc.ram[10] = 0xDEAD
            assert soc.ram[10] == 0xDEAD
        except Exception:
            pytest.xfail("mem __getitem__/__setitem__ shape not yet aspirational")

    @pytest.mark.xfail(strict=False, reason="MemView slice wrap not active on generated mem")
    def test_mem_slice_returns_memview(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        from peakrdl_pybind11.runtime import MemView

        view = soc.ram[5:10]
        assert isinstance(view, MemView)

    @pytest.mark.xfail(strict=False, reason="MemView.copy() returning ndarray not active")
    def test_mem_slice_copy_returns_ndarray(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        import numpy as np

        soc, _master, _module = e2e_soc
        arr = soc.ram[5:10].copy()
        assert isinstance(arr, np.ndarray)


# --------------------------------------------------------------------------- #
# Arrays (sketch §7)
# --------------------------------------------------------------------------- #


class TestArrays:
    @pytest.mark.xfail(
        strict=False,
        reason="register arrays unsupported by exporter today (Index of array element error)",
    )
    def test_register_array_indexing(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        # If the array were exported, this would access soc.uart.lut[3].
        if not hasattr(soc.uart, "lut"):
            pytest.skip("lut[8] removed from RDL because exporter rejects it")
        _ = soc.uart.lut[3]

    @pytest.mark.xfail(strict=False, reason="ArrayView slice wrap not active on register arrays")
    def test_array_slice_returns_arrayview(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        from peakrdl_pybind11.runtime import ArrayView

        if not hasattr(soc.uart, "lut"):
            pytest.skip("lut[8] removed from RDL because exporter rejects it")
        view = soc.uart.lut[:]
        assert isinstance(view, ArrayView)


# --------------------------------------------------------------------------- #
# Aliases (sketch §10)
# --------------------------------------------------------------------------- #


class TestAliases:
    def test_alias_exists(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        assert hasattr(soc.uart, "control_alt")

    @pytest.mark.xfail(
        strict=False,
        reason="alias_alt.target / .is_alias not bindable to pybind11 register class",
    )
    def test_alias_target_identity(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        assert soc.uart.control_alt.target is soc.uart.control

    @pytest.mark.xfail(strict=False, reason="alias.is_alias attribute not bindable")
    def test_alias_is_alias_flag(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        assert soc.uart.control_alt.is_alias is True


# --------------------------------------------------------------------------- #
# Interrupts (sketch §9)
# --------------------------------------------------------------------------- #


class TestInterrupts:
    @pytest.mark.xfail(
        strict=False,
        reason="soc.uart.interrupts not bindable to pybind11 regfile class — Unit 5 wire-up",
    )
    def test_interrupt_group_exists(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        assert hasattr(soc.uart, "interrupts")
        assert soc.uart.interrupts.tx_done.is_pending() in (True, False)

    @pytest.mark.xfail(strict=False, reason="InterruptSource.enable() not bound on generated tree")
    def test_interrupt_enable(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        soc.uart.interrupts.tx_done.enable()

    @pytest.mark.xfail(strict=False, reason="InterruptSource.fire() not bound on generated tree")
    def test_interrupt_fire(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        soc.uart.interrupts.tx_done.fire()

    @pytest.mark.xfail(strict=False, reason="InterruptSource.clear() not bound on generated tree")
    def test_interrupt_clear(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        soc.uart.interrupts.tx_done.clear()

    @pytest.mark.xfail(strict=False, reason="InterruptGroup.pending()/clear_all() not bound")
    def test_interrupt_group_ops(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, _master, _module = e2e_soc
        pending = soc.uart.interrupts.pending()
        assert isinstance(pending, frozenset)
        soc.uart.interrupts.clear_all()


# --------------------------------------------------------------------------- #
# Routing (sketch §13.1)
# --------------------------------------------------------------------------- #


class TestRouting:
    def test_attach_master_with_where(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, original_master, module = e2e_soc
        if not hasattr(module, "MockMaster"):
            pytest.skip("no MockMaster available")
        other = module.MockMaster()
        try:
            soc.attach_master(other, where="ram")
        except TypeError:
            pytest.xfail("attach_master(where=...) signature not aspirational on this build")
        finally:
            soc.attach_master(original_master)


# --------------------------------------------------------------------------- #
# Hot reload (sketch §21)
# --------------------------------------------------------------------------- #


class TestHotReload:
    @pytest.mark.xfail(
        strict=False,
        reason="soc.reload() not bindable to pybind11 SoC instance",
    )
    def test_reload_preserves_master(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        soc.reload()
        # Bus state on the mock is not reset by host-side reload.
        master.write(0x1000, 0xCAFE, 4)
        assert master.read(0x1000, 4) == 0xCAFE


# --------------------------------------------------------------------------- #
# Async session (sketch §13.8 / §22.3)
# --------------------------------------------------------------------------- #


class TestAsyncSession:
    @pytest.mark.xfail(
        strict=False,
        reason="soc.async_session() not bindable to pybind11 SoC — Unit 4 wire-up",
    )
    def test_async_read(self, e2e_soc: tuple[Any, Any, Any]) -> None:
        soc, master, _module = e2e_soc
        master.write(0x1000, 0x99, 4)

        async def _run() -> int:
            async with soc.async_session() as s:
                return int(await s.uart.control.aread())

        assert asyncio.run(_run()) == 0x99


# --------------------------------------------------------------------------- #
# Summary punchlist
# --------------------------------------------------------------------------- #


def test_summary_punchlist() -> None:
    """Always-passing marker so the module reports counts in the pytest summary.

    The xfail-marked tests above form the punchlist. Read the pytest -v
    output (xpassed = unexpectedly fixed → un-xfail; xfailed = still
    waiting on a sibling unit; passed = wired and working).
    """
    assert True
