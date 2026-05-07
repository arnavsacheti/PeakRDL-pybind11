"""Runtime helpers for the PeakRDL-pybind11 generated bindings.

This package hosts the *pure-Python* runtime surface that wraps the
``pybind11``-generated descriptors with higher-level ergonomics: typed
values, bulk array views, snapshots, etc. See
``docs/IDEAL_API_SKETCH.md`` for the contract.
"""

from __future__ import annotations

from peakrdl_pybind11.runtime.arrays import (
    ArrayView,
    FieldArray,
    register_register_enhancement,
)

__all__ = [
    "ArrayView",
    "FieldArray",
    "register_register_enhancement",
]
