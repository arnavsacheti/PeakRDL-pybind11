"""
Runtime error types referenced by sibling units.

Underscore-prefixed so the auto-import machinery treats it as part of
the runtime's own implementation (loaded before sibling units, see
``runtime/__init__.py``). The full error taxonomy described in
``IDEAL_API_SKETCH.md`` §19 will land in dedicated sibling units; this
module exists so units that need a base error type can import a stable
location without circular dependencies.

Sibling units should re-export the names they care about from
:mod:`peakrdl_pybind11.runtime` so users see a flat namespace:

    from peakrdl_pybind11.runtime import NotSupportedError
"""

from __future__ import annotations

__all__ = ["NotSupportedError"]


class NotSupportedError(RuntimeError):
    """Raised when an operation is unavailable in the current configuration.

    The canonical example is ``--watch`` on a system without the optional
    ``watchdog`` package: the feature exists in the API surface, but the
    soft dependency that powers it is not installed.

    Stays a subclass of :class:`RuntimeError` so generic ``except
    RuntimeError`` blocks (common in long-running test scripts) still
    catch it without callers needing to import the new type.
    """
