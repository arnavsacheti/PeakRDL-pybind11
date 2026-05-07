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

    # Forward-compat aliases (see ``IDEAL_API_SKETCH.md`` §3): RegisterValue
    # and FieldValue are the names used in the new API surface; they alias
    # the existing ``RegisterInt`` / ``FieldInt`` types so user code can be
    # ported to the new vocabulary without behavioural change.
    RegisterValue = RegisterInt
    FieldValue = FieldInt

try:
    __version__ = version("peakrdl-pybind11")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"


__all__ = [
    "FieldInt",
    "FieldValue",
    "Pybind11Exporter",
    "RegisterInt",
    "RegisterIntEnum",
    "RegisterIntFlag",
    "RegisterValue",
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
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
