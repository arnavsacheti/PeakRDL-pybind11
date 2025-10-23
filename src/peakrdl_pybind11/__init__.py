"""
PeakRDL-pybind11
Export SystemRDL to PyBind11 modules for Python-based hardware testing
"""

from importlib.metadata import PackageNotFoundError, version

from src.peakrdl_pybind11.exporter import Pybind11Exporter

try:
    __version__ = version("peakrdl-pybind11")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"


# Lazy import to avoid requiring systemrdl-compiler for submodules
def __getattr__(name: str) -> type[Pybind11Exporter]:
    if name == "Pybind11Exporter":
        from .exporter import Pybind11Exporter

        return Pybind11Exporter
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = ["Pybind11Exporter"]
