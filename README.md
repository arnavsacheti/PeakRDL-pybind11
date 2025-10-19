# PeakRDL PyBind11 Backend

`peakrdl-pybind` is a [PeakRDL](https://github.com/SystemRDL/peakrdl) backend that generates a C++ extension module
exposing a register map as Python classes via [PyBind11](https://pybind11.readthedocs.io/).

The generated modules allow Python-based integration tests to attach to hardware or simulators
through a pluggable `Master` interface. This repository hosts the backend implementation, Jinja2 templates,
and development utilities.

## Features

* CLI and backend entry point for the PeakRDL compiler.
* Templated generation of C++ sources, CMake configuration, and optional typing stubs.
* Configurable word size, endianness, namespace, and optional example masters.
* Lightweight internal IR that captures registers, fields, arrays, and access policies from the SystemRDL design.

## Getting Started

```
pip install peakrdl-pybind
peakrdl-pybind --soc-name aurora --top top --out build/aurora \
    --rdl path/to/top.rdl --gen-pyi --with-examples
```

After generation, build the produced extension using `python -m build` inside the output directory.

Refer to the documentation within `docs/` for deeper guidance.
