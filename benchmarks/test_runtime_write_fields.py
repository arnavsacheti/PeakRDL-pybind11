"""Microbenchmark: per-field write loop vs single ``write_fields`` call.

Compares the cost of writing 5 fields on the same register via 5 individual
``reg.<field>.write(...)`` calls (5 RMW = 10 master ops, 5 C++ entries) vs a
single ``reg.write_fields(...)`` (1 RMW = 2 master ops, 1 C++ entry plus a
small Python boundary).
"""

import importlib.util
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import py
import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

BENCH_RDL = """
addrmap wfb_soc {
    reg {
        field { sw = rw; hw = r; } f0[3:0]   = 0;
        field { sw = rw; hw = r; } f1[7:4]   = 0;
        field { sw = rw; hw = r; } f2[11:8]  = 0;
        field { sw = rw; hw = r; } f3[15:12] = 0;
        field { sw = rw; hw = r; } f4[19:16] = 0;
    } reg_multi @ 0x0;
};
"""


def _build(workdir: py.path.local, soc_name: str = "wfb_soc") -> ModuleType | None:
    rdl_path = Path(workdir) / "wfb.rdl"
    rdl_path.write_text(BENCH_RDL)
    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_path))
    root = rdl.elaborate()

    output_dir = Path(workdir) / "out"
    output_dir.mkdir()
    Pybind11Exporter().export(root.top, str(output_dir), soc_name=soc_name)

    build_dir = output_dir / "build"
    build_dir.mkdir()
    if subprocess.run(["cmake", ".."], cwd=build_dir, capture_output=True, text=True).returncode != 0:
        return None
    if (
        subprocess.run(
            ["cmake", "--build", ".", "--config", "Release"], cwd=build_dir, capture_output=True, text=True
        ).returncode
        != 0
    ):
        return None

    so_files = list(build_dir.glob("**/*.so")) + list(build_dir.glob("**/*.pyd"))
    if not so_files:
        return None
    pkg_dir = output_dir / soc_name
    pkg_dir.mkdir(exist_ok=True)
    shutil.copy(so_files[0], pkg_dir)
    sys.path.insert(0, str(output_dir))
    spec = importlib.util.spec_from_file_location(soc_name, str(pkg_dir / "__init__.py"))
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[soc_name] = module
    spec.loader.exec_module(module)
    return module


def test_write_fields_microbench(tmpdir: py.path.local) -> None:
    soc_module = _build(tmpdir)
    if soc_module is None:
        pytest.skip("Could not build benchmark module (cmake/pybind11 unavailable)")

    soc = soc_module.create()
    soc.attach_master(soc_module.MockMaster())
    reg = soc.reg_multi

    iters = 20_000

    # Warm-up.
    for _ in range(1000):
        reg.f0.write(1)
        reg.f1.write(2)
        reg.f2.write(3)
        reg.f3.write(4)
        reg.f4.write(5)
        reg.write_fields(f0=1, f1=2, f2=3, f3=4, f4=5)

    t0 = time.perf_counter()
    for _ in range(iters):
        reg.f0.write(1)
        reg.f1.write(2)
        reg.f2.write(3)
        reg.f3.write(4)
        reg.f4.write(5)
    t_field = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _ in range(iters):
        reg.write_fields(f0=1, f1=2, f2=3, f3=4, f4=5)
    t_combined = time.perf_counter() - t0

    speedup = t_field / t_combined if t_combined > 0 else float("inf")
    print(
        f"\n[write_fields microbench] iters={iters}\n"
        f"  per-field x5:        {t_field * 1e6 / iters:8.2f} us/op  ({t_field:.3f}s total)\n"
        f"  write_fields(...):   {t_combined * 1e6 / iters:8.2f} us/op  ({t_combined:.3f}s total)\n"
        f"  speedup:             {speedup:.2f}x"
    )

    # Sanity: combined call should not be slower than per-field loop.
    assert t_combined < t_field, f"write_fields should be faster: {t_combined:.3f}s vs {t_field:.3f}s"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
