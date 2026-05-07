"""Runtime helpers for PeakRDL-pybind11 generated bindings.

This package provides a uniform Python-side API on top of generated native
modules. Everything here is pure Python; the C++ bindings supply the bus
plumbing.
"""

from __future__ import annotations

from peakrdl_pybind11.runtime.info import Info, attach_info, from_rdl_node

__all__ = ["Info", "attach_info", "from_rdl_node"]
