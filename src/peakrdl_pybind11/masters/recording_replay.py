"""Recording and replay masters (sketch §13.6).

``RecordingMaster`` wraps any other :class:`MasterBase` and logs every
read/write transaction to an in-memory list of events. The log can be
saved to disk as JSON (one array, default for ``.json`` paths) or NDJSON
(one event per line, ideal for streaming via ``file=`` so each op is
flushed as it occurs and a crashed test still leaves a usable trace).

``ReplayMaster`` consumes a saved trace and re-serves the recorded
values to the caller. Two modes are supported:

* **Strict** (default): the next requested op must match the next
  recorded op exactly (op kind + address + width). Mismatches raise
  :class:`ReplayMismatchError`. This is the mode regression tests want.
* **Loose**: requested reads are served from the recording when an
  address matches; extras (reads or writes not in the recording) are
  silently ignored. This is the mode quick "what does the driver do
  with these reads" experiments want.

The on-disk format is intentionally trivial — one event per transaction
with the keys described in :data:`Event` — so external tools (graphing,
diffing, golden-trace assertions) can consume it without importing
peakrdl_pybind11.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Literal, TypedDict

from .base import AccessOp, MasterBase

__all__ = [
    "Event",
    "FlushPolicy",
    "RecordingMaster",
    "ReplayMaster",
    "ReplayMismatchError",
]


# Streaming-file flush policy. ``"event"`` is the conservative default —
# every event is flushed to disk before the next bus op returns, so a
# crashed long-running session still leaves a usable trace. The faster
# modes trade some crash recovery for throughput:
#
# * ``"never"`` — only ``close()`` / ``__exit__`` flushes. Good for soak
#   tests where you accept losing the unflushed tail on a hard crash.
# * positive ``int N`` — flush every ``N`` events. The compromise.
#
# Profiling against ``wrap_master(PyMockMaster())`` shows per-event flush
# costs ~6.7 us/op on top of the bus op itself; ``"never"`` collapses
# that to the in-memory cost (~0.5 us) and ``N=100`` lands in between.
FlushPolicy = Literal["event", "never"] | int


class Event(TypedDict):
    """One recorded bus transaction.

    The schema is the same on the wire (NDJSON / JSON) and in memory.
    Keys mirror sketch §13.6:

    * ``op`` — ``"read"`` or ``"write"``.
    * ``address`` — absolute address (int).
    * ``value`` — the value read (for ``read``) or written (for ``write``).
    * ``width`` — register width in bytes.
    * ``timestamp`` — monotonic seconds since the recorder started.
    """

    op: str
    address: int
    value: int
    width: int
    timestamp: float


class ReplayMismatchError(Exception):
    """Raised by :class:`ReplayMaster` (strict mode) when a requested
    transaction does not match the next recorded event."""

    def __init__(
        self,
        expected: Event | None,
        actual: dict[str, Any],
        message: str | None = None,
    ) -> None:
        self.expected = expected
        self.actual = actual
        if message is None:
            if expected is None:
                message = (
                    f"replay log exhausted: requested {actual['op']} @ "
                    f"0x{actual['address']:x} width={actual['width']} "
                    "but the recording has no more events"
                )
            else:
                message = (
                    f"replay mismatch: expected {expected['op']} @ "
                    f"0x{expected['address']:x} width={expected['width']}, "
                    f"got {actual['op']} @ 0x{actual['address']:x} "
                    f"width={actual['width']}"
                )
        super().__init__(message)


def _is_ndjson_path(path: str | Path) -> bool:
    """Return True if ``path`` looks like an NDJSON-format trace file."""
    s = str(path).lower()
    return s.endswith(".ndjson") or s.endswith(".jsonl")


class RecordingMaster(MasterBase):
    """Master that records every read/write to an event log.

    Args:
        inner: The wrapped master that actually services transactions.
        file: Optional path. When set, every event is appended to the
            file as a single JSON document on its own line (NDJSON).
            Streaming this way means a long-running session that
            crashes still leaves a usable trace on disk. Use
            :meth:`save` to dump the in-memory log as a JSON array if
            you prefer a single-file artefact.
        flush: When ``file`` is set, controls how aggressively the
            streaming file is flushed. ``"event"`` (default) flushes
            after every recorded op — safest, but ~6.7 us/op overhead
            on top of the bus op. ``"never"`` only flushes on
            :meth:`close` / ``__exit__`` — fastest, but a hard crash
            loses the unflushed tail. A positive ``int N`` flushes
            every ``N`` events. ``close()`` and ``__exit__`` always
            flush any remaining buffer regardless of policy.
    """

    def __init__(
        self,
        inner: MasterBase,
        file: str | Path | None = None,
        flush: FlushPolicy = "event",
    ) -> None:
        self.inner = inner
        self.events: list[Event] = []
        self._start = time.monotonic()
        self._file: IO[str] | None = None
        # Validate up-front so a typo (``flush=0`` or ``flush="evnt"``)
        # raises at construction, not silently after the first op.
        # ``isinstance(flush, int) and not isinstance(flush, bool)``
        # excludes True/False, which would otherwise sneak through as 1/0.
        is_pos_int = isinstance(flush, int) and not isinstance(flush, bool) and flush >= 1
        if flush not in ("event", "never") and not is_pos_int:
            raise ValueError(f"flush must be 'event', 'never', or a positive int; got {flush!r}")
        self._flush_policy: FlushPolicy = flush
        self._events_since_flush = 0
        if file is not None:
            # Open in append mode so multiple sessions can stream into
            # one log file. NDJSON's one-event-per-line shape is
            # specifically chosen so concatenation is valid.
            self._file = Path(file).open("a", encoding="utf-8")

    def __enter__(self) -> RecordingMaster:  # convenience for ad-hoc traces
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Flush and close the streaming file if one was opened.

        Always flushes any buffered events before closing, regardless of
        the ``flush`` policy — otherwise ``flush="never"`` would lose
        the tail of the trace on the cm exit.
        """
        if self._file is not None:
            try:
                self._file.flush()
                self._file.close()
            finally:
                self._file = None
                self._events_since_flush = 0

    def __del__(self) -> None:  # best-effort cleanup
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # MasterBase contract
    # ------------------------------------------------------------------

    def read(self, address: int, width: int) -> int:
        value = self.inner.read(address, width)
        self._record("read", address, value, width)
        return value

    def write(self, address: int, value: int, width: int) -> None:
        self.inner.write(address, value, width)
        self._record("write", address, value, width)

    def read_many(self, ops: Sequence[AccessOp]) -> list[int]:
        # Delegate to inner.read_many when available so we get its
        # batched fast path; record per op so the trace is granular
        # enough for replay against a non-batching master.
        values = self.inner.read_many(ops)
        for op, v in zip(ops, values, strict=True):
            self._record("read", op.address, int(v), op.width)
        return list(values)

    def write_many(self, ops: Sequence[AccessOp]) -> None:
        self.inner.write_many(ops)
        for op in ops:
            self._record("write", op.address, op.value, op.width)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Write the in-memory event log to ``path``.

        Format is selected by extension: ``.ndjson`` / ``.jsonl`` writes
        one event per line (the same shape as the streaming ``file=``
        output); anything else writes a single JSON array.
        """
        out = Path(path)
        if _is_ndjson_path(out):
            with out.open("w", encoding="utf-8") as fh:
                for event in self.events:
                    fh.write(json.dumps(event))
                    fh.write("\n")
        else:
            out.write_text(json.dumps(list(self.events), indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _record(self, op: str, address: int, value: int, width: int) -> None:
        event: Event = {
            "op": op,
            "address": int(address),
            "value": int(value),
            "width": int(width),
            "timestamp": time.monotonic() - self._start,
        }
        self.events.append(event)
        if self._file is None:
            return
        self._file.write(json.dumps(event))
        self._file.write("\n")
        policy = self._flush_policy
        if policy == "event":
            # Default: flush per-op so a crashed run leaves a usable trace.
            self._file.flush()
        elif policy == "never":
            # Buffered until close() / __exit__.
            pass
        else:
            # Positive int: flush every ``policy`` events.
            self._events_since_flush += 1
            if self._events_since_flush >= policy:
                self._file.flush()
                self._events_since_flush = 0


class ReplayMaster(MasterBase):
    """Master that replays a previously recorded trace.

    Reads return the recorded value; writes are accepted (and
    optionally compared in strict mode). The recording is loaded from
    a JSON array or NDJSON file; both formats produced by
    :meth:`RecordingMaster.save` round-trip.

    Args:
        events: The recorded events. Use :meth:`from_file` for the
            common case of loading from disk.
        strict: When True (default), every transaction must match the
            next recorded event in order. When False, reads serve
            matching addresses from the recording and unmatched ops
            are silently ignored — useful for shorter/longer scripts
            that want to share a recording.
    """

    def __init__(self, events: Sequence[Event], strict: bool = True) -> None:
        self.events: list[Event] = [dict(e) for e in events]  # type: ignore[misc]
        self.strict = strict
        self._cursor = 0

    @classmethod
    def from_file(cls, path: str | Path, strict: bool = True) -> ReplayMaster:
        """Load a recording from ``path``.

        The format is auto-detected: NDJSON (one event per line) or a
        single JSON array. ``RecordingMaster.save`` writes whichever
        shape matches the path's extension.
        """
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        events: list[Event]
        if _is_ndjson_path(p):
            events = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            stripped = text.lstrip()
            if stripped.startswith("["):
                events = json.loads(text)
            else:
                # Tolerant fallback: NDJSON with a non-standard
                # extension (saving to ``run.log`` is common).
                events = [json.loads(line) for line in text.splitlines() if line.strip()]
        return cls(events, strict=strict)

    # ------------------------------------------------------------------
    # MasterBase contract
    # ------------------------------------------------------------------

    def read(self, address: int, width: int) -> int:
        if self.strict:
            event = self._consume("read", address, width)
            return int(event["value"])
        # Loose: scan from cursor forward for the first matching read.
        for i in range(self._cursor, len(self.events)):
            event = self.events[i]
            if event["op"] == "read" and event["address"] == address:
                self._cursor = i + 1
                return int(event["value"])
        # Nothing matched — return 0 as a benign default. Tests that
        # care use strict mode.
        return 0

    def write(self, address: int, value: int, width: int) -> None:
        if self.strict:
            self._consume("write", address, width, value=value)
            return
        # Loose mode: writes are simply not validated. Advance the
        # cursor past any matching write so subsequent reads line up.
        for i in range(self._cursor, len(self.events)):
            event = self.events[i]
            if (
                event["op"] == "write"
                and event["address"] == address
                and int(event.get("value", 0)) == int(value)
            ):
                self._cursor = i + 1
                return

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _consume(
        self,
        op: str,
        address: int,
        width: int,
        value: int | None = None,
    ) -> Event:
        actual: dict[str, Any] = {"op": op, "address": int(address), "width": int(width)}
        if value is not None:
            actual["value"] = int(value)
        if self._cursor >= len(self.events):
            raise ReplayMismatchError(None, actual)
        event = self.events[self._cursor]
        if event["op"] != op or int(event["address"]) != int(address):
            raise ReplayMismatchError(event, actual)
        # Width is checked because a 1-byte vs 4-byte read at the same
        # address is a real semantic difference; do not require a match
        # if the recording dropped width (older traces).
        if "width" in event and int(event["width"]) != int(width):
            raise ReplayMismatchError(event, actual)
        # Writes also compare value: replaying ``write(0x0, 0xCAFE)``
        # against a recording of ``write(0x0, 0xDEAD)`` is a real
        # divergence. Reads pass ``value=None`` so this only fires for
        # writes.
        if value is not None and int(event["value"]) != int(value):
            raise ReplayMismatchError(event, actual)
        self._cursor += 1
        return event
