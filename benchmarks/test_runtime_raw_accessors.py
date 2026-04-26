"""Microbench: read()/write() vs read_raw()/write_raw() on hot paths.

Compares 10000 calls of each, exercising MockMaster (no Python in the C++
read/write path). The win for ``read_raw`` is from skipping FieldInt /
RegisterInt allocation per call.
"""

import time
from collections.abc import Callable
from typing import Any

import pytest

from tests.test_native_masters_integration import _build_test_module

N = 10_000


@pytest.fixture(scope="module")
def soc(tmp_path_factory: pytest.TempPathFactory) -> Any:  # noqa: ANN401
    workdir = tmp_path_factory.mktemp("raw_bench")
    mod = _build_test_module(workdir)
    if mod is None:
        pytest.skip("Could not build test module (cmake/pybind11 unavailable)")
    soc = mod.create()
    soc.attach_master(mod.MockMaster())
    soc.reg_a.write(0xDEADBEEF)
    return soc


def _time(fn: Callable[[], None]) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def test_field_read_vs_read_raw(soc: Any) -> None:  # noqa: ANN401
    field = soc.reg_a.data

    def loop_read() -> None:
        for _ in range(N):
            field.read()

    def loop_read_raw() -> None:
        for _ in range(N):
            field.read_raw()

    # Warmup
    loop_read()
    loop_read_raw()

    t_read = _time(loop_read)
    t_raw = _time(loop_read_raw)

    print(
        f"\n[field] read(): {t_read * 1e6 / N:.3f} us/call  "
        f"read_raw(): {t_raw * 1e6 / N:.3f} us/call  "
        f"speedup: {t_read / t_raw:.2f}x"
    )
    # Sanity: raw should not be slower than wrapped (allow noise margin).
    assert t_raw <= t_read * 1.5


def test_register_read_vs_read_raw(soc: Any) -> None:  # noqa: ANN401
    reg = soc.reg_a

    def loop_read() -> None:
        for _ in range(N):
            reg.read()

    def loop_read_raw() -> None:
        for _ in range(N):
            reg.read_raw()

    loop_read()
    loop_read_raw()

    t_read = _time(loop_read)
    t_raw = _time(loop_read_raw)

    print(
        f"\n[reg] read(): {t_read * 1e6 / N:.3f} us/call  "
        f"read_raw(): {t_raw * 1e6 / N:.3f} us/call  "
        f"speedup: {t_read / t_raw:.2f}x"
    )
    assert t_raw <= t_read * 1.5


def test_field_write_vs_write_raw(soc: Any) -> None:  # noqa: ANN401
    field = soc.reg_a.data

    def loop_write() -> None:
        for i in range(N):
            field.write(i & 0xFFFFFFFF)

    def loop_write_raw() -> None:
        for i in range(N):
            field.write_raw(i & 0xFFFFFFFF)

    loop_write()
    loop_write_raw()

    t_w = _time(loop_write)
    t_raw = _time(loop_write_raw)

    print(
        f"\n[field] write(): {t_w * 1e6 / N:.3f} us/call  "
        f"write_raw(): {t_raw * 1e6 / N:.3f} us/call  "
        f"speedup: {t_w / t_raw:.2f}x"
    )
