"""
PeakRDL-pybind11
Export SystemRDL to PyBind11 modules for Python-based hardware testing
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .exporter import Pybind11Exporter
    from .int_types import FieldInt, RegisterInt, RegisterIntEnum, RegisterIntFlag
    from .runtime.transactions import Burst, Read, Write
    from .runtime.values import FieldValue, RegisterValue

try:
    __version__ = version("peakrdl-pybind11")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"


__all__ = [
    "Burst",
    "FieldInt",
    "FieldValue",
    "Pybind11Exporter",
    "Read",
    "RegisterInt",
    "RegisterIntEnum",
    "RegisterIntFlag",
    "RegisterValue",
    "Write",
]


def __getattr__(name: str) -> type:
    if name == "Pybind11Exporter":
        from .exporter import Pybind11Exporter

        return Pybind11Exporter
    if name in ("RegisterInt", "RegisterValue"):
        from .int_types import RegisterInt

        return RegisterInt
    if name == "RegisterIntFlag":
        from .int_types import RegisterIntFlag

        return RegisterIntFlag
    if name == "RegisterIntEnum":
        from .int_types import RegisterIntEnum

        return RegisterIntEnum
    if name in ("FieldInt", "FieldValue"):
        from .int_types import FieldInt

        return FieldInt
    if name in ("Read", "Write", "Burst"):
        from .runtime.transactions import Burst, Read, Write

        return {"Read": Read, "Write": Write, "Burst": Burst}[name]
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
