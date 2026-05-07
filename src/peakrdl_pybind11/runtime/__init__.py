"""Runtime support for the planned PeakRDL-pybind11 API.

This sub-package collects the host-side machinery that wraps masters with
policies (barriers, caching, retries) and routes calls through the seams
defined in :mod:`peakrdl_pybind11.masters.base`.

The modules here are part of the API overhaul described in
``docs/IDEAL_API_SKETCH.md``. They are intentionally additive to keep each
unit independently mergeable; sibling units (``_errors``, ``_registry``,
``info``) provide the same names as cooperating shims here until they are
unified by the umbrella PR.
"""

from __future__ import annotations
