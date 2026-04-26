#!/usr/bin/env python3
"""Build the top_earlgrey pybind11 module using HEP-SoC/PeakRDL-opentitan
as the HJSON->SystemRDL frontend.

STATUS: experimental. As of HEP-SoC/PeakRDL-opentitan @ 9289e7b, only
about 5 of top_earlgrey's ~36 unique IP types successfully round-trip
through `OpenTitanImporter`. The rest hit upstream bugs:

  - TypeError in create_signal_definition when an alert/interrupt entry
    carries a bool `type` key (affects aes, kmac, hmac, otbn, keymgr,
    pwrmgr, rstmgr, clkmgr, sysrst_ctrl, adc_ctrl, pwm, pinmux,
    aon_timer, ast, sensor_ctrl, sram_ctrl, flash_ctrl, rv_dm,
    rom_ctrl, rv_core_ibex, usbdev, ...)
  - RDLCompileError on multireg fields wider than 32 bits
    (rv_plic, csrng)
  - KeyError 'name' on certain register descriptions (hmac)
  - The wheel ships without `sig_props.rdl`; we copy it manually below.

Until those bugs are fixed upstream, the supported path is the hand-rolled
hjson_to_rdl.py + run_export.py pipeline. This file is kept as a starting
point so a future maintainer can pick up where it leaves off.

Approach (when it works):
  - For each module in top_earlgrey.gen.hjson, hand the IP's hjson to
    `OpenTitanImporter`, which registers an addrmap component with the
    systemrdl compiler.
  - Compile a small synthesized `addrmap top_earlgrey { ... }` that
    instantiates each of those addrmap types at its absolute base.
  - Pybind11Exporter runs against the elaborated top.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import warnings
from pathlib import Path

import hjson

HERE = Path(__file__).parent
OT_ROOT = HERE / "opentitan"
TOP_HJSON = OT_ROOT / "hw/top_earlgrey/data/autogen/top_earlgrey.gen.hjson"
OUT_DIR = HERE / "output"

from systemrdl import RDLCompiler  # noqa: E402

from peakrdl_opentitan import OpenTitanImporter  # noqa: E402
from peakrdl_pybind11 import Pybind11Exporter  # noqa: E402

warnings.filterwarnings("ignore")


def find_hjson(ip_type: str) -> Path | None:
    """Locate `<ip>.hjson` under one of the known OpenTitan source layouts."""
    for d in (
        OT_ROOT / "hw/ip",
        OT_ROOT / "hw/top_earlgrey/ip_autogen",
        OT_ROOT / "hw/top_earlgrey/ip",
    ):
        p = d / ip_type / "data" / f"{ip_type}.hjson"
        if p.exists():
            return p
    return None


def to_int(v) -> int:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        return int(s, 16) if s.startswith(("0x", "0X")) else int(s)
    return int(v) if v is not None else 0


def first_base_addr(mod: dict) -> int | None:
    """Return the first non-empty base address across any addr_space."""
    for _space, ifaces in (mod.get("base_addrs") or {}).items():
        if not ifaces:
            continue
        for _if, addr in ifaces.items():
            if addr in (None, ""):
                continue
            return to_int(addr)
    return None


def main() -> int:
    with open(TOP_HJSON) as f:
        top = hjson.load(f)

    # Pass 1: import each IP type into its OWN compiler so a broken IP
    # doesn't poison the rest. Then re-import the survivors into the real
    # compiler. This isolates HEP-SoC-importer bugs (currently affecting
    # rv_plic, csrng, pattgen, ...) where wide multiregs produce fields
    # whose high bit exceeds the parent register's MSb.
    imported_types: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []

    seen_types: set[str] = set()
    good_types: list[tuple[str, Path]] = []
    for mod in top["module"]:
        ip_type = mod["type"]
        if ip_type in seen_types:
            continue
        seen_types.add(ip_type)
        hj = find_hjson(ip_type)
        if hj is None:
            skipped.append((mod["name"], f"no hjson for type {ip_type}"))
            continue
        probe = RDLCompiler()
        try:
            OpenTitanImporter(probe).import_file(str(hj))
            probe.elaborate(top_def_name=ip_type)
        except Exception as e:
            skipped.append((mod["name"], f"importer rejected {ip_type}: {type(e).__name__}"))
            continue
        good_types.append((ip_type, hj))

    rdl = RDLCompiler()
    importer = OpenTitanImporter(rdl)
    for ip_type, hj in good_types:
        importer.import_file(str(hj))
        imported_types[ip_type] = ip_type

    # Pass 2: synthesize a top-level addrmap that instantiates each module.
    instances: list[tuple[str, str, int]] = []  # (def_name, inst_name, base)
    for mod in top["module"]:
        inst = re.sub(r"[^A-Za-z0-9_]", "_", mod["name"])
        ip_type = mod["type"]
        if ip_type not in imported_types:
            continue
        base = first_base_addr(mod)
        if base is None:
            skipped.append((mod["name"], "no base address"))
            continue
        instances.append((imported_types[ip_type], inst, base))

    lines = ["addrmap top_earlgrey {"]
    for def_name, inst_name, base in instances:
        lines.append(f"    {def_name} {inst_name} @ 0x{base:X};")
    lines.append("};")
    top_rdl = "\n".join(lines)

    fd, top_path = tempfile.mkstemp(suffix=".rdl")
    with os.fdopen(fd, "w") as f:
        f.write(top_rdl)
    rdl.compile_file(top_path)
    root = rdl.elaborate(top_def_name="top_earlgrey")

    OUT_DIR.mkdir(exist_ok=True)
    Pybind11Exporter().export(
        root.top,
        str(OUT_DIR),
        soc_name="top_earlgrey",
        gen_pyi=True,
        split_by_hierarchy=True,
    )

    print(f"Imported {len(imported_types)} unique IP types")
    print(f"Instantiated {len(instances)} modules in top_earlgrey")
    if skipped:
        print(f"Skipped {len(skipped)}:")
        for n, why in skipped:
            print(f"  - {n}: {why}")
    print(f"Wrote {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
