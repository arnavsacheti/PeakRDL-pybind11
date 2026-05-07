# PeakRDL-pybind11

[![License](https://img.shields.io/badge/license-GPL--3.0-blue)](https://github.com/arnavsacheti/PeakRDL-pybind11/blob/main/LICENSE)
[![Documentation Status](https://readthedocs.org/projects/peakrdl-pybind11/badge/?version=latest)](https://peakrdl-pybind11.readthedocs.io/en/latest/?badge=latest)

PeakRDL-pybind11 generates Python bindings from SystemRDL register descriptions so a Python-fluent person — bring-up engineer, test author, FW dev, lab automator, debugger — can drive a chip from a REPL or a Jupyter notebook with `dir(soc)` and a docstring as the only documentation needed.

> **Status:** the API documented in this README is the **design target**. The current release ships the foundation (exporter, generated bindings, pluggable masters, the context-manager pattern) and is converging on the surface described here. The full design lives in [`docs/IDEAL_API_SKETCH.md`](docs/IDEAL_API_SKETCH.md). Sections marked aspirational are roadmap items; please file an issue if you hit a gap.

## Mental model

A generated module is a **typed tree of nodes** that mirrors the RDL hierarchy. Every node knows its path, its absolute address, its kind (`AddrMap`, `RegFile`, `Reg`, `Field`, `Mem`, `InterruptGroup`, `Alias`, `Signal`), its metadata, and which master serves its address range.

```
SoC
├── AddrMap          (peripherals)
│   ├── RegFile      (uart[0..3])
│   │   ├── Reg      (control)
│   │   │   └── Field (enable, baudrate, parity)
│   │   ├── Reg      (status)            ← side-effecting reads (rclr) are flagged
│   │   └── InterruptGroup               ← auto-detected from intr_state/enable/test
│   └── Mem          (sram)              ← buffer-protocol, ndarray, slice
└── Master           (the bus, pluggable)
```

Nodes are *descriptors*, not values. They produce values by reading. Values produced by reads are typed wrappers (`RegisterValue`, `FieldValue`) that behave like ints, decode enums, and round-trip cleanly back into writes.

There are exactly **four primitive operations**:

```python
reg.read()              # → RegisterValue   (1 bus read)
reg.write(value)        # raw write         (1 bus write, no read)
reg.modify(**fields)    # RMW               (1 read + 1 write)
reg.poke(value)         # explicit "I know what I'm doing", same as .write()
```

`field.read()` reads the parent register and slices. `field.write(v)` is shorthand for `reg.modify(field=v)` — one read, one write. The names are chosen so the bus cost is the obvious reading.

For non-clearing inspection of registers with read-side effects, use `peek()`. Side-effect-only operations are first-class: `clear()`, `set()`, `pulse()`, `acknowledge()`.

## Features

- **Hierarchy & discovery** — typed tree mirroring RDL. Navigate by attribute, by index, or by path string. `soc.find(0x4000_1004)`, `soc.walk(kind=Reg)`, `soc.find_by_name("control", glob=True)`.
- **Atomic multi-field updates** — `modify(**fields)` is the canonical RMW; kwargs map to fields by name and are type-checked against generated `.pyi` stubs.
- **Interrupts as a first-class group** — `INTR_STATE`/`INTR_ENABLE`/`INTR_TEST` trios are auto-detected and exposed as `InterruptGroup` nodes with `wait()`, `clear()`, `pending()`, `enable_all()`, ISR-friendly iteration, and an asyncio variant.
- **Memory as numpy-aware `MemView`** — `mem[10:20]` is a live view; `.copy()` / `.read()` snapshots into a one-burst `ndarray`. Buffer-protocol and `__array__` interop everywhere.
- **Snapshots & diff** — `soc.snapshot()` for whole-SoC golden-state captures, JSON/pickle round-trip, `snap2.diff(snap1)` for change tables, `soc.restore(snap)` for replay.
- **Jupyter widgets** — `_repr_html_` on every node renders a field table with side-effect badges, color-coded access modes, and click-to-expand source location. `node.watch(period=0.1)` produces a refreshing live monitor.
- **Pluggable masters with tracing & replay** — Mock, OpenOCD, SSH, simulator, and a `RecordingMaster`/`ReplayMaster` pair. Transactions are reified objects you can script.
- **Type-safe stubs** — generated `.pyi` declares enum types, field literals, and `Unpack[TypedDict]` for `modify(...)`. IDE completion is the spec; mypy/pyright catch unknown field names and out-of-range values.
- **Typed values** — `RegisterValue`/`FieldValue` are immutable, hashable, picklable, JSON-serializable. Mutation goes through `.replace(**fields)` and never through assignment.
- **Side effects loud, not silent** — `repr` shows `⚠ rclr`, `↻ singlepulse`, `✱ sticky`, `⚡ volatile`. `with soc.no_side_effects():` blocks destructive reads. `--strict-fields=false` opt-out exists for C-driver porting and emits a `DeprecationWarning` on every loose assignment.

## Quick example

The first three vignettes from [`docs/IDEAL_API_SKETCH.md`](docs/IDEAL_API_SKETCH.md) §24:

### Bring-up

```python
from MyChip import MyChip
from MyChip.uart import BaudRate
from peakrdl_pybind11.masters import OpenOCDMaster

soc = MyChip.create(master=OpenOCDMaster())
print(soc.uart.control)
soc.uart.control.modify(enable=1, baudrate=BaudRate.BAUD_115200)
soc.uart.data.write(ord('A'))
soc.uart.interrupts.tx_done.wait(timeout=1.0)
soc.uart.interrupts.tx_done.clear()
```

### Test

```python
def test_uart_loopback(soc):
    with soc.uart.control as r:
        r.enable    = 1
        r.loopback  = 1
        r.baudrate  = BaudRate.BAUD_115200

    soc.uart.data.write(0xA5)
    soc.uart.status.rx_ready.wait_for(True, timeout=0.1)
    assert soc.uart.data.read() == 0xA5

    soc.uart.interrupts.clear_all()
```

### Bulk memory init

```python
import numpy as np

soc.ram[:] = 0
soc.ram[0:64] = np.arange(64, dtype=np.uint32) * 4
checksum = np.bitwise_xor.reduce(np.asarray(soc.ram)[0:1024])
```

`modify(**fields)` is the canonical multi-field update; the `with soc.uart.control as r:` block is the secondary form for staging many writes (and for cases where one staged value is read back to compute another).

## Installation

```bash
pip install peakrdl-pybind11
```

## Usage

### Command line

```bash
peakrdl pybind11 input.rdl -o output_dir --soc-name MySoC --top top_addrmap --gen-pyi
```

#### CLI options

- `--soc-name` — name of the generated SoC module (default: derived from input file)
- `--top` — top-level address map node to export (default: top-level node)
- `--gen-pyi` — generate `.pyi` stub files for type hints (enabled by default)
- `--split-bindings COUNT` — split bindings into multiple files for parallel compilation when register count exceeds COUNT. Default: 100. Set to 0 to disable. Ignored when `--split-by-hierarchy` is used.
- `--split-by-hierarchy` — split bindings by addrmap/regfile hierarchy instead of by register count. Keeps related registers together; recommended for designs with clear hierarchical structure.

For large register maps, compilation can be slow. PeakRDL-pybind11 emits CMake projects that compile split files in parallel and uses `-O1` even for debug builds to reduce template-heavy build times — see [`COMPILATION_OPTIMIZATIONS.md`](COMPILATION_OPTIMIZATIONS.md) for the full breakdown.

### Python API

```python
from peakrdl_pybind11 import Pybind11Exporter
from systemrdl import RDLCompiler

# Compile SystemRDL
rdl = RDLCompiler()
rdl.compile_file("input.rdl")
root = rdl.elaborate()

# Export to PyBind11
exporter = Pybind11Exporter()
exporter.export(root, "output_dir", soc_name="MySoC")

# For large designs, enable hierarchical binding splitting (recommended)
exporter.export(root, "output_dir", soc_name="MySoC", split_by_hierarchy=True)
```

### Using generated modules

```python
import MySoC
from MySoC.uart import BaudRate, Parity
from peakrdl_pybind11.masters import MockMaster

soc = MySoC.create(master=MockMaster())

# Read a register — typed value with decoded fields in repr
v = soc.uart.control.read()
print(v)

# Atomic multi-field update — one read, one write, type-checked from .pyi
soc.uart.control.modify(enable=1, baudrate=BaudRate.BAUD_115200, parity=Parity.NONE)

# Wait for an interrupt
soc.uart.interrupts.tx_done.wait(timeout=1.0)
soc.uart.interrupts.tx_done.clear()

# Stage many writes with the secondary context-manager form
with soc.uart.control as r:
    r.enable    = 1
    r.baudrate  = BaudRate.BAUD_115200
    if r.parity.read() == Parity.NONE:
        r.parity = Parity.EVEN
# 1 read + 1 write hits the bus on exit

# Snapshot & diff
snap1 = soc.snapshot()
do_thing()
snap2 = soc.snapshot()
print(snap2.diff(snap1))
```

## Requirements

- Python >= 3.10
- [systemrdl-compiler](https://pypi.org/project/systemrdl-compiler/) >= 1.30.1
- [NumPy](https://numpy.org/) — hard runtime dependency (bursts, bulk reads, buffer protocol, snapshot frames)
- [Jinja2](https://palletsprojects.com/p/jinja/)
- CMake >= 3.15 (for building generated modules)
- C++11 compatible compiler (for building generated modules)
- [pybind11](https://pybind11.readthedocs.io/) (runtime dependency for generated code)

## Benchmarks

Performance benchmarks measure export and build times across realistic register maps:

```bash
# Run fast export benchmarks
python benchmarks/run_benchmarks.py fast

# Run all benchmarks
pytest benchmarks/ --benchmark-only

# See all benchmark options
python benchmarks/run_benchmarks.py
```

See [benchmarks/README.md](benchmarks/README.md) for detailed documentation.

## Documentation

Full documentation is available at [ReadTheDocs](https://peakrdl-pybind11.readthedocs.io/). The full design sketch lives in [`docs/IDEAL_API_SKETCH.md`](docs/IDEAL_API_SKETCH.md).

To build the documentation locally:

```bash
pip install -e .[docs]
cd docs
make html
```

The built documentation will be in `docs/_build/html/`.

## License

This project is licensed under the GPL-3.0 License — see the [LICENSE](LICENSE) file for details.
