"""Runtime microbenchmark for the batched ``write_many`` / ``read_many``
path on the native ``CallbackMaster``.

The whole point of the batch interface is amortizing the C++ <-> Python
boundary across N ops. We compare:

* 1000 single-op ``write`` calls (the Python lambda is hit 1000 times)
* 1 ``write_many`` call with 1000 ops (the Python batch lambda is hit once)

If pybind11 / cmake aren't available we skip — same recipe as
``tests/test_native_masters_integration.py``.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from typing import Any

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

# Same minimal RDL as the integration tests; we don't actually go through
# the register objects here, we drive the master directly.
_BENCH_RDL = """
addrmap bench_soc {
    reg {
        field { sw = rw; hw = r; } data[31:0] = 0;
    } reg_a @ 0x0;
};
"""


@pytest.fixture(scope="module")
def native(tmp_path_factory: pytest.TempPathFactory) -> Any:  # noqa: ANN401
    workdir = tmp_path_factory.mktemp("bench_master")
    rdl_path = workdir / "bench.rdl"
    rdl_path.write_text(_BENCH_RDL)

    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_path))
    root = rdl.elaborate()

    output_dir = workdir / "out"
    output_dir.mkdir()
    Pybind11Exporter().export(root.top, str(output_dir), soc_name="bench_soc")

    build_dir = output_dir / "build"
    build_dir.mkdir()

    if subprocess.run(["cmake", ".."], cwd=build_dir, capture_output=True, text=True).returncode != 0:
        pytest.skip("cmake configure failed (pybind11 unavailable?)")
    if (
        subprocess.run(
            ["cmake", "--build", ".", "--config", "Release"], cwd=build_dir, capture_output=True, text=True
        ).returncode
        != 0
    ):
        pytest.skip("cmake build failed")

    so_files = list(build_dir.glob("**/*.so")) + list(build_dir.glob("**/*.pyd"))
    if not so_files:
        pytest.skip("No built extension found")

    pkg_dir = output_dir / "bench_soc"
    pkg_dir.mkdir(exist_ok=True)
    shutil.copy(so_files[0], pkg_dir)
    sys.path.insert(0, str(output_dir))

    spec = importlib.util.spec_from_file_location("bench_soc", str(pkg_dir / "__init__.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_soc"] = module
    spec.loader.exec_module(module)
    return module


N_OPS = 1000


def test_callback_single_op_writes(benchmark: Any, native: Any) -> None:  # noqa: ANN401
    """1000 individual write() calls -> 1000 C++ <-> Python hops."""
    counter = [0]

    def write_cb(addr: int, value: int, width: int) -> None:
        counter[0] += 1

    cb = native.CallbackMaster(lambda a, w: 0, write_cb)

    def run() -> None:
        counter[0] = 0
        for i in range(N_OPS):
            cb.write(i * 4, i, 4)

    benchmark(run)
    assert counter[0] == N_OPS


def test_callback_batched_writes(benchmark: Any, native: Any) -> None:  # noqa: ANN401
    """One write_many() with 1000 ops -> 1 C++ <-> Python hop."""
    counter = [0]
    op_counter = [0]

    def write_many(ops: Any) -> None:  # noqa: ANN401
        counter[0] += 1
        op_counter[0] += len(ops)

    cb = native.CallbackMaster()
    cb.set_write_many(write_many)

    ops = [native.AccessOp(i * 4, i, 4) for i in range(N_OPS)]

    def run() -> None:
        cb.write_many(ops)

    benchmark(run)
    assert counter[0] >= 1  # benchmark runs the body multiple times
    assert op_counter[0] == counter[0] * N_OPS
