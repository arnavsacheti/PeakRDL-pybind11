#!/usr/bin/env python3
"""Convert OpenTitan top_earlgrey HJSON register descriptions into a single
SystemRDL file, then export it via PeakRDL-pybind11.

Strategy:
- Parse top_earlgrey.gen.hjson to enumerate modules + base addresses.
- For each module, parse the IP hjson with reggen.IpBlock and emit one RDL
  addrmap. Multiregs are expanded by reggen into flat_regs.
- Compose all module addrmaps under a top-level `top_earlgrey` addrmap with
  absolute byte offsets (relative to the addrmap base of 0x40000000).
- Skip windows (memory regions) and modules that have no register block or
  no base address on any address space.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import hjson

OT_ROOT = Path(__file__).parent / "opentitan"
sys.path.insert(0, str(OT_ROOT / "util"))

from reggen.ip_block import IpBlock  # noqa: E402
from reggen.register import Register  # noqa: E402
from reggen.window import Window  # noqa: E402

TOP_HJSON = OT_ROOT / "hw/top_earlgrey/data/autogen/top_earlgrey.gen.hjson"

# Reggen swaccess keys -> (sw, optional onread, optional onwrite)
SW_MAP = {
    "ro":     ("r",  None,    None),
    "rw":     ("rw", None,    None),
    "wo":     ("w",  None,    None),
    "rc":     ("r",  "rclr",  None),
    "rw1c":   ("rw", None,    "woclr"),
    "rw1s":   ("rw", None,    "woset"),
    "rw0c":   ("rw", None,    "woclr"),  # approximate
    "w1c":    ("w",  None,    "woclr"),
    "w1s":    ("w",  None,    "woset"),
    "w0c":    ("w",  None,    "woclr"),
    "r0w1c":  ("w",  None,    "woclr"),
    "wo":     ("w",  None,    None),
}
HW_MAP = {
    "hro":  "r",
    "hrw":  "rw",
    "hwo":  "w",
    "none": "na",
}

# SystemRDL reserved words / common builtins to avoid as identifiers.
RDL_RESERVED = {
    "abstract", "accesswidth", "activehigh", "activelow", "addressing",
    "addrmap", "alias", "all", "alternate", "anded", "arbiter", "async",
    "bigendian", "boolean", "bothedge", "bridge", "buffer", "burst",
    "byteinfo", "ored", "rclr", "regfile", "reg", "reset", "rsvdset",
    "rsvdsetX", "signal", "singlepulse", "string", "swacc", "sw", "hw",
    "swmod", "swwe", "swwel", "sync", "true", "false", "default", "decr",
    "decrsaturate", "decrthreshold", "decrvalue", "decrwidth", "donttest",
    "dontcompare", "encode", "enum", "errextbus", "external", "field",
    "fieldwidth", "haltenable", "haltmask", "hwclr", "hwenable", "hwmask",
    "hwset", "incr", "incrsaturate", "incrthreshold", "incrvalue", "incrwidth",
    "internal", "intr", "lebigendian", "level", "littleendian", "lsb0",
    "mask", "mem", "memwidth", "msb0", "name", "negedge", "next", "nonsticky",
    "number", "onread", "onwrite", "ored", "outofband", "overflow",
    "posedge", "precedence", "property", "rclr", "ref", "reg", "regalign",
    "regwidth", "rset", "rsvd", "rw", "rw1", "ro", "rwclr", "rwset",
    "saturate", "shared", "sharedextbus", "signalwidth", "singlepulse",
    "stalled", "sticky", "stickybit", "swacc", "swmod", "this",
    "threshold", "underflow", "we", "wel", "woclr", "woset", "wo",
    "wr", "writeable", "xored", "rdl", "ref", "in", "out",
}


def sanitize(name: str, prefix: str = "_") -> str:
    """Make `name` a safe SystemRDL identifier."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name))
    if not s:
        s = "x"
    if s[0].isdigit():
        s = prefix + s
    if s.lower() in RDL_RESERVED:
        s = s + "_r"
    return s


def to_int(v) -> int:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("0x") or s.startswith("0X"):
            return int(s, 16)
        return int(s)
    if v is None:
        return 0
    try:
        return int(v)
    except Exception:
        return 0


def emit_field(out, f, indent="            "):
    sw_key = f.swaccess.key if f.swaccess else "rw"
    hw_key = f.hwaccess.key if f.hwaccess else "hro"
    sw, onread, onwrite = SW_MAP.get(sw_key, ("rw", None, None))
    hw = HW_MAP.get(hw_key, "r")
    name = sanitize(f.name)
    lsb, msb = f.bits.lsb, f.bits.msb
    out.append(f"{indent}field {{")
    out.append(f"{indent}    sw = {sw}; hw = {hw};")
    if onread:
        out.append(f"{indent}    onread = {onread};")
    if onwrite:
        out.append(f"{indent}    onwrite = {onwrite};")
    out.append(f"{indent}}} {name}[{msb}:{lsb}] = {to_int(f.resval)};")


def _next_pow2(n: int) -> int:
    p = 8
    while p < n:
        p <<= 1
    return p


def emit_register(out, r: Register, indent="        ", name_prefix: str = ""):
    # name_prefix is no longer needed for collision avoidance — the upstream
    # exporter now uses path-derived class names — but the parameter is kept
    # so callers stay backward-compatible.
    del name_prefix
    name = sanitize(r.name)
    raw_width = r.get_width()
    width = _next_pow2(max(raw_width, 8))
    out.append(f"{indent}reg {{")
    out.append(f"{indent}    regwidth = {width};")
    if not r.fields:
        # Synthesize a single field spanning the register so the reg is non-empty.
        out.append(f"{indent}    field {{ sw = rw; hw = r; }} val[{width-1}:0] = 0;")
    else:
        for f in r.fields:
            emit_field(out, f, indent + "    ")
    out.append(f"{indent}}} {name} @ 0x{r.offset:X};")


def emit_addrmap_for_ip(ip: IpBlock, inst_name: str) -> str:
    """Emit a SystemRDL addrmap definition (not instance) for one IP block."""
    type_name = sanitize(inst_name) + "_t"
    lines = [f"addrmap {type_name} {{"]
    lines.append(f"    name = \"{inst_name}\";")
    # OpenTitan IPs may have multiple register blocks (interfaces). Emit each
    # as a regfile (or inline regs if exactly one anonymous block).
    rb_items = list(ip.reg_blocks.items())
    if len(rb_items) == 1 and (rb_items[0][0] is None or rb_items[0][0] == ""):
        rb = rb_items[0][1]
        for entry in rb.entries:
            if isinstance(entry, Register):
                emit_register(lines, entry, indent="    ")
            # Skip Window/MultiRegister; flat_regs handled below if needed
        # Also handle multiregs that aren't in entries
        seen = {id(r) for r in rb.entries if isinstance(r, Register)}
        for r in rb.flat_regs:
            if id(r) not in seen and not any(r.name == e.name for e in rb.entries if isinstance(e, Register)):
                emit_register(lines, r, indent="    ")
    else:
        for rb_name, rb in rb_items:
            rf_name = sanitize(rb_name or "regs")
            lines.append(f"    regfile {{")
            for r in rb.flat_regs:
                emit_register(lines, r, indent="        ")
            lines.append(f"    }} {rf_name};")
    lines.append("};")
    return "\n".join(lines)


def main() -> int:
    with open(TOP_HJSON) as f:
        top = hjson.load(f)

    # IP type -> hjson search paths
    search_dirs = [
        OT_ROOT / "hw/ip",
        OT_ROOT / "hw/top_earlgrey/ip_autogen",
        OT_ROOT / "hw/top_earlgrey/ip",
    ]

    def find_hjson(ip_type: str) -> Path | None:
        for d in search_dirs:
            p = d / ip_type / "data" / f"{ip_type}.hjson"
            if p.exists():
                return p
        return None

    addrmap_defs: list[str] = []
    instances: list[tuple[str, str, int]] = []  # (type_name, inst_name, base_addr)
    skipped: list[tuple[str, str]] = []

    cache: dict[str, IpBlock] = {}
    for mod in top["module"]:
        inst = mod["name"]
        ip_type = mod["type"]

        # Find a base address — try every addr_space, take first non-empty.
        base_addr = None
        for space, ifaces in (mod.get("base_addrs") or {}).items():
            if not ifaces:
                continue
            for _if, addr in ifaces.items():
                if addr is None or addr == "":
                    continue
                base_addr = to_int(addr)
                break
            if base_addr is not None:
                break
        if base_addr is None:
            skipped.append((inst, "no base address"))
            continue

        hj = find_hjson(ip_type)
        if hj is None:
            skipped.append((inst, f"no hjson for type {ip_type}"))
            continue

        try:
            if ip_type not in cache:
                cache[ip_type] = IpBlock.from_path(str(hj), [])
            ip = cache[ip_type]
        except Exception as e:
            skipped.append((inst, f"parse error: {e}"))
            continue

        # Generate per-instance addrmap def (so name/offsets are unique)
        try:
            type_name = sanitize(inst) + "_t"
            rb_items = list(ip.reg_blocks.items())
            lines = [f"addrmap {type_name} {{", f'    name = "{inst}";']
            if len(rb_items) == 1 and (rb_items[0][0] is None or rb_items[0][0] == ""):
                rb = rb_items[0][1]
                for r in rb.flat_regs:
                    emit_register(lines, r, indent="    ")
            else:
                for rb_name, rb in rb_items:
                    if not rb.flat_regs:
                        continue
                    rf_name = sanitize(rb_name or "regs")
                    lines.append("    regfile {")
                    for r in rb.flat_regs:
                        emit_register(lines, r, indent="        ")
                    lines.append(f"    }} {rf_name};")
            # Guard against fully-empty addrmap (unsupported by SystemRDL).
            if not any(l.strip().startswith("reg ") or l.strip().startswith("regfile ") for l in lines):
                lines.insert(2, f"    reg {{ field {{ sw=rw; hw=r; }} val[31:0] = 0; }} {sanitize(inst)}_placeholder @ 0x0;")
            lines.append("};")
            addrmap_defs.append("\n".join(lines))
            instances.append((type_name, sanitize(inst), base_addr))
        except Exception as e:
            skipped.append((inst, f"emit error: {e}"))

    # Compose top addrmap. Use 0x40000000 as the reference base; offsets are
    # absolute (RDL allows arbitrary @ offsets at this scope).
    top_lines = ["addrmap top_earlgrey {", '    name = "OpenTitan top_earlgrey";']
    for type_name, inst_name, base in instances:
        top_lines.append(f"    {type_name} {inst_name} @ 0x{base:X};")
    top_lines.append("};")

    out_dir = Path(__file__).parent
    rdl_path = out_dir / "top_earlgrey.rdl"
    with open(rdl_path, "w") as f:
        f.write("// Auto-generated from OpenTitan top_earlgrey HJSON via reggen.\n\n")
        f.write("\n\n".join(addrmap_defs))
        f.write("\n\n")
        f.write("\n".join(top_lines))
        f.write("\n")

    print(f"Wrote {rdl_path}")
    print(f"Modules emitted: {len(instances)}")
    print(f"Modules skipped: {len(skipped)}")
    for n, why in skipped:
        print(f"  - {n}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
