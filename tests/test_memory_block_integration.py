"""Integration test for batched memory access (``read_block`` / ``write_block``).

Builds a tiny SoC that contains a memory and verifies the new batched
APIs round-trip against the native ``MockMaster`` and a Python
``CallbackMaster``, plus the bounds-checking error path.

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

MEM_RDL = """
addrmap mb_soc {
    reg entry_t {
        field { sw = rw; hw = rw; } data[31:0] = 0;
    };
    external mem {
        mementries = 64;
        memwidth = 32;
        entry_t entry;
    } data_mem @ 0x1000;
};
"""


def _build_test_module(workdir, soc_name="mb_soc"):
    rdl_path = Path(workdir) / "mb.rdl"
    rdl_path.write_text(MEM_RDL)

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
    except Exception as e:  # pragma: no cover - debug helper
        print(f"Failed to import generated module: {e}")
        return None
    return module


class TestMemoryBlock:
    def test_read_block_write_block_round_trip(self, tmpdir):
        soc_module = _build_test_module(tmpdir)
        if soc_module is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = soc_module.create()
        soc.attach_master(soc_module.MockMaster())

        n = len(soc.data_mem)
        assert n == 64

        values = [(i * 0x01010101) & 0xFFFFFFFF for i in range(n)]
        soc.data_mem.write_block(0, values)

        # Full block read.
        read_back = soc.data_mem.read_block(0, n)
        assert list(read_back) == values

        # Per-entry read agrees (sanity).
        for i in range(n):
            assert int(soc.data_mem[i].read()) == values[i]

        # Partial range.
        assert list(soc.data_mem.read_block(8, 4)) == values[8:12]

        # Partial overwrite leaves rest untouched.
        soc.data_mem.write_block(16, [0xAA, 0xBB, 0xCC])
        again = soc.data_mem.read_block(0, n)
        assert again[15] == values[15]
        assert again[16:19] == [0xAA, 0xBB, 0xCC]
        assert again[19] == values[19]

    def test_read_block_with_callback_master_batches(self, tmpdir):
        """Each entry still hops Python via CallbackMaster's default
        ``read_many`` (loops ``read``), but the call shape -- one
        Python-level method invocation on the memory -- exercises the
        batched plumbing end-to-end."""
        soc_module = _build_test_module(tmpdir)
        if soc_module is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = soc_module.create()
        store = {}
        cb = soc_module.CallbackMaster(
            lambda addr, width: store.get(addr, 0),
            lambda addr, value, width: store.__setitem__(addr, value),
        )
        soc.attach_master(cb)

        soc.data_mem.write_block(0, [i + 1 for i in range(8)])
        assert list(soc.data_mem.read_block(0, 8)) == [i + 1 for i in range(8)]

    def test_bounds_checking(self, tmpdir):
        soc_module = _build_test_module(tmpdir)
        if soc_module is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = soc_module.create()
        soc.attach_master(soc_module.MockMaster())

        n = len(soc.data_mem)

        # Past end on read.
        with pytest.raises((IndexError, ValueError, RuntimeError, OverflowError)) as exc:
            soc.data_mem.read_block(n - 2, 5)
        assert "out of range" in str(exc.value).lower() or isinstance(
            exc.value, (IndexError, ValueError)
        )

        # Length mismatch / out of range on write.
        with pytest.raises((IndexError, ValueError, RuntimeError, OverflowError)):
            soc.data_mem.write_block(n - 1, [0, 1, 2])
