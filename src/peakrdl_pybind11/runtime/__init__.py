"""Runtime support package for the PeakRDL-pybind11 generated API.

See ``docs/IDEAL_API_SKETCH.md`` for the full design. This package houses the
pure-Python helpers that ride on top of the generated pybind11 module — the
generated tree exposes raw register/field accessors, and the runtime layers
the higher-level ergonomics (transactions, snapshots, interrupts, waits, …).
"""

from __future__ import annotations
