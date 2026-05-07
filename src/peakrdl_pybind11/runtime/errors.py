"""Error taxonomy for the PeakRDL-pybind11 runtime.

Implements the model described in ``docs/IDEAL_API_SKETCH.md`` §19, plus the
:class:`BusError` detail from §13.5. Every exception carries enough context
(node path and, where meaningful, the absolute address) to triage failures in
CI without re-running the test under a debugger.

Stack traces stay short by setting ``__tracebackhide__`` in internal helper
frames; pytest honors this convention to skip framework code when rendering
tracebacks.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence

__all__ = [
    "AccessError",
    "BusError",
    "NotSupportedError",
    "RoutingError",
    "SideEffectError",
    "StaleHandleError",
    "WaitTimeoutError",
    "did_you_mean",
]


class AccessError(Exception):
    """Raised when a software access violates the field's access mode.

    Examples include writing to a read-only field or reading from a
    write-only field. The default message follows the sketch §19 form
    ``"<node_path> is sw=<access_mode>"`` (e.g. ``"uart.status.tx_ready is
    sw=r"``); callers may override with a custom ``message``.
    """

    def __init__(
        self,
        node_path: str,
        access_mode: str,
        message: str | None = None,
    ) -> None:
        self.node_path = node_path
        self.access_mode = access_mode
        if message is None:
            message = f"{node_path} is sw={access_mode}"
        self.message = message
        super().__init__(message)


class SideEffectError(Exception):
    """Raised when an access would trigger a side-effect inside a
    ``no_side_effects()`` block.

    The canonical case is reading an ``rclr`` field while side-effects are
    suppressed (the read would clear the field, which the caller has
    explicitly opted out of).
    """

    def __init__(self, node_path: str, message: str | None = None) -> None:
        self.node_path = node_path
        if message is None:
            message = (
                f"{node_path} would trigger a side-effect inside no_side_effects()"
            )
        self.message = message
        super().__init__(message)


class NotSupportedError(Exception):
    """Raised when a feature is not implemented by the active master/transport.

    For example, calling ``peek()`` on a master that cannot do non-destructive
    reads, or requesting interrupt subscription on a polling-only backend.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class BusError(Exception):
    """Raised when a bus transaction fails after all retries are exhausted.

    Carries the failed transaction, the master that issued it, the retry
    count, and the underlying exception — enough for a CI run to triage why
    it died (sketch §13.5).
    """

    def __init__(
        self,
        address: int,
        op: str,
        master: object,
        retries: int = 0,
        underlying: BaseException | None = None,
    ) -> None:
        self.address = address
        self.op = op
        self.master = master
        self.retries = retries
        self.underlying = underlying
        retry_word = "retry" if retries == 1 else "retries"
        parts = [
            f"bus {op} @ 0x{address:08x}",
            f"master={_master_name(master)}",
            f"{retries} {retry_word}",
        ]
        underlying_summary = _summarise_exception(underlying)
        if underlying_summary:
            parts.append(f"underlying={underlying_summary}")
        self.message = ", ".join(parts)
        super().__init__(self.message)


class RoutingError(Exception):
    """Raised when an absolute address has no master attached to serve it.

    The default message follows the sketch §19 form
    ``"no master attached for 0x<address>"``; callers may override.
    """

    def __init__(self, address: int, message: str | None = None) -> None:
        self.address = address
        if message is None:
            message = f"no master attached for 0x{address:08x}"
        self.message = message
        super().__init__(message)


class StaleHandleError(Exception):
    """Raised when a :class:`RegisterValue` or :class:`Snapshot` is used after
    the underlying SoC has been reloaded (``soc.reload()``)."""

    def __init__(self, node_path: str, message: str | None = None) -> None:
        self.node_path = node_path
        if message is None:
            message = f"handle for {node_path} is stale; SoC was reloaded"
        self.message = message
        super().__init__(message)


class WaitTimeoutError(TimeoutError):
    """Raised when ``wait_until``/``poll`` exceeds its timeout.

    Subclasses :class:`TimeoutError` so existing ``except TimeoutError``
    handlers still catch it. Optionally records a sample trail of the
    intermediate values seen while polling, useful for glitch debugging.
    """

    def __init__(
        self,
        node_path: str,
        expected: object,
        last_seen: object,
        samples: Sequence[object] | None = None,
    ) -> None:
        self.node_path = node_path
        self.expected = expected
        self.last_seen = last_seen
        self.samples = tuple(samples) if samples is not None else None
        message = (
            f"timeout waiting for {node_path} == {expected!r}; "
            f"last_seen={last_seen!r}"
        )
        if self.samples is not None:
            message += f"; samples={list(self.samples)!r}"
        self.message = message
        super().__init__(message)


def did_you_mean(name: str, candidates: Iterable[str]) -> str:
    """Return the closest match from ``candidates`` for ``name``.

    Returns the empty string when nothing is close enough. Used to power the
    "did you mean ...?" hint shown alongside :class:`AttributeError` for
    misspelled field/register names.
    """

    # pytest skips frames whose locals contain __tracebackhide__ = True, so
    # user-facing tracebacks stay focused on the caller's code rather than
    # this helper.
    __tracebackhide__ = True

    matches = difflib.get_close_matches(name, candidates, n=1)
    return matches[0] if matches else ""


def _master_name(master: object) -> str:
    """Best-effort name extraction for a master object."""

    __tracebackhide__ = True
    if master is None:
        return "<no-master>"
    name = getattr(master, "name", None)
    if isinstance(name, str) and name:
        return name
    return type(master).__name__


def _summarise_exception(exc: BaseException | None) -> str:
    """One-line summary of an underlying exception for :class:`BusError`."""

    __tracebackhide__ = True
    if exc is None:
        return ""
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {text}"
