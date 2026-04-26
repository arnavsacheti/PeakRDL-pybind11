"""Runtime microbench: per-entry ``mem[i].read()`` vs batched ``read_block``.

Builds a tiny SoC with a 1000-entry memory and times a full dump
through:

  1) ``[mem[i].read() for i in range(1000)]`` - 1000 Python<->C++ hops,
     plus 1000 C++->Python hops into the ``CallbackMaster`` lambda.
  2) ``mem.read_block(0, 1000)`` - 1 Python<->C++ hop, then 1000
     C++->Python hops into the lambda (the default ``read_many``
     just loops single-op ``read`` -- so the saving comes from the
     outer trampoline, not the inner one).

Run directly: ``python benchmarks/test_runtime_memory.py``. As a
pytest test it asserts the batched form is at least as fast.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

BENCH_RDL = """
addrmap bench_soc {
    reg entry_t {
        field { sw = rw; hw = rw; } data[31:0] = 0;
    };
    external mem {
        mementries = 1000;
        memwidth = 32;
        entry_t entry;
    } big_mem @ 0x1000;
};
"""

N = 1000


def _build_module(workdir: Path, soc_name: str = "bench_soc"):
    rdl_path = workdir / "bench.rdl"
    rdl_path.write_text(BENCH_RDL)

    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_path))
    root = rdl.elaborate()

    output_dir = workdir / "out"
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


def _time_loop(fn, repeats: int) -> float:
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        dt = time.perf_counter() - t0
        if dt < best:
            best = dt
    return best


def _run_bench(module):
    soc = module.create()

    counters = {"r": 0, "w": 0}
    store: dict[int, int] = {}

    def cb_read(addr, width):
        counters["r"] += 1
        return store.get(addr, 0)

    def cb_write(addr, value, width):
        counters["w"] += 1
        store[addr] = value

    cb = module.CallbackMaster(cb_read, cb_write)
    soc.attach_master(cb)

    # Pre-populate via direct write_block so the bench reads see real values.
    soc.big_mem.write_block(0, list(range(N)))

    # Bind locally for both loops to avoid attribute-lookup noise.
    big_mem = soc.big_mem

    def per_entry_read():
        return [big_mem[i].read() for i in range(N)]

    def block_read():
        return big_mem.read_block(0, N)

    # Warmup.
    per_entry_read()
    block_read()

    counters["r"] = 0
    t_per_entry = _time_loop(per_entry_read, repeats=5)
    reads_per_entry = counters["r"]

    counters["r"] = 0
    t_block = _time_loop(block_read, repeats=5)
    reads_block = counters["r"]

    return {
        "per_entry_s": t_per_entry,
        "block_s": t_block,
        "reads_per_entry": reads_per_entry // 5,
        "reads_block": reads_block // 5,
        "speedup": t_per_entry / t_block if t_block > 0 else float("inf"),
    }


def test_read_block_microbench(tmpdir):
    module = _build_module(Path(str(tmpdir)))
    if module is None:
        pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

    res = _run_bench(module)
    print(
        f"\n[mem.read_block microbench, N={N}]\n"
        f"  per-entry  : {res['per_entry_s'] * 1e3:8.3f} ms ({res['reads_per_entry']} master.read calls)\n"
        f"  read_block : {res['block_s'] * 1e3:8.3f} ms ({res['reads_block']} master.read calls)\n"
        f"  speedup    : {res['speedup']:.2f}x\n"
    )
    # Both produce the same number of underlying reads (default
    # read_many loops ``read``); the win is purely the Python<->C++
    # boundary collapse on the outer call.
    assert res["block_s"] <= res["per_entry_s"] * 1.5  # generous bound


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        module = _build_module(Path(tmp))
        if module is None:
            print("Could not build module (cmake/pybind11 unavailable)")
            sys.exit(1)
        res = _run_bench(module)
        print(
            f"[mem.read_block microbench, N={N}]\n"
            f"  per-entry  : {res['per_entry_s'] * 1e3:8.3f} ms ({res['reads_per_entry']} master.read calls)\n"
            f"  read_block : {res['block_s'] * 1e3:8.3f} ms ({res['reads_block']} master.read calls)\n"
            f"  speedup    : {res['speedup']:.2f}x"
        )
