# Contributing to PeakRDL-pybind11

Thank you for your interest in contributing to PeakRDL-pybind11!

## Development Setup

1. Clone the repository:
```bash
git clone https://github.com/arnavsacheti/PeakRDL-pybind11.git
cd PeakRDL-pybind11
```

2. Install in development mode:
```bash
pip install -e .
```

3. Install development dependencies:
```bash
pip install -e ".[dev]"
```

## Running Tests

Run the test suite:
```bash
pytest tests/
```

Run tests with coverage:
```bash
pytest --cov=peakrdl_pybind11 tests/
```

## Code Style

This project follows PEP 8 style guidelines. Use pylint to check your code:
```bash
pylint peakrdl_pybind11/
```

## Project Structure

```
peakrdl_pybind11/
├── __init__.py           # Package initialization
├── __peakrdl__.py        # PeakRDL plugin entry point
├── exporter.py           # Main exporter implementation
├── masters/              # Master backend implementations
│   ├── __init__.py
│   ├── openocd.py
│   └── ssh.py
└── templates/            # Jinja2 templates for code generation
    ├── descriptors.hpp.jinja
    ├── bindings.cpp.jinja
    ├── runtime.py.jinja
    ├── setup.py.jinja
    └── stubs.pyi.jinja
```

## Adding a New Master Backend

To add a new Master backend:

1. Create a new file in `peakrdl_pybind11/masters/` (e.g., `mymaster.py`)
2. Implement a class inheriting from `MasterBase`
3. Implement the `read()` and `write()` methods
4. Import it in `peakrdl_pybind11/masters/__init__.py`

Example:
```python
from . import MasterBase

class MyMaster(MasterBase):
    def read(self, address: int, width: int) -> int:
        # Your implementation
        pass
    
    def write(self, address: int, value: int, width: int) -> None:
        # Your implementation
        pass
```

## Submitting Changes

1. Create a new branch for your feature/fix
2. Make your changes
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## License

By contributing, you agree that your contributions will be licensed under the GPL-3.0 License.
