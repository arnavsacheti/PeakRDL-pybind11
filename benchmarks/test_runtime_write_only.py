"""
Microbench: regular context manager vs ``reg.write_only()``.

Each iteration performs 100 "enable" sequences on a single register --
once with the regular ``with reg as r:`` context (one read + one write
per iteration) and once with ``with reg.write_only() as r:`` (one write
per iteration). The bench reports the per-iteration master op counts to
make the ~2x reduction obvious.
"""

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from pytest_benchmark.fixture import BenchmarkFixture
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

WO_BENCH_RDL = """
addrmap wo_bench_soc {
    reg {
        field { sw = rw; hw = r; } enable[0:0] = 0;
        field { sw = rw; hw = r; } start[1:1] = 0;
        field { sw = rw; hw = r; } mode[7:4] = 0;
    } cmd @ 0x0;
};
"""


class _CountingMaster:
    def __init__(self):
        self.store = {}
        self.reads = 0
        self.writes = 0

    def read(self, addr, width):
        self.reads += 1
        return self.store.get(addr, 0)

    def write(self, addr, value, width):
        self.writes += 1
        self.store[addr] = value


def _build_module():
    workdir = Path(tempfile.mkdtemp(prefix="wo_bench_"))
    rdl_path = workdir / "wo.rdl"
    rdl_path.write_text(WO_BENCH_RDL)

    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_path))
    root = rdl.elaborate()

    output_dir = workdir / "out"
    output_dir.mkdir()
    Pybind11Exporter().export(root.top, str(output_dir), soc_name="wo_bench_soc")

    build_dir = output_dir / "build"
    build_dir.mkdir()

    if subprocess.run(["cmake", ".."], cwd=build_dir,
                      capture_output=True, text=True).returncode != 0:
        return None
    if subprocess.run(["cmake", "--build", ".", "--config", "Release"],
                      cwd=build_dir, capture_output=True, text=True).returncode != 0:
        return None

    so_files = list(build_dir.glob("**/*.so")) + list(build_dir.glob("**/*.pyd"))
    if not so_files:
        return None
    pkg_dir = output_dir / "wo_bench_soc"
    pkg_dir.mkdir(exist_ok=True)
    shutil.copy(so_files[0], pkg_dir)

    sys.path.insert(0, str(output_dir))
    spec = importlib.util.spec_from_file_location(
        "wo_bench_soc", str(pkg_dir / "__init__.py")
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules["wo_bench_soc"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def soc_module():
    mod = _build_module()
    if mod is None:
        pytest.skip("Could not build test module (cmake/pybind11 unavailable)")
    return mod


class TestRuntimeWriteOnly:
    """Compare master-op counts for regular context vs write_only."""

    def test_regular_context_100_enables(self, benchmark: BenchmarkFixture, soc_module):
        soc = soc_module.create()
        cm = _CountingMaster()
        soc.attach_master(soc_module.wrap_master(cm))

        def run():
            for _ in range(100):
                with soc.cmd as reg:
                    reg.enable.write(1)
                    reg.mode.write(0xA)

        # Warm + measure ops on a single iteration so the count is stable.
        cm.reads = 0
        cm.writes = 0
        run()
        regular_reads, regular_writes = cm.reads, cm.writes
        print(f"\n[regular  ] 100 enables -> reads={regular_reads} writes={regular_writes}")

        benchmark(run)

        # Stash for the comparison test; cheap and avoids cross-test deps.
        TestRuntimeWriteOnly.regular_ops = (regular_reads, regular_writes)

    def test_write_only_context_100_enables(self, benchmark: BenchmarkFixture, soc_module):
        soc = soc_module.create()
        cm = _CountingMaster()
        soc.attach_master(soc_module.wrap_master(cm))

        def run():
            for _ in range(100):
                with soc.cmd.write_only() as reg:
                    reg.enable.write(1)
                    reg.mode.write(0xA)

        cm.reads = 0
        cm.writes = 0
        run()
        wo_reads, wo_writes = cm.reads, cm.writes
        print(f"\n[write_only] 100 enables -> reads={wo_reads} writes={wo_writes}")

        benchmark(run)

        # Sanity: write_only halves the master ops.
        regular = getattr(TestRuntimeWriteOnly, "regular_ops", None)
        if regular is not None:
            regular_reads, regular_writes = regular
            total_regular = regular_reads + regular_writes
            total_wo = wo_reads + wo_writes
            print(f"[summary   ] total master ops: regular={total_regular} "
                  f"write_only={total_wo} (ratio={total_regular / max(total_wo, 1):.2f}x)")
            assert wo_reads == 0
            assert wo_writes == 100
            assert regular_reads == 100
            assert regular_writes == 100
