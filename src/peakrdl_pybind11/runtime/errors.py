"""Error taxonomy for the PeakRDL-pybind11 runtime.

Minimal subset shipped with this unit so it can be tested standalone; the
full taxonomy described in ``docs/IDEAL_API_SKETCH.md`` §19 lands in Unit 2
and supersedes this module on merge. Only :class:`WaitTimeoutError` is
exercised here — the interrupt runtime raises it on poll exhaustion.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["WaitTimeoutError"]


class WaitTimeoutError(TimeoutError):
    """Raised when a poll-based wait exceeds its ``timeout``.

    Subclasses :class:`TimeoutError` so existing ``except TimeoutError``
    handlers still catch it. ``last_seen`` records the most recent value
    observed before the timeout fired; ``samples`` (when supplied) is the
    full polled trail, useful for glitch debugging.
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
        message = f"timeout waiting for {node_path} == {expected!r}; last_seen={last_seen!r}"
        if self.samples is not None:
            message += f"; samples={list(self.samples)!r}"
        self.message = message
        super().__init__(message)
