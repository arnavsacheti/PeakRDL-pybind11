"""End-to-end tests for the per-register ``write_fields`` multi-field write.

Verifies that ``reg.write_fields(field_a=..., field_b=...)`` collapses N
field writes into exactly 1 master read + 1 master write, regardless of N,
and that field-name / writability validation happens at the Python boundary.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

WRITE_FIELDS_RDL = """
addrmap wf_soc {
    name = "write_fields integration test";
    reg {
        field { sw = rw; hw = r; } field_a[3:0] = 0;
        field { sw = rw; hw = r; } field_b[7:4] = 0;
        field { sw = rw; hw = r; } field_c[15:8] = 0;
        field { sw = r;  hw = r; } ro_field[23:16] = 0;
    } reg_multi @ 0x0;
};
"""


def _build_module(workdir, soc_name="wf_soc"):
    rdl_path = Path(workdir) / "wf.rdl"
    rdl_path.write_text(WRITE_FIELDS_RDL)

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
    except Exception as e:
        print(f"Failed to import generated module: {e}")
        return None
    return module


class TestWriteFields:
    def test_single_rmw_for_multi_field_write(self, tmpdir):
        soc_module = _build_module(tmpdir)
        if soc_module is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = soc_module.create()

        store = {}
        reads = [0]
        writes = [0]

        def _read(addr, width):
            reads[0] += 1
            return store.get(addr, 0)

        def _write(addr, value, width):
            writes[0] += 1
            store[addr] = value

        cb = soc_module.CallbackMaster(_read, _write)
        soc.attach_master(cb)

        # Multi-field write: exactly 1 read + 1 write on the master.
        soc.reg_multi.write_fields(field_a=0x5, field_b=0x3, field_c=0xAB)
        assert reads[0] == 1, f"expected 1 read, got {reads[0]}"
        assert writes[0] == 1, f"expected 1 write, got {writes[0]}"

        # Resulting register value: each field placed at its lsb.
        expected = (0x5 << 0) | (0x3 << 4) | (0xAB << 8)
        # +1 read for verification.
        assert int(soc.reg_multi.read()) == expected

    def test_unknown_field_raises_key_error(self, tmpdir):
        soc_module = _build_module(tmpdir)
        if soc_module is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = soc_module.create()
        soc.attach_master(soc_module.MockMaster())

        with pytest.raises(KeyError):
            soc.reg_multi.write_fields(field_a=1, nonexistent=2)

    def test_read_only_field_raises_permission_error(self, tmpdir):
        soc_module = _build_module(tmpdir)
        if soc_module is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = soc_module.create()
        soc.attach_master(soc_module.MockMaster())

        with pytest.raises(PermissionError):
            soc.reg_multi.write_fields(ro_field=1)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
