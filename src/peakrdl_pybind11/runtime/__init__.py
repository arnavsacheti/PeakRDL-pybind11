"""Runtime helpers layered on top of generated bindings.

The :mod:`peakrdl_pybind11.runtime` package collects pure-Python pieces that
sit *above* a generated module and don't depend on any specific chip:
routing, transactions, observers, snapshots, etc.
"""

from __future__ import annotations

from .routing import (
    NodeLike,
    Router,
    RoutingError,
    RoutingRule,
    attach_master,
)

__all__ = [
    "NodeLike",
    "Router",
    "RoutingError",
    "RoutingRule",
    "attach_master",
]
