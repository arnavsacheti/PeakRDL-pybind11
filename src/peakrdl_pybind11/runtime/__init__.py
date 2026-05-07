"""
peakrdl_pybind11.runtime

Pure-Python runtime helpers consumed by generated SoC modules. Each
sub-module owns a slice of the public API sketched in
``docs/IDEAL_API_SKETCH.md`` and wires itself onto the generated classes
through the registration seam in ``_registry``.
"""

from __future__ import annotations
