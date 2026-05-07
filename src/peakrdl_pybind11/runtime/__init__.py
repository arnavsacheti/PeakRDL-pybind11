"""Runtime helpers for the PeakRDL-pybind11 generated SoC surface.

This package collects pure-Python pieces of the API sketch that don't need
to live in the C++ extension or the generated runtime template. See
``docs/IDEAL_API_SKETCH.md`` for the source of truth.

Currently exposes Unit 8 (`§15` Snapshots, diff, save/restore). Other units
(`§19` errors, `§4.2` info, `§11.2` peek) will land alongside this and reuse
the ``register_post_create`` seam below.
"""

from __future__ import annotations

from .snapshot import (
    SideEffectError,
    Snapshot,
    SnapshotDiff,
    register_post_create,
)

__all__ = [
    "SideEffectError",
    "Snapshot",
    "SnapshotDiff",
    "register_post_create",
]
