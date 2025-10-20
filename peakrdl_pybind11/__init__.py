"""
PeakRDL-pybind11
Export SystemRDL to PyBind11 modules for Python-based hardware testing
"""

__version__ = "0.1.0"

# Lazy import to avoid requiring systemrdl-compiler for submodules
def __getattr__(name):
    if name == 'Pybind11Exporter':
        from .exporter import Pybind11Exporter
        return Pybind11Exporter
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

__all__ = ['Pybind11Exporter']
