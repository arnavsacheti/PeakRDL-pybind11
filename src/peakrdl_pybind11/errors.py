"""
PeakRDL-pybind11 typed exception hierarchy.

This module is the canonical home for the library's typed errors. It is a
sibling-coordinated seam: other Units (Unit 2 in particular) extend this file
with the rest of the error taxonomy (``AccessError``, ``BusError``,
``RoutingError``, etc.). Unit 9 only needs ``WaitTimeoutError``.

Every exception this library raises shares two properties:

* The message embeds the **path** of the offending node.
* Stack traces are short. ``__traceback_hide__`` is honored where applicable.
"""

from __future__ import annotations

from typing import Any


class PeakRDLError(Exception):
    """Base class for all PeakRDL-pybind11 typed errors.

    Catching ``PeakRDLError`` catches every error this library raises. Most
    user code should catch the more specific subclass that describes the
    failure mode they care about.
    """


class WaitTimeoutError(PeakRDLError, TimeoutError):
    """Raised when a polling wait does not converge before its deadline.

    Attributes
    ----------
    path
        The descriptor path of the node being polled (e.g.
        ``"soc.uart.status.tx_ready"``). Always present.
    expected
        The value, predicate, or description of the condition the wait was
        looking for. ``None`` when the predicate cannot be summarised.
    last_seen
        The most recent value read from the node before the deadline. May be
        ``None`` if no successful read happened.
    samples
        Ordered list of every sample observed during the wait, *only* when
        the wait was started with ``capture=True``. ``None`` otherwise (the
        default) so tight polling loops do not pay the memory cost.
    timeout
        The deadline in seconds that was exceeded.
    polls
        Number of bus reads that were issued before the deadline elapsed.
    """

    def __init__(
        self,
        path: str,
        *,
        expected: Any = None,
        last_seen: Any = None,
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
