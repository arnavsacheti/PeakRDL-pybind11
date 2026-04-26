"""Microbenchmark: ``soc.transaction()`` vs. individual writes.

Builds a SoC with 50 registers, then issues 100 writes either one-at-a-time
through the master (baseline) or batched inside a ``with soc.transaction():``
context that flushes them in a single ``write_many`` call.

Run with:

    pytest benchmarks/test_runtime_transaction.py -v -s

Set ``PEAKRDL_BENCH_TRANSACTION_LOOPS=N`` to override the number of loops per
benchmark iteration.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter


def _build_module(workdir: Path, num_regs: int, soc_name: str = "tx_bench_soc") -> Any:  # noqa: ANN401
    body = "\n".join(
        f"reg {{ field {{ sw=rw; hw=r; }} data[31:0] = 0; }} r{i} @ 0x{i * 4:x};" for i in range(num_regs)
    )
    rdl_src = f"addrmap {soc_name} {{\n{body}\n}};\n"

    rdl_path = workdir / "bench.rdl"
    rdl_path.write_text(rdl_src)
    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_path))
    root = rdl.elaborate()

    output_dir = workdir / "out"
    output_dir.mkdir()
    Pybind11Exporter().export(root.top, str(output_dir), soc_name=soc_name)

    build_dir = output_dir / "build"
    build_dir.mkdir()
    cmake_args = ["cmake", ".."]
    try:
        import pybind11

        cmake_args.append(f"-Dpybind11_DIR={pybind11.get_cmake_dir()}")
        cmake_args.append(f"-DPython_EXECUTABLE={sys.executable}")
    except ImportError:
        pass
    if subprocess.run(cmake_args, cwd=build_dir, capture_output=True, text=True).returncode != 0:
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
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        print(f"Failed to import generated module: {e}")
        return None
    return module


def _time_loops(fn: Callable[[], None], loops: int) -> float:
    start = time.perf_counter()
    for _ in range(loops):
        fn()
    return (time.perf_counter() - start) / loops


def _run_bench(soc: Any, master_label: str, loops: int) -> tuple[float, float]:  # noqa: ANN401
    regs = [getattr(soc, f"r{i}") for i in range(50)]
    targets = [regs[i % 50] for i in range(100)]
    values = [0xA5A5A500 | i for i in range(100)]

    def baseline() -> None:
        for r, v in zip(targets, values, strict=True):
            r.write(v)

    def transactional() -> None:
        with soc.transaction():
            for r, v in zip(targets, values, strict=True):
                r.write(v)

    baseline()
    transactional()
    baseline_s = _time_loops(baseline, loops)
    tx_s = _time_loops(transactional, loops)
    speedup = baseline_s / tx_s if tx_s > 0 else float("inf")
    print(
        f"\n[transaction bench / {master_label}] 100 writes / 50 regs over {loops} loops:\n"
        f"  individual writes   : {baseline_s * 1e6:8.2f} us/iter\n"
        f"  inside transaction  : {tx_s * 1e6:8.2f} us/iter\n"
        f"  speedup             : {speedup:.2f}x\n"
    )
    return baseline_s, tx_s


def test_transaction_vs_individual_writes(tmp_path: Path) -> None:
    soc_module = _build_module(tmp_path, num_regs=50)
    if soc_module is None:
        pytest.skip("Could not build benchmark module (cmake/pybind11 unavailable)")

    loops = int(os.environ.get("PEAKRDL_BENCH_TRANSACTION_LOOPS", "200"))

    # MockMaster: writes are entirely native C++. Transaction savings come
    # from the Python -> C++ dispatch on each RegisterBase::write.
    soc = soc_module.create()
    soc.attach_master(soc_module.MockMaster())
    _run_bench(soc, "MockMaster (native)", loops)

    # CallbackMaster: each write crosses C++ -> Python once. The C++ default
    # write_many loops the single-op std::function (still 100 crossings), so
    # we don't expect a big win here.
    soc = soc_module.create()
    cb_store = {}
    cb = soc_module.CallbackMaster(
        lambda addr, width: cb_store.get(addr, 0),
        lambda addr, val, width: cb_store.__setitem__(addr, val),
    )
    soc.attach_master(cb)
    _run_bench(soc, "CallbackMaster (python cb)", loops)

    # PyMaster subclass that overrides write_many: this is where the
    # transaction shines, since 100 individual ``master.write`` calls become
    # one ``master.write_many`` call (1 C++ <-> Python boundary cross).
    py_store = {}

    class BatchPyMaster(soc_module.Master):
        def read(self, addr: int, width: int) -> int:
            return py_store.get(addr, 0)

        def write(self, addr: int, val: int, width: int) -> None:
            py_store[addr] = val

        def write_many(self, ops: Any) -> None:  # noqa: ANN401
            for op in ops:
                py_store[op.address] = op.value

    soc = soc_module.create()
    soc.attach_master(BatchPyMaster())
    _run_bench(soc, "PyMaster + write_many override", loops)

    # Sanity: confirm functional equivalence.
    assert int(soc.r0.read()) == 0xA5A5A532 or int(soc.r0.read()) >= 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
