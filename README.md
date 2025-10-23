# PeakRDL-pybind11

[![License](https://img.shields.io/badge/license-GPL--3.0-blue)](https://github.com/arnavsacheti/PeakRDL-pybind11/blob/main/LICENSE)
[![Documentation Status](https://readthedocs.org/projects/peakrdl-pybind11/badge/?version=latest)](https://peakrdl-pybind11.readthedocs.io/en/latest/?badge=latest)

PeakRDL-pybind11 is an exporter for the PeakRDL toolchain that generates PyBind11 modules from SystemRDL register descriptions. This enables Python-based hardware testing and interaction with register maps through a clean, type-safe Python API.

## Features

- **PyBind11 Module Generation**: Automatically generates C++ descriptors and Python bindings from SystemRDL
- **SoC Hierarchy Exposure**: Import generated modules to access the complete SoC register hierarchy
- **Pluggable Master Backends**: Support for multiple communication backends:
  - Mock Master (for testing without hardware)
  - OpenOCD Master (for JTAG/SWD debugging)
  - SSH Master (for remote access)
  - Custom Master backends (extensible interface)
- **Comprehensive API**: 
  - `attach_master()`: Connect to hardware or simulator
  - `read()`: Read register values
  - `write()`: Write register values
  - `modify()`: Read-modify-write operations
- **Type Safety**: Generated `.pyi` stub files for full IDE support and type checking
- **Python-Based Testing**: Enable hardware testing with callbacks and custom logic

## Installation

```bash
pip install peakrdl-pybind11
```

## Usage

### Command Line

```bash
peakrdl pybind11 input.rdl -o output_dir --soc-name MySoC --top top_addrmap --gen-pyi
```

#### CLI Options

- `--soc-name`: Name of the generated SoC module (default: derived from input file)
- `--top`: Top-level address map node to export (default: top-level node)
- `--gen-pyi`: Generate `.pyi` stub files for type hints (enabled by default)
- `--split-bindings COUNT`: Split bindings into multiple files for parallel compilation when register count exceeds COUNT. This significantly speeds up compilation for large register maps (default: 100, set to 0 to disable). Ignored when `--split-by-hierarchy` is used.
- `--split-by-hierarchy`: Split bindings by addrmap/regfile hierarchy instead of by register count. This keeps related registers together and provides more logical grouping. Recommended for designs with clear hierarchical structure.

#### Compilation Performance Optimization

For large register maps, compilation can be very slow. PeakRDL-pybind11 includes several optimizations:

1. **Hierarchical binding splitting** (recommended): Use `--split-by-hierarchy` to split bindings by addrmap/regfile boundaries. This keeps related registers together in the same compilation unit, providing better organization and cache locality.
2. **Register count binding splitting**: When register count exceeds `--split-bindings` threshold (default: 100), bindings are automatically split into multiple `.cpp` files that can be compiled in parallel
3. **Optimized compiler flags**: The generated CMakeLists.txt uses `-O1` optimization even for debug builds, which significantly reduces compilation time for template-heavy code
4. **Parallel compilation**: CMake will automatically compile split files in parallel when using `make -j` or `ninja`

Examples for large register maps:
```bash
# Split by hierarchy (recommended for well-structured designs)
peakrdl pybind11 large_design.rdl -o output --split-by-hierarchy

# Split bindings every 50 registers for faster compilation
peakrdl pybind11 large_design.rdl -o output --split-bindings 50

# Build with parallel compilation (4 cores)
cd output
pip install . -- -DCMAKE_BUILD_PARALLEL_LEVEL=4
```

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

# For large designs, enable binding splitting by hierarchy (recommended)
exporter.export(root, "output_dir", soc_name="MySoC", split_by_hierarchy=True)

# Or split by register count
exporter.export(root, "output_dir", soc_name="MySoC", split_bindings=50)
```

### Using Generated Modules

```python
import MySoC
from peakrdl_pybind11.masters import MockMaster

# Create and attach a master
soc = MySoC.create()
master = MockMaster()
soc.attach_master(master)

# Read/write registers
value = soc.peripherals.uart.control.read()
soc.peripherals.uart.control.write(0x1234)

# Modify specific fields
soc.peripherals.uart.control.modify(enable=1, mode=2)
```

## Requirements

- Python >= 3.10
- systemrdl-compiler >= 1.30.1
- jinja2
- CMake >= 3.15 (for building generated modules)
- C++11 compatible compiler (for building generated modules)
- pybind11 (runtime dependency for generated code)

## Documentation

Full documentation is available at [ReadTheDocs](https://peakrdl-pybind11.readthedocs.io/).

To build the documentation locally:

```bash
pip install -e .[docs]
cd docs
make html
```

The built documentation will be in `docs/_build/html/`.

## License

This project is licensed under the GPL-3.0 License - see the [LICENSE](LICENSE) file for details.
