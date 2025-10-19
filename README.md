# peakrdl-pybind

`peakrdl-pybind` is a [PeakRDL](https://github.com/SystemRDL/peakrdl) backend that turns a SystemRDL specification into a C++ register model that is exposed to Python through [PyBind11](https://pybind11.readthedocs.io/).

The generated package exposes a light-weight API that allows Python code to read and write registers through a pluggable **Master** interface. Masters provide word-level access to the target system (OpenOCD, SSH/devmem, simulators, mocks, …). The backend focuses on test automation and interactive bring-up flows.

## Features

* Walks a fully elaborated SystemRDL design and emits C++ descriptors for address maps, blocks, registers and fields.
* Generates a PyBind11 extension module that exposes block, register and field proxy objects with Pythonic helpers.
* Provides a built-in `Master` base class plus helper glue for attaching a master instance at runtime.
* Optional generation of `.pyi` typing hints for IDE auto-completion.
* Optional generation of example master implementations for OpenOCD and SSH/devmem setups.
* CLI wrapper (`peakrdl-pybind`) that ties into the standard PeakRDL workflow.

## Quick Start

```
pip install peakrdl-pybind

peakrdl-pybind --soc-name aurora --top top --out ./build/aurora \
               --gen-pyi --with-examples my_soc.rdl

cd build/aurora
python -m build
pip install dist/aurora-*.whl
```

After installing the generated package, use it in Python tests:

```python
import aurora as soc

class MyMockMaster(soc.Master):
    def __init__(self):
        self.memory = {}

    def read32(self, addr: int) -> int:
        return self.memory.get(addr, 0)

    def write32(self, addr: int, data: int, wstrb: int = 0xF) -> None:
        self.memory[addr] = data

soc.attach_master(MyMockMaster())

soc.top.uart0.LCR.modify(DLAB=1, WLS=3)
```

## Development

* Run unit tests with `pytest`.
* Code generation is implemented in pure Python to avoid external build-time dependencies.
* The backend currently focuses on 32-bit word accesses but is designed for easy extension.

## License

This project is released under the terms of the MIT license. See `LICENSE` for details.
