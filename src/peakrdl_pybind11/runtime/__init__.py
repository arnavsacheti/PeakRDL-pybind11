"""
PeakRDL-pybind11 runtime helpers.

The :mod:`peakrdl_pybind11.runtime` package gathers Python helpers that wrap
the generated chip module: polling toolkits, snapshots, observers, and the
small registries that other Units (Unit 1) use to wire enhancements onto the
generated descriptor classes.

This file deliberately only re-exports pure-Python helpers; the generated
chip module imports the helpers it needs at module-init time so the runtime
package is safe to import on its own.
"""

from __future__ import annotations
