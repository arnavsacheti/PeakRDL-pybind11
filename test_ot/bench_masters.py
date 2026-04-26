#!/usr/bin/env python3
"""Compare per-access overhead of the native vs Python masters.

Cached register reference, tight loop of `write` then `read`, average
microseconds per round-trip. Run after building top_earlgrey (see
README.md). The cached-ref shape is the realistic worst-case for hot
register loops; uncached attribute traversal would add ~1 us per access
on top of every number below.
"""
import time

import top_earlgrey
from peakrdl_pybind11.masters import MockMaster as PyMockMaster

N = 100_000


def bench(label: str, attach) -> None:
    soc = top_earlgrey.create()
    attach(soc)
    reg = soc.uart0.INTR_ENABLE
    t = time.time()
    for _ in range(N):
        reg.write(0x42)
        reg.read()
    dt = time.time() - t
    print(f"{label:40s} {dt * 1e6 / N:6.3f} us/access  ({dt * 1000:.1f} ms total)")


print(f"Round-trips: {N} write+read each, on soc.uart0.INTR_ENABLE\n")
bench("native MockMaster",
      lambda s: s.attach_master(top_earlgrey.MockMaster()))
bench("native CallbackMaster (Py lambdas)", lambda s: s.attach_master(
    top_earlgrey.CallbackMaster(
        (lambda mem: lambda a, w: mem.get(a, 0))(d := {}),
        (lambda mem: lambda a, v, w: mem.__setitem__(a, v))(d),
    )
))
bench("Python MockMaster + wrap_master",
      lambda s: s.attach_master(top_earlgrey.wrap_master(PyMockMaster())))
