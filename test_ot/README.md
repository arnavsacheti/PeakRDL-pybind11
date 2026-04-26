# test_ot — PeakRDL-pybind11 on the OpenTitan SoC

End-to-end test that generates a PyBind11 module covering the full register
space of OpenTitan's `top_earlgrey` SoC (44 modules, ~thousands of registers).

OpenTitan describes registers in its own HJSON format (consumed by
`util/reggen/`), not SystemRDL. This directory bridges that gap:

```
opentitan/ ── shallow git clone of lowRISC/opentitan
hjson_to_rdl.py ── walks every module in top_earlgrey.gen.hjson, parses each
                   IP via reggen.IpBlock, emits one big top_earlgrey.rdl
top_earlgrey.rdl ── auto-generated SystemRDL covering the full SoC
run_export.py ── runs Pybind11Exporter with --split-by-hierarchy
output/ ── generated C++ + CMake + .pyi ready to `pip install`
```

## Reproduce

```bash
# from repo root, with the project venv active and reggen deps installed
cd test_ot
git clone --depth 1 https://github.com/lowRISC/opentitan.git   # ~300 MB
uv pip install hjson semantic_version mistletoe pydantic mako
python3 hjson_to_rdl.py        # writes top_earlgrey.rdl
python3 run_export.py          # writes output/
cd output
CMAKE_BUILD_PARALLEL_LEVEL=$(sysctl -n hw.ncpu) pip install .   # ~25 min: full LTO + ~50k lines of templates
cd ..
python3 smoke_test.py     # round-trip writes/reads on uart, aes, gpio, i2c, hmac, rv_timer
```

## Conversion notes / known simplifications

- **Multiregs** are expanded by reggen's `flat_regs`, so each replica becomes
  a discrete RDL register.
- **Windows** (memory regions) are *not* emitted. Only register entries are
  exported. Adding `mem` definitions for windows would be the next step.
- **Aliases** are ignored (reggen exposes them as separate registers via
  `flat_regs`).
- **Reg widths** are rounded up to the next power of two (>= 8) because
  SystemRDL requires `regwidth` to be a power of two; OpenTitan often uses
  9-, 11-, 13-bit interrupt registers.
- **Address spaces.** A module's base address is taken from the first
  non-empty entry across all `addr_spaces`. Modules with no base in any
  space are skipped (none in earlgrey, currently).
- **Empty register blocks.** Some IPs (e.g. `rv_dm`'s `dbg`/`dmi`) have
  reg-block entries with zero registers; those are skipped, with a
  placeholder register added if the whole IP would otherwise be empty.
- **swaccess mapping** uses the table at the top of `hjson_to_rdl.py`. A few
  exotic OpenTitan modes (`rw0c`, `r0w1c`) are approximated to the closest
  SystemRDL semantics (`woclr`).
## Exporter fixes that landed alongside this work

The first version of this directory needed three workarounds. They have all
been fixed upstream in PeakRDL-pybind11:

1. **Class-name collisions across IPs** — the exporter now derives every
   C++ class name from the node path (e.g. `top_earlgrey__uart0__INTR_STATE_t`)
   instead of the raw `inst_name`, so reusing register names like
   `INTR_STATE` across IPs no longer redefines the same class.
2. **Install layout** — the generated wheel now ships as a real Python
   package at `<soc_name>/__init__.py` plus the native `.so` in the same
   directory, so `import <soc_name>` works after a vanilla `pip install .`.
3. **Master lifetime** — `attach_master` uses `py::keep_alive<1, 2>` to tie
   the master object's Python lifetime to the SoC's, so passing an inline
   temporary like `soc.attach_master(wrap_master(MockMaster()))` no longer
   segfaults on the next register access.

## Why this isn't checked in upstream OpenTitan

OpenTitan ships its own register tooling and does not publish SystemRDL
output, so any user wanting to drive PeakRDL exporters at the OT register
space has to bridge HJSON → RDL themselves. This script is one such bridge,
intentionally pragmatic and lossy.
