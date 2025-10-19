# peakrdl-pybind

`peakrdl-pybind` is a [PeakRDL](https://github.com/SystemRDL/PeakRDL) backend that emits a C++/PyBind11
register access model. The backend walks an elaborated SystemRDL design, renders a set of C++
sources through Jinja2 templates, and produces a scikit-build-core project that can be built into a
wheel. Once built, the generated Python module exposes the register map in a Python friendly API so
hardware tests can directly read/write registers from scripts.

## Features

* Generates PyBind11 bindings for address maps, blocks, registers, and fields.
* Provides a `Master` interface that allows the Python module to delegate memory accesses to an
  externally supplied implementation (OpenOCD, simulator, etc.).
* Supports register and field level access policies, masking, and read-modify-write helpers.
* Optional generation of typing stubs for IDE assistance.
* Command line interface and PeakRDL backend entry point for easy integration into existing flows.

## Usage

```bash
peakrdl-pybind --soc-name aurora --top top --out ./build/aurora \
    --gen-pyi --with-examples

cd build/aurora
python -m build
pip install dist/aurora-*.whl
```

After installation, tests can interact with the register map using the generated module:

```python
import aurora as soc

class MyMaster(soc.Master):
    def read32(self, addr: int) -> int:
        ...

    def write32(self, addr: int, data: int, wstrb: int = 0xF) -> None:
        ...

soc.attach_master(MyMaster())
value = soc.top.uart0.LCR.read()
soc.top.uart0.LCR.modify(DLAB=1, WLS=3)
```

See [`docs/`](docs/) for more detailed documentation on architecture, CLI options, and template
structure.
