"""
PeakRDL-pybind11
Export SystemRDL to PyBind11 modules for Python-based hardware testing
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("peakrdl-pybind11")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"





__all__ = ["Pybind11Exporter", "RegisterInt", "FieldInt"]


def __getattr__(name: str) -> type:
    if name == "Pybind11Exporter":
        from .exporter import Pybind11Exporter

        return Pybind11Exporter
    if name == "RegisterInt":
        from .int_types import RegisterInt

        return RegisterInt
    if name == "FieldInt":
        from .int_types import FieldInt

        return FieldInt
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
