"""
PeakRDL-pybind11 typed exception hierarchy — top-level shim.

The canonical taxonomy now lives in :mod:`peakrdl_pybind11.runtime.errors`
(landed via the API-overhaul Unit 2). This module re-exports those classes
at the top-level path that early sibling units (notably Unit 9 wait-poll)
were already importing from, so existing code keeps working.

Two extras live here that are not in the runtime taxonomy:

* :class:`PeakRDLError` — a non-conflicting common base for the library's
  typed errors. ``isinstance(e, PeakRDLError)`` lets users catch every
  PeakRDL-pybind11 exception with a single clause.
* :class:`WaitTimeoutError` — same shape as the runtime version but
  inheriting from :class:`PeakRDLError` *and* :class:`TimeoutError` so
  it sits in both hierarchies.
"""

from __future__ import annotations

from typing import Any

# Re-export the full taxonomy from the canonical home.
from .runtime.errors import (
    AccessError,
    BusError,
    NotSupportedError,
    RoutingError,
    SideEffectError,
    StaleHandleError,
)


class PeakRDLError(Exception):
    """Base class for all PeakRDL-pybind11 typed errors.

    Catching ``PeakRDLError`` catches every error this library raises. Most
    user code should catch the more specific subclass that describes the
    failure mode they care about.
    """


class WaitTimeoutError(PeakRDLError, TimeoutError):
    """Raised when a polling wait does not converge before its deadline.

    Distinct from :class:`peakrdl_pybind11.runtime.errors.WaitTimeoutError`
    (which has a different positional-argument signature inherited from
    Unit 2's error taxonomy). Both classes inherit from :class:`TimeoutError`
    so a single ``except TimeoutError`` clause catches either; users who
    want to catch every PeakRDL-pybind11 error use :class:`PeakRDLError`.
    """

    def __init__(
        self,
        path: str,
        *,
        expected: Any = None,  # noqa: ANN401 - register/field values are user-typed
        last_seen: Any = None,  # noqa: ANN401 - register/field values are user-typed
        samples: list[Any] | None = None,
        timeout: float | None = None,
        polls: int | None = None,
        message: str | None = None,
    ) -> None:
        self.path = path
        self.expected = expected
        self.last_seen = last_seen
        self.samples = samples
        self.timeout = timeout
        self.polls = polls

        if message is None:
            timeout_part = f"{timeout:.3f}s" if timeout is not None else "the deadline"
            target = f"reach {expected!r}" if expected is not None else "satisfy predicate"
            message = f"{path} did not {target} within {timeout_part} (last seen={last_seen!r})"
            if polls is not None:
                message += f" after {polls} reads"
        super().__init__(message)


__all__ = [
    "AccessError",
    "BusError",
    "NotSupportedError",
    "PeakRDLError",
    "RoutingError",
    "SideEffectError",
    "StaleHandleError",
    "WaitTimeoutError",
]
