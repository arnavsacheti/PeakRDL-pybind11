# PeakRDL PyBind Backend Architecture

This document captures the high level architecture of the `peakrdl-pybind` backend. It serves as a
quick reference for contributors and explains how the Python backend, the templates, and the emitted
artifacts interact.

## Backend Overview

The backend entry point is `peakrdl_pybind.backend.PyBindBackend`. When invoked from PeakRDL it
receives an elaborated SystemRDL top level node and user supplied options. The backend walks the node
hierarchy, normalises register addresses and field widths, and constructs an intermediate
representation (IR). The IR is a pure Python data structure that is passed to Jinja2 templates to
render the final C++ sources, build files, and optional typing stubs.

## Key Modules

* `backend.py`: Backend entry point, CLI option parsing, IR builder orchestration.
* `ir.py`: Helper dataclasses that describe address spaces, blocks, registers, fields, and enums.
* `render.py`: Wrapper around the Jinja2 environment with convenience helpers for template filters
  (bit masking, wstrb computation, etc.).
* `templates/`: Jinja2 templates for C++, CMake, pyproject.toml, and typing stubs.

## Generated Artifacts

The backend emits the following file structure:

```
<soc-package>/
├── cpp/
│   ├── master.hpp
│   ├── master.cpp
│   ├── reg_model.hpp
│   ├── reg_model.cpp
│   ├── accessors.hpp
│   ├── accessors.cpp
│   └── soc_module.cpp
├── CMakeLists.txt
├── pyproject.toml
└── typing/
    └── soc.pyi
```

Each source file is generated from a dedicated template. Templates are authored with readability in
mind and leverage macros to keep the output consistent across registers and address spaces.

## Testing

Pytest based tests live in `tests/` and exercise the IR builder and template rendering using the
example SystemRDL fragment from the engineering specification. The tests rely on a lightweight mock
of the SystemRDL node classes to avoid a hard dependency during unit testing.
