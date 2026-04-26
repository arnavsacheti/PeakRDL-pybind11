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
CMAKE_BUILD_PARALLEL_LEVEL=$(sysctl -n hw.ncpu) pip install .   # ~21 min on M1: full LTO + ~50k lines of templates
cd ..
python3 smoke_test.py     # round-trip writes/reads on uart, aes, gpio, i2c, hmac, rv_timer
```

## Timings

Measured on an Apple M1 (8 cores) with Apple clang 21 / Python 3.13.2 /
scikit-build-core. Single end-to-end run from a clean output dir:

| stage | time |
|---|---|
| `hjson_to_rdl.py` (parse 44 IPs → 27k-line RDL) | ~3 s |
| `run_export.py` (RDL → 222k-line C++ + 40 split bindings) | ~5 s |
| `pip install .` of the generated module | **21 m 11 s** |
| `import top_earlgrey` (cold) | 0.14 s |
| `top_earlgrey.create()` | <1 ms |
| `smoke_test.py` (6 IPs round-tripped) | <1 s |

Generated artifacts:

| artifact | size |
|---|---|
| `top_earlgrey.rdl` | ~27 k lines |
| `top_earlgrey_descriptors.hpp` | ~222 k lines, 2 611 register classes, 56 node classes |
| `top_earlgrey_bindings_*.cpp` (40 split files) | ~47 k lines total |
| compiled `_top_earlgrey_native.*.so` | 69 MB |

Build time is dominated by the LTO link of ~2 600 register classes; per
the README in the parent project, splitting by hierarchy (`split_by_hierarchy=True`)
is what makes the parallel compile feasible at all.

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
## Why we don't use HEP-SoC/PeakRDL-opentitan

`peakrdl-opentitan` exists ([HEP-SoC/PeakRDL-opentitan](https://github.com/HEP-SoC/PeakRDL-opentitan))
and does HJSON ↔ SystemRDL conversion — in principle exactly what we
need. In practice, only ~5 of top_earlgrey's ~36 unique IP types
survive its importer:

- `TypeError: expected string or bytes-like object, got 'bool'` in
  `create_signal_definition` for any IP with bool-typed alert/intr
  signal entries (most IPs)
- `RDLCompileError: High bit … exceeds MSb of parent register` for
  multiregs wider than the parent (rv_plic, csrng, pattgen, …)
- `KeyError: 'name'` on certain register descriptors (hmac)
- The 0.0.1 wheel is also missing `sig_props.rdl` — would need a
  manual copy from the source repo.

`build_ot.py` in this directory is the alternative pipeline using their
importer; it succeeds on 11/44 module instances. Until those upstream
bugs are fixed it isn't a viable replacement, so the supported path
remains `hjson_to_rdl.py` (which uses OpenTitan's own `reggen.IpBlock`
parser and handles all 44 modules).

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
