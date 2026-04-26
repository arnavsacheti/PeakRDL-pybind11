#!/usr/bin/env python3
"""Smoke test for the generated top_earlgrey pybind11 module.

Round-trips writes and reads against several IPs scattered across the SoC's
register map using a MockMaster.

Also implicitly verifies the three exporter fixes:
  #1 Class-name collisions — same-named registers across IPs
     (e.g. ``INTR_STATE`` in every peripheral) used to redefine the same
     C++ class. The exporter now derives class names from the node path
     (``top_earlgrey__uart0__INTR_STATE_t``).
  #2 Install layout — the generated wheel ships as a real package at
     ``top_earlgrey/__init__.py``, so ``import top_earlgrey`` works after
     a vanilla ``pip install .`` from output/, with no manual reshuffling.
  #3 Master lifetime — ``attach_master`` now uses ``py::keep_alive<1, 2>``,
     so passing an inline temporary like
     ``soc.attach_master(wrap_master(MockMaster()))`` no longer segfaults.
"""
import top_earlgrey
from peakrdl_pybind11.masters import MockMaster

soc = top_earlgrey.create()
# Inline temporaries — exercises fix #3 (keep_alive on attach_master).
soc.attach_master(top_earlgrey.wrap_master(MockMaster()))

cases = [
    ("uart0",    soc.uart0.INTR_ENABLE,    0xff),
    ("aes",      soc.aes.CTRL_SHADOWED,    0x1234),
    ("gpio",     soc.gpio.DIRECT_OUT,      0xdead),
    ("i2c0",     soc.i2c0.CTRL,            0x7),
    ("hmac",     soc.hmac.CFG,             0xa5),
    ("rv_timer", soc.rv_timer.CTRL,         0x1),
]
for ip, reg, val in cases:
    reg.write(val)
    rb = int(reg.read())
    assert rb == val, f"{ip}: wrote 0x{val:x} read 0x{rb:x}"
    print(f"  {ip:10s} {reg.name:30s} round-trip 0x{rb:x}")

ips = sorted(
    a for a in dir(soc)
    if not a.startswith("_")
    and not callable(getattr(soc, a, None))
    and a not in ("name", "offset")
)
print(f"\nSoC has {len(ips)} IP instances exposed:")
print("  " + ", ".join(ips))
print("\nSMOKE TEST OK")
