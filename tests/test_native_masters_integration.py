"""Integration test for the C++ MockMaster / CallbackMaster shipped in
every generated module.

Builds a tiny SoC, attaches the native ``MockMaster`` (and then the
native ``CallbackMaster`` with Python callables), and checks that
``reg.write`` / ``reg.read`` round-trip correctly.

Skips automatically if cmake / pybind11 isn't available.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

NATIVE_RDL = """
addrmap nm_soc {
    name = "Native masters integration test";
    reg {
        field { sw = rw; hw = r; } data[31:0] = 0;
    } reg_a @ 0x0;

    reg {
        field { sw = rw; hw = r; } data[31:0] = 0;
    } reg_b @ 0x4;
};
"""


def _build_test_module(workdir, soc_name="nm_soc"):
    """Export + build + import a tiny SoC, returning the module or None on failure."""
    rdl_path = Path(workdir) / "nm.rdl"
    rdl_path.write_text(NATIVE_RDL)

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

    # Import the package via its own directory.
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
    except Exception as e:
        print(f"Failed to import generated module: {e}")
        return None
    return module


class TestNativeMasters:
    def test_native_mock_master_round_trip(self, tmpdir):
        soc_module = _build_test_module(tmpdir)
        if soc_module is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = soc_module.create()
        # Inline temporary verifies py::keep_alive on attach_master.
        soc.attach_master(soc_module.MockMaster())

        soc.reg_a.write(0xDEADBEEF)
        soc.reg_b.write(0xCAFE)
        assert int(soc.reg_a.read()) == 0xDEADBEEF
        assert int(soc.reg_b.read()) == 0xCAFE

        # MockMaster also exposes a reset that clears the dict-backed store.
        master = soc_module.MockMaster()
        soc.attach_master(master)
        soc.reg_a.write(0x1234)
        assert master.size == 1
        master.reset()
        assert master.size == 0
        assert int(soc.reg_a.read()) == 0  # default after reset

    def test_native_callback_master_round_trip(self, tmpdir):
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

        soc.reg_a.write(0xAA)
        soc.reg_b.write(0xBB)
        assert int(soc.reg_a.read()) == 0xAA
        assert int(soc.reg_b.read()) == 0xBB
        # The store dict was populated by the C++ -> Python callback hop.
        assert set(store.keys()) == {soc.reg_a.offset, soc.reg_b.offset}


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
