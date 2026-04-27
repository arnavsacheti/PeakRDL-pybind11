"""End-to-end tests for the cross-register ``soc.transaction()`` context.

Builds a small generated module, attaches a CallbackMaster whose
``read``/``write``/``write_many`` callbacks count invocations, then verifies
that:

* writes inside ``with soc.transaction():`` do NOT hit ``master.write`` until
  exit, and they reach the master in a single ``write_many`` call;
* exiting cleanly flushes; exiting via exception discards the queue;
* writes outside a transaction still go through one-by-one (no regression).
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

TX_RDL = """
addrmap tx_soc {
    name = "Transaction batching test";
    reg {
        field { sw = rw; hw = r; } data[31:0] = 0;
    } reg_a @ 0x0;
    reg {
        field { sw = rw; hw = r; } data[31:0] = 0;
    } reg_b @ 0x4;
    reg {
        field { sw = rw; hw = r; } data[31:0] = 0;
    } reg_c @ 0x8;
};
"""


def _build(workdir, soc_name="tx_soc"):
    rdl_path = Path(workdir) / "tx.rdl"
    rdl_path.write_text(TX_RDL)
    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_path))
    root = rdl.elaborate()

    output_dir = Path(workdir) / "out"
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
    if subprocess.run(cmake_args, cwd=build_dir,
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


class _CountingStore:
    """Mutable counters carried across the C++ <-> Python boundary."""

    def __init__(self):
        self.store = {}
        self.write_calls = 0
        self.write_many_calls = 0
        self.write_many_total_ops = 0
        self.read_calls = 0


def _make_counting_master(soc_module):
    """Wrap a Python Master subclass that counts write/write_many invocations.

    The native ``CallbackMaster`` only forwards single-op calls, so to observe
    ``write_many`` we go through the trampoline.
    """
    counters = _CountingStore()

    class CountingMaster(soc_module.Master):
        def __init__(self):
            super().__init__()

        def read(self, addr, width):
            counters.read_calls += 1
            return counters.store.get(addr, 0)

        def write(self, addr, value, width):
            counters.write_calls += 1
            counters.store[addr] = value

        def write_many(self, ops):
            counters.write_many_calls += 1
            counters.write_many_total_ops += len(ops)
            for op in ops:
                counters.store[op.address] = op.value

    return CountingMaster(), counters


class TestTransaction:
    def test_writes_batched_through_write_many(self, tmpdir):
        soc_module = _build(tmpdir)
        if soc_module is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = soc_module.create()
        master, counters = _make_counting_master(soc_module)
        soc.attach_master(master)

        # Baseline: a write outside the transaction goes through write().
        soc.reg_a.write(0x1)
        assert counters.write_calls == 1
        assert counters.write_many_calls == 0

        with soc.transaction() as tx:
            soc.reg_a.write(0xAA)
            soc.reg_b.write(0xBB)
            soc.reg_c.write(0xCC)
            # Until __exit__, none of those writes should have hit the master.
            assert counters.write_calls == 1
            assert counters.write_many_calls == 0
            assert tx.pending == 3

        # Exactly one boundary cross for the three writes.
        assert counters.write_calls == 1
        assert counters.write_many_calls == 1
        assert counters.write_many_total_ops == 3
        assert counters.store[soc.reg_a.offset] == 0xAA
        assert counters.store[soc.reg_b.offset] == 0xBB
        assert counters.store[soc.reg_c.offset] == 0xCC

    def test_exception_discards_queue(self, tmpdir):
        soc_module = _build(tmpdir)
        if soc_module is None:
            pytest.skip("Could not build test module")

        soc = soc_module.create()
        master, counters = _make_counting_master(soc_module)
        soc.attach_master(master)

        with pytest.raises(RuntimeError):
            with soc.transaction():
                soc.reg_a.write(0x55)
                raise RuntimeError("boom")

        # No flush on exception.
        assert counters.write_calls == 0
        assert counters.write_many_calls == 0
        assert soc.reg_a.offset not in counters.store

        # And the master is left in a clean state -- a subsequent transaction
        # works normally.
        with soc.transaction():
            soc.reg_b.write(0x77)
        assert counters.write_many_calls == 1
        assert counters.store[soc.reg_b.offset] == 0x77

    def test_reads_pass_through_inside_transaction(self, tmpdir):
        soc_module = _build(tmpdir)
        if soc_module is None:
            pytest.skip("Could not build test module")

        soc = soc_module.create()
        master, counters = _make_counting_master(soc_module)
        soc.attach_master(master)

        # Seed a value via direct write, then verify reads inside a tx still
        # observe synchronous values (not deferred).
        soc.reg_a.write(0x1234)
        assert counters.write_calls == 1
        with soc.transaction():
            assert int(soc.reg_a.read()) == 0x1234
            soc.reg_b.write(0x5678)
        # Single batched flush for the one queued write.
        assert counters.write_many_calls == 1
        assert counters.write_many_total_ops == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
