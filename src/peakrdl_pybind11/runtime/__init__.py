"""PeakRDL-pybind11 runtime support package.

This package collects the small Python-side helpers that compose with
the generated C++ binding (snapshots, observers, tracing, …) without
the per-SoC ``runtime.py`` module having to know about every feature.

Only the modules this unit owns are imported eagerly here. Sibling
units of the API overhaul that ship their own runtime modules can
extend this package by adding more imports — see
``IDEAL_API_SKETCH.md`` §1-§3 for the broader registry seam.
"""

from __future__ import annotations

from .trace import Trace, attach_trace

__all__ = [
    "Trace",
    "attach_trace",
]
