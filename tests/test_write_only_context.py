"""
Tests for the skip-readback (write-only) context manager.

Verifies that ``reg.write_only()``:
  (a) does NOT trigger a master read on enter,
  (b) accumulates field writes inside the context,
  (c) flushes a single master write on exit with the accumulated value,
  (d) leaves unspecified bits as 0,
  (e) is mutually exclusive with the regular ``__enter__`` (nested -> error).

Uses the same export/build harness as test_native_masters_integration.py.
Skips automatically if cmake / pybind11 isn't available.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

WO_RDL = """
addrmap wo_soc {
    name = "Write-only context test SoC";
    reg {
        field { sw = rw; hw = r; } enable[0:0] = 0;
        field { sw = rw; hw = r; } start[1:1] = 0;
        field { sw = rw; hw = r; } mode[7:4] = 0;
    } cmd @ 0x0;
};
"""


def _build(workdir, soc_name="wo_soc"):
    rdl_path = Path(workdir) / "wo.rdl"
    rdl_path.write_text(WO_RDL)

    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_path))
    root = rdl.elaborate()

    output_dir = Path(workdir) / "out"
    output_dir.mkdir()
    Pybind11Exporter().export(root.top, str(output_dir), soc_name=soc_name)

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
    pkg_dir = output_dir / soc_name
    pkg_dir.mkdir(exist_ok=True)
    shutil.copy(so_files[0], pkg_dir)

    sys.path.insert(0, str(output_dir))
    spec = importlib.util.spec_from_file_location(
        soc_name, str(pkg_dir / "__init__.py")
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[soc_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


class _CountingMaster:
    """Pure-Python master that counts read/write calls (via wrap_master)."""

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


class TestWriteOnlyContext:
    def test_skip_readback_and_accumulate(self, tmpdir):
        mod = _build(tmpdir)
        if mod is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = mod.create()
        cm = _CountingMaster()
        soc.attach_master(mod.wrap_master(cm))

        # Pre-populate "hardware" with garbage. write_only() must NOT read it.
        cm.store[soc.cmd.offset] = 0xDEADBEEF

        # Reset counters after attach (no traffic should have happened, but
        # be defensive).
        cm.reads = 0
        cm.writes = 0

        with soc.cmd.write_only() as reg:
            # (a) no master read on enter
            assert cm.reads == 0
            # (b) field writes accumulate in the cache; no master writes yet
            reg.enable.write(1)
            reg.mode.write(0xA)
            assert cm.writes == 0
            assert cm.reads == 0

        # (c) exactly one master write on exit
        assert cm.writes == 1
        assert cm.reads == 0

        # (d) accumulated value: enable=1 (bit 0), mode=0xA (bits 7:4) ->
        # 0xA1. Unspecified bits (start, upper bits) are 0.
        assert cm.store[soc.cmd.offset] == 0xA1

    def test_nested_with_regular_context_errors(self, tmpdir):
        mod = _build(tmpdir)
        if mod is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = mod.create()
        cm = _CountingMaster()
        soc.attach_master(mod.wrap_master(cm))

        # write_only inside regular context -> error.
        with pytest.raises(RuntimeError, match="already in a context"):
            with soc.cmd as _reg:
                with soc.cmd.write_only() as _reg2:
                    pass

        # Regular context inside write_only -> error.
        with pytest.raises(RuntimeError, match="already in a context"):
            with soc.cmd.write_only() as _reg:
                with soc.cmd as _reg2:
                    pass

        # Nested write_only -> error.
        with pytest.raises(RuntimeError, match="already in a context"):
            with soc.cmd.write_only() as _reg:
                with soc.cmd.write_only() as _reg2:
                    pass
