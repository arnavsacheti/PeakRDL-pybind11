"""Scoped tracing of master transactions (sketch §13.6).

``soc.trace()`` is sugar around :class:`peakrdl_pybind11.masters.RecordingMaster`:
on entry the SoC's master is swapped for a recorder; on exit it is
restored. The yielded :class:`Trace` is a thin view over the recorder's
event log so callers can inspect, save, or pretty-print captured
transactions::

    with soc.trace() as t:
        soc.uart.control.write(0x42)
        soc.uart.status.read()
    print(t)
    t.save("session.json")

The implementation uses the same :class:`RecordingMaster` that powers
``soc.attach_master(RecordingMaster(...))`` — one mechanism for both
"record this whole session" and "trace this block".
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ..masters.recording_replay import Event, RecordingMaster

if TYPE_CHECKING:
    from ..masters.base import MasterBase

__all__ = [
    "Trace",
    "attach_trace",
]


class Trace:
    """Live view onto a :class:`RecordingMaster`'s event log.

    The trace shares its event list with the recorder, so events appear
    in :attr:`events` as transactions occur. After the trace's context
    manager exits, the recorder is decoupled from the SoC but the
    event list remains.

    Methods mirror :class:`RecordingMaster` for the bits users need:
    :meth:`save` writes the log to disk, ``str(t)`` pretty-prints the
    captured transactions, and :func:`len` is the transaction count.
    """

    __slots__ = ("_recorder",)

    def __init__(self, recorder: RecordingMaster) -> None:
        self._recorder = recorder

    @property
    def events(self) -> list[Event]:
        """The list of recorded events. Mutating the list is not part
        of the contract; treat it as read-only."""
        return self._recorder.events

    def __len__(self) -> int:
        return len(self._recorder.events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._recorder.events)

    def save(self, path: str | Path) -> None:
        """Write the captured events to ``path``.

        Format follows :meth:`RecordingMaster.save`: ``.ndjson`` /
        ``.jsonl`` writes one event per line; anything else writes a
        single JSON array.
        """
        self._recorder.save(path)

    def __str__(self) -> str:
        events = self._recorder.events
        n = len(events)
        # Width in bytes is per-op; total bytes is the sum so callers
        # can spot "1k transactions, 4kB" at a glance.
        total_bytes = sum(e["width"] for e in events)
        plural = "transaction" if n == 1 else "transactions"
        lines = [f"{n} {plural}, {total_bytes} bytes"]
        for e in events:
            op = e["op"]
            addr = e["address"]
            value = e["value"]
            width = e["width"]
            hex_value = f"0x{value:0{width * 2}x}" if width else f"0x{value:x}"
            if op == "read":
                lines.append(f"  rd  @0x{addr:08x}  -> {hex_value}")
            elif op == "write":
                lines.append(f"  wr  @0x{addr:08x}  {hex_value}")
            else:
                lines.append(f"  {op:<3} @0x{addr:08x}  {hex_value}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<Trace events={len(self._recorder.events)}>"


def _trace_context_manager(
    soc: object,
    file: str | Path | None = None,
) -> Callable[..., AbstractContextManager[Trace]]:
    """Build the context-manager callable that becomes ``soc.trace``.

    The returned callable supports two usage shapes:

    * ``with soc.trace() as t: ...`` — the canonical form. Swaps the
      master for a :class:`RecordingMaster` for the duration of the
      block.
    * ``soc.trace(file=...)`` — same, but stream events to ``file``
      while the block runs (useful for huge sessions).
    """

    @contextmanager
    def trace(file: str | Path | None = file) -> Iterator[Trace]:
        # Capture the current master, wrap it, and put the recorder
        # back in place. We restore on exit, even if the body raised,
        # so a flaky test never leaves a recorder dangling on the SoC.
        original = _get_master(soc)
        if original is None:
            raise RuntimeError(
                "soc.trace() requires a master to be attached; "
                "call soc.attach_master(...) first"
            )
        recorder = RecordingMaster(original, file=file)
        _set_master(soc, recorder)
        try:
            yield Trace(recorder)
        finally:
            _set_master(soc, original)
            recorder.close()

    return trace


def attach_trace(soc: object) -> None:
    """Attach the ``trace()`` helper to ``soc``.

    This is the integration seam used by :func:`register_post_create`
    in the runtime registry — it mutates ``soc`` in place so the
    documented ``with soc.trace() as t:`` shape becomes available
    without subclassing the generated SoC type.

    Calling this twice on the same SoC is a no-op.
    """
    existing = getattr(soc, "trace", None)
    if existing is not None and getattr(existing, "__peakrdl_trace__", False):
        return

    cm = _trace_context_manager(soc)
    cm.__peakrdl_trace__ = True  # type: ignore[attr-defined]
    # Bind to the instance directly. Using a plain attribute assignment
    # rather than method-on-class keeps the helper's lifetime tied to
    # the SoC instance; multiple SoCs in one process don't share state.
    soc.trace = cm  # type: ignore[attr-defined]


def _get_master(soc: object) -> MasterBase | None:
    """Pull the active master out of ``soc``.

    Generated SoC objects expose the master through different shapes
    depending on the binding's history (``soc.master``, ``soc._master``,
    ``soc.get_master()``). Probe in order and return the first hit so
    sibling units don't have to know which surface their version of
    the binding uses.
    """
    for attr in ("master", "_master"):
        m = getattr(soc, attr, None)
        if m is not None:
            return m
    getter = getattr(soc, "get_master", None)
    if callable(getter):
        return getter()
    return None


def _set_master(soc: object, master: MasterBase | None) -> None:
    """Inverse of :func:`_get_master`. Probe in the same order."""
    setter = getattr(soc, "attach_master", None)
    if callable(setter):
        setter(master)
        return
    if hasattr(soc, "master"):
        soc.master = master  # type: ignore[attr-defined]
        return
    if hasattr(soc, "_master"):
        soc._master = master  # type: ignore[attr-defined]
        return
    raise RuntimeError(
        "could not assign master back to SoC: no attach_master / master / _master"
    )


# ---------------------------------------------------------------------------
# Registry wiring (sibling-dep: Unit 1's runtime/_registry).
#
# When the registry seam is present we register ``attach_trace`` as a
# post-create hook so every ``MySoc.create()`` automatically gains the
# ``soc.trace()`` helper. When it isn't (this unit can land before
# Unit 1), the import quietly fails and callers can still use
# ``attach_trace(soc)`` explicitly.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - depends on Unit 1 landing order
    from . import _registry  # type: ignore[attr-defined]
except ImportError:
    _registry = None  # type: ignore[assignment]

if _registry is not None and hasattr(_registry, "register_post_create"):
    _registry.register_post_create(attach_trace)
