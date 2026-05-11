#!/usr/bin/env python3
"""Benchmark the aspirational API surface on top_earlgrey.

Builds on ``bench_masters.py`` (which measures the bare ``reg.write`` /
``reg.read`` round-trip) by also covering every aspirational hot path:

* ``reg.read(raw=True)`` / ``reg.write(v, raw=True)`` — keyword-only fast
                                                       path; skips the
                                                       ``RegisterValue`` /
                                                       ``FieldValue`` wrap
* ``reg.write_fields(**fields)``      — N fields, **one** master RMW
* ``reg.modify(**fields)``            — canonical alias for write_fields
* ``with reg.write_only():``          — skip the readback, accumulate
                                         field writes, flush once
* ``with soc.transaction():``         — N register writes, **one**
                                         ``master.write_many`` call
* ``with soc.batch():``               — runtime alias for transaction()

Each row prints microseconds-per-operation and total ms so the relative
shape of the numbers stays obvious. ``N`` is per-case so heavy paths
(per-field RMW) don't dominate the wall clock.

Run after building ``top_earlgrey`` (see ``README.md``).
"""

from __future__ import annotations

import time

import os
import tempfile

import top_earlgrey
from peakrdl_pybind11.masters import MockMaster as PyMockMaster


def _attach_native(soc):
    soc.attach_master(top_earlgrey.MockMaster())


def _attach_wrapped(soc):
    soc.attach_master(top_earlgrey.wrap_master(PyMockMaster()))


# Module-level temp file so MmapMaster lives as long as the bench. Cleaned up
# via atexit. Sized to comfortably hold top_earlgrey's address space (~64 MB);
# we only ever touch a handful of registers in the bench so the backing file
# is sparse and cheap on macOS / Linux.
_mmap_path = tempfile.NamedTemporaryFile(suffix=".bin", delete=False).name
import atexit  # noqa: E402
atexit.register(lambda: os.path.exists(_mmap_path) and os.unlink(_mmap_path))


def _attach_mmap(soc):
    # top_earlgrey peripheral base addresses live in [0x4000_0000, 0x4140_0000)
    # roughly. Map exactly that window via ``base_address`` so absolute
    # addresses translate cleanly to file offsets. 64 MiB covers all bench
    # targets and stays page-aligned.
    m = top_earlgrey.MmapMaster(_mmap_path, size=64 * 1024 * 1024,
                                base_address=0x4000_0000, read_only=False)
    soc.attach_master(m)


def _format(label: str, dt: float, ops: int, unit: str = "op") -> None:
    per = dt * 1e6 / ops
    print(f"  {label:48s} {per:7.3f} us/{unit}   ({dt * 1000:7.1f} ms / {ops} {unit}s)")


def bench_scalar_roundtrip(label: str, attach, n: int = 100_000) -> None:
    print(f"\n[{label}]  scalar round-trip (write+read on uart0.INTR_ENABLE)")
    soc = top_earlgrey.create()
    attach(soc)
    reg = soc.uart0.INTR_ENABLE

    # Default surface: typed RegisterValue + FieldValue-aware write.
    t = time.time()
    for _ in range(n):
        reg.write(0x42)
        reg.read()
    _format("reg.write(int) + reg.read() -> RegisterValue", time.time() - t, n)

    # ``raw=True`` fast path: bypass the wrap.
    t = time.time()
    for _ in range(n):
        reg.write(0x42, raw=True)
        reg.read(raw=True)
    _format("reg.write(int, raw=True) + reg.read(raw=True)", time.time() - t, n)


def bench_field_roundtrip(label: str, attach, n: int = 100_000) -> None:
    print(f"\n[{label}]  field round-trip (write+read on uart0.INTR_ENABLE.tx_watermark)")
    soc = top_earlgrey.create()
    attach(soc)
    field = soc.uart0.INTR_ENABLE.tx_watermark

    t = time.time()
    for _ in range(n):
        field.write(1)
        field.read()
    _format("field.write(int) + field.read() -> FieldValue", time.time() - t, n)

    t = time.time()
    for _ in range(n):
        field.write(1, raw=True)
        field.read(raw=True)
    _format("field.write(int, raw=True) + field.read(raw=True)", time.time() - t, n)


def bench_multi_field(label: str, attach, n: int = 20_000) -> None:
    print(f"\n[{label}]  3-field write on uart0.INTR_ENABLE (per-field RMW vs combined)")
    soc = top_earlgrey.create()
    attach(soc)
    reg = soc.uart0.INTR_ENABLE
    f1, f2, f3 = reg.tx_watermark, reg.rx_watermark, reg.tx_done

    # Per-field write: each call issues its own read+modify+write on the master,
    # so for 3 fields that's 3 read+3 write = 6 master ops per outer iteration.
    t = time.time()
    for _ in range(n):
        f1.write(1)
        f2.write(1)
        f3.write(1)
    _format("3x field.write() (3 RMWs per outer op)", time.time() - t, n)

    # Manual combined modify: 1 RMW for all 3 fields, hand-computed mask/value.
    mask = f1.mask | f2.mask | f3.mask
    value = (1 << f1.lsb) | (1 << f2.lsb) | (1 << f3.lsb)
    t = time.time()
    for _ in range(n):
        reg.modify(value, mask)
    _format("reg.modify(value, mask) — manual combined (1 RMW)", time.time() - t, n)

    # write_fields(**fields): same single-RMW work, validation + dispatch in Python.
    t = time.time()
    for _ in range(n):
        reg.write_fields(tx_watermark=1, rx_watermark=1, tx_done=1)
    _format("reg.write_fields(**3 fields)", time.time() - t, n)

    # modify(**fields): canonical alias, same shape.
    t = time.time()
    for _ in range(n):
        reg.modify(tx_watermark=1, rx_watermark=1, tx_done=1)
    _format("reg.modify(**3 fields) — canonical alias", time.time() - t, n)


def bench_write_only(label: str, attach, n: int = 20_000) -> None:
    print(f"\n[{label}]  context manager (3 field writes per iter)")
    soc = top_earlgrey.create()
    attach(soc)
    reg = soc.uart0.INTR_ENABLE
    f1, f2, f3 = reg.tx_watermark, reg.rx_watermark, reg.tx_done

    # Regular context: 1 master read on enter, accumulate, 1 write on exit.
    t = time.time()
    for _ in range(n):
        with reg:
            f1.write(1)
            f2.write(1)
            f3.write(1)
    _format("with reg: (read+accumulate+write)", time.time() - t, n)

    # Write-only context: skip the read, 1 master write on exit.
    t = time.time()
    for _ in range(n):
        with reg.write_only():
            f1.write(1)
            f2.write(1)
            f3.write(1)
    _format("with reg.write_only(): (skip readback)", time.time() - t, n)


def bench_transaction(label: str, attach, outer: int = 1_000, per_tx: int = 16) -> None:
    print(
        f"\n[{label}]  cross-register transaction "
        f"({outer} loops x {per_tx} writes; total ops {outer * per_tx})"
    )
    soc = top_earlgrey.create()
    attach(soc)
    # Cache a list of register refs across multiple IPs (and offsets) so the
    # writes really do cross addresses — that's what ``write_many`` is for.
    regs = [
        soc.uart0.INTR_ENABLE,
        soc.uart1.INTR_ENABLE,
        soc.uart2.INTR_ENABLE,
        soc.uart3.INTR_ENABLE,
        soc.uart0.CTRL,
        soc.uart1.CTRL,
        soc.uart2.CTRL,
        soc.uart3.CTRL,
        soc.gpio.INTR_ENABLE,
        soc.gpio.DIRECT_OE,
        soc.gpio.MASKED_OE_LOWER,
        soc.gpio.MASKED_OE_UPPER,
        soc.i2c0.INTR_ENABLE,
        soc.i2c1.INTR_ENABLE,
        soc.i2c2.INTR_ENABLE,
        soc.hmac.CFG,
    ][:per_tx]
    assert len(regs) == per_tx, "extend the regs list for larger per_tx"

    total = outer * per_tx

    # Baseline: per-write dispatch, no batching.
    t = time.time()
    for _ in range(outer):
        for r in regs:
            r.write(0x55)
    _format("plain reg.write x N (no batching)", time.time() - t, total, unit="write")

    t = time.time()
    for _ in range(outer):
        for r in regs:
            r.write(0x55, raw=True)
    _format("plain reg.write(raw=True) x N", time.time() - t, total, unit="write")

    # Transaction: writes queue, 1 ``master.write_many`` per outer iteration.
    t = time.time()
    for _ in range(outer):
        with soc.transaction():
            for r in regs:
                r.write(0x55)
    _format("with soc.transaction(): (1 write_many/loop)", time.time() - t, total, unit="write")

    # soc.batch() is the runtime alias.
    if hasattr(soc, "batch"):
        t = time.time()
        for _ in range(outer):
            with soc.batch():
                for r in regs:
                    r.write(0x55)
        _format("with soc.batch(): (alias)", time.time() - t, total, unit="write")
    else:
        print("  soc.batch() not attached on this build — skipping")


def main() -> None:
    print("=" * 78)
    print("PeakRDL-pybind11 aspirational API micro-bench — top_earlgrey")
    print("=" * 78)
    print(
        "All benchmarks attach a master once and reuse cached register refs;\n"
        "uncached attribute traversal would add ~1 us per access on top."
    )

    bench_scalar_roundtrip("native MockMaster", _attach_native)
    bench_field_roundtrip("native MockMaster", _attach_native)
    bench_multi_field("native MockMaster", _attach_native)
    bench_write_only("native MockMaster", _attach_native)
    bench_transaction("native MockMaster", _attach_native)

    print("\n" + "-" * 78)
    print("Native MmapMaster (file-backed; no Python in hot path)")
    print("-" * 78)
    bench_scalar_roundtrip("MmapMaster", _attach_mmap)
    bench_transaction("MmapMaster", _attach_mmap)

    print("\n" + "-" * 78)
    print("With wrap_master(PyMockMaster()) — Python master, trampolined")
    print("-" * 78)
    bench_scalar_roundtrip("wrap_master(PyMockMaster)", _attach_wrapped)
    bench_transaction("wrap_master(PyMockMaster)", _attach_wrapped)


if __name__ == "__main__":
    main()
