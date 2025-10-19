# PeakRDL PyBind Backend

This repository provides the `peakrdl-pybind` backend for the
[SystemRDL compiler](https://systemrdl-compiler.readthedocs.io/). The backend
turns a SystemRDL description into a C++/PyBind11 extension module that can be
imported from Python tests to perform register accesses through a pluggable
*Master* interface.

The project is currently under development. The initial goal is to provide a
solid skeleton for the backend, including the command-line interface, template
rendering pipeline, and basic documentation.

## Development environment

Install the package in editable mode with the optional testing dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
```

Run the unit tests with:

```bash
pytest
```

## Command Line Interface

The backend exposes a `peakrdl-pybind` command that wraps the backend entry
point. The command mirrors the options described in the engineering
specification and acts as a friendly shim around the SystemRDL compiler.

## Templates

The `src/peakrdl_pybind/templates` directory contains Jinja2 templates used to
render the generated C++ sources, build system files, and optional Python typing
stubs.

