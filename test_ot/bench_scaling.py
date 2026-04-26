#!/usr/bin/env python3
"""Time pip-install of synthetic SoCs at different register counts.

For each N in --sizes, generate an RDL with N register classes (each
with 8 fields), export with --split-by-hierarchy, run `pip install .`,
and record build wall time. The build time vs N curve tells us whether
compile cost scales linearly or super-linearly.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from systemrdl import RDLCompiler  # noqa: E402

from peakrdl_pybind11 import Pybind11Exporter  # noqa: E402


def make_rdl(n_regs: int, regs_per_block: int = 32) -> str:
    """Realistic-shaped synthetic SoC: registers grouped into regfiles.

    Flat top-level designs would put every register in a single chunk
    file when exported with split_by_hierarchy=True, defeating the
    parallel compile we want to measure. We chunk into ~32-reg
    regfiles, mirroring the typical IP block layout.
    """
    n_blocks = max(1, (n_regs + regs_per_block - 1) // regs_per_block)
    block_stride = regs_per_block * 4
    lines = ["addrmap synth {"]
    reg_idx = 0
    for b in range(n_blocks):
        lines.append("    regfile {")
        for k in range(regs_per_block):
            if reg_idx >= n_regs:
                break
            lines.append("        reg {")
            lines.append("            regwidth = 32;")
            for f in range(8):
                lines.append(
                    f"            field {{ sw=rw; hw=r; }} f{f}[{f * 4 + 3}:{f * 4}] = 0;"
                )
            lines.append(f"        }} reg_{reg_idx:04d} @ 0x{k * 4:X};")
            reg_idx += 1
        lines.append(f"    }} block_{b:03d} @ 0x{b * block_stride:X};")
    lines.append("};")
    return "\n".join(lines)


def export_and_build(n_regs: int, soc_name: str) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"bench_{n_regs}_"))
    rdl_text = make_rdl(n_regs)
    rdl_path = work / "synth.rdl"
    rdl_path.write_text(rdl_text)

    out = work / "out"
    out.mkdir()

    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_path))
    root = rdl.elaborate()

    t = time.time()
    Pybind11Exporter().export(root.top, str(out), soc_name=soc_name,
                              gen_pyi=True, split_by_hierarchy=True)
    export_time = time.time() - t

    env = os.environ.copy()
    env["CMAKE_BUILD_PARALLEL_LEVEL"] = str(os.cpu_count())

    t = time.time()
    proc = subprocess.run(
        ["uv", "pip", "install", "--reinstall", "--no-deps", "."],
        cwd=out, env=env, capture_output=True, text=True,
    )
    install_time = time.time() - t

    so = next((work / "out" / soc_name).glob("_*.so"), None)
    so_size = so.stat().st_size if so else None
    if so is None:
        # Find via site-packages
        import site
        for sp in site.getsitepackages():
            p = Path(sp) / soc_name
            if p.exists():
                for x in p.iterdir():
                    if x.suffix == ".so":
                        so_size = x.stat().st_size
                        break

    # Cleanup
    subprocess.run(["uv", "pip", "uninstall", soc_name],
                   capture_output=True, check=False)
    shutil.rmtree(work, ignore_errors=True)
    return {
        "n_regs": n_regs,
        "export_s": export_time,
        "install_s": install_time,
        "so_bytes": so_size,
        "ok": proc.returncode == 0,
        "stderr_tail": proc.stderr[-300:] if proc.returncode != 0 else "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[50, 200, 500, 1000])
    args = ap.parse_args()

    print(f"{'N':>6}  {'export':>9}  {'install':>10}  {'so_size':>10}  ok")
    print("-" * 50)
    for i, n in enumerate(args.sizes):
        soc = f"synth_{n}_{i}"
        r = export_and_build(n, soc)
        size = (
            f"{r['so_bytes'] / 1e6:.1f} MB" if r["so_bytes"] else "?"
        )
        print(
            f"{r['n_regs']:>6}  "
            f"{r['export_s']:>8.2f}s  "
            f"{r['install_s']:>9.1f}s  "
            f"{size:>10}  {'Y' if r['ok'] else 'N'}"
        )
        if not r["ok"]:
            print(f"   stderr: {r['stderr_tail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
