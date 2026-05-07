"""Reified bus transactions and batched-write context manager.

Implements the API described in ``docs/IDEAL_API_SKETCH.md`` §13.2:

* :class:`Read`, :class:`Write`, :class:`Burst` — frozen dataclasses that
  describe a single bus operation. Cheap to construct, hashable, and
  intentionally light on validation so building lists of thousands of them
  in a tight loop stays fast.
* :func:`execute` — dispatches a sequence of those transactions against a
  master, using ``read_many`` / ``write_many`` where the master provides
  them. Returns the values produced by reads only; writes do not
  contribute to the result list.
* :func:`batch` — context manager that opens a cross-register
  ``soc.transaction()`` so every write inside the ``with`` block is
  staged and flushed in a single ``master.write_many`` call on exit.

The module also wires itself into the runtime registry:

* ``register_master_extension`` attaches :func:`execute` as a bound
  ``master.execute(txns)`` method on every wrapped/extended master.
* ``register_post_create`` attaches :func:`batch` as a bound
  ``soc.batch()`` method on every freshly created SoC.

Both attachments are best-effort: pybind11-generated classes without
``py::dynamic_attr()`` reject ``setattr`` and we skip silently. Pure
Python masters/SoCs (or wrapped ones) get the convenience methods.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from ..masters.base import AccessOp
from . import _registry

__all__ = [
    "Burst",
    "Read",
    "Write",
    "batch",
    "execute",
]

logger = logging.getLogger("peakrdl_pybind11.runtime.transactions")


# ---------------------------------------------------------------------------
# Reified transaction dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Read:
    """A single bus read.

    ``addr`` is an absolute byte address. ``width`` is the access width in
    bytes (default 4 for the common 32-bit register case). The dataclass
    is frozen so a list of ``Read`` instances is safe to share across
    threads or to use as dict keys.
    """

    addr: int
    width: int = 4


@dataclass(frozen=True, slots=True)
class Write:
    """A single bus write.

    ``value`` is interpreted in register-space (no field shifting); the
    master receives ``(addr, value, width)`` exactly as constructed.
    """

    addr: int
    value: int
    width: int = 4


@dataclass(frozen=True, slots=True)
class Burst:
    """An N-element burst at consecutive ``width``-byte addresses.

    ``op`` selects the direction:

    * ``"read"`` issues ``count`` reads starting at ``addr``; ``values``
      must be ``None``.
    * ``"write"`` issues ``count`` writes; ``values`` must be a sequence
      of ``count`` integers (one per element).

    Validation runs in ``__post_init__`` so building an invalid burst
    fails at construction time rather than at execute().
    """

    addr: int
    count: int
    op: Literal["read", "write"]
    values: list[int] | None = None
    width: int = 4

    def __post_init__(self) -> None:
        if self.op not in ("read", "write"):
            raise ValueError(
                f"Burst.op must be 'read' or 'write', got {self.op!r}"
            )
        if self.count < 0:
            raise ValueError(f"Burst.count must be >= 0, got {self.count}")
        if self.op == "read":
            if self.values is not None:
                raise ValueError("Burst(op='read') must not specify values")
        else:  # "write"
            if self.values is None:
                raise ValueError("Burst(op='write') requires values=...")
            if len(self.values) != self.count:
                raise ValueError(
                    f"Burst(op='write') values length {len(self.values)} "
                    f"does not match count {self.count}"
                )


# ---------------------------------------------------------------------------
# Execute: dispatch a sequence of transactions on a master
# ---------------------------------------------------------------------------


def _burst_ops(burst: Burst) -> list[AccessOp]:
    """Materialize a Burst into the per-element :class:`AccessOp` list."""
    if burst.op == "read":
        return [
            AccessOp(address=burst.addr + i * burst.width, value=0, width=burst.width)
            for i in range(burst.count)
        ]
    # op == "write"; values is non-None per Burst.__post_init__.
    assert burst.values is not None
    return [
        AccessOp(address=burst.addr + i * burst.width, value=v, width=burst.width)
        for i, v in enumerate(burst.values)
    ]


def execute(master: Any, txns: Sequence[Read | Write | Burst]) -> list[int]:
    """Execute ``txns`` against ``master``; return values from reads only.

    Each :class:`Read` produces one value in the result list; each
    :class:`Write` produces nothing; a :class:`Burst` with ``op="read"``
    contributes ``count`` values; a :class:`Burst` with ``op="write"``
    contributes nothing.

    When the master exposes ``read_many`` / ``write_many`` (Unit 1's
    batch interface), bursts use that single boundary cross. Single
    Read/Write fall back to ``master.read`` / ``master.write`` directly
    so the latency for one-off scripted txns stays minimal.

    Transaction order is preserved: writes execute strictly before any
    later reads, so the ``Write(addr, v); Read(addr)`` idiom returns
    ``[v]`` even on masters that buffer writes asynchronously.
    """
    out: list[int] = []
    read_many = getattr(master, "read_many", None)
    write_many = getattr(master, "write_many", None)

    for txn in txns:
        if isinstance(txn, Read):
            out.append(int(master.read(txn.addr, txn.width)))
        elif isinstance(txn, Write):
            master.write(txn.addr, txn.value, txn.width)
        elif isinstance(txn, Burst):
            ops = _burst_ops(txn)
            if txn.op == "read":
                if read_many is not None:
                    out.extend(int(v) for v in read_many(ops))
                else:
                    out.extend(int(master.read(op.address, op.width)) for op in ops)
            else:  # "write"
                if write_many is not None:
                    write_many(ops)
                else:
                    for op in ops:
                        master.write(op.address, op.value, op.width)
        else:
            raise TypeError(
                f"execute(): unsupported transaction type {type(txn).__name__!r}"
            )
    return out


# ---------------------------------------------------------------------------
# Batch: cross-register write-staging context manager
# ---------------------------------------------------------------------------


@contextmanager
def batch(soc: Any) -> Iterator[Any]:
    """Open a write-staging context that flushes on exit.

    Equivalent to::

        with soc.transaction():
            yield soc

    Inside the ``with`` block, every register write through ``soc``
    accumulates in the active transaction; on clean exit the whole batch
    is dispatched via a single ``master.write_many`` call. On exception
    the queue is discarded (matches ``soc.transaction()`` semantics).

    Yields ``soc`` itself — ``b is soc`` — because the existing
    transaction machinery in C++ already routes writes through the
    active transaction without needing a proxy tree.
    """
    transaction = getattr(soc, "transaction", None)
    if transaction is None:
        raise AttributeError(
            "soc.batch() requires soc.transaction() — attach a master first"
        )
    with transaction():
        yield soc


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def _try_setattr(obj: Any, name: str, value: Any) -> None:
    """``setattr`` that swallows the rejection from pybind11 classes.

    pybind11 classes without ``py::dynamic_attr()`` raise ``AttributeError``
    on ``setattr``; we treat that as "nothing to attach here" so the
    registry hook stays a no-op for raw C++ master/SoC objects.
    """
    try:
        setattr(obj, name, value)
    except (AttributeError, TypeError) as exc:
        logger.debug("could not attach %r to %r: %s", name, type(obj).__name__, exc)


@_registry.register_master_extension
def _attach_execute_to_master(master: Any) -> None:
    """Attach a bound ``execute`` method to ``master``."""
    if hasattr(master, "execute") and callable(master.execute):
        return  # already attached or natively provided

    def _bound_execute(txns: Sequence[Read | Write | Burst]) -> list[int]:
        return execute(master, txns)

    _try_setattr(master, "execute", _bound_execute)


@_registry.register_post_create
def _attach_batch_to_soc(soc: Any) -> None:
    """Attach a bound ``batch`` method to ``soc``."""
    if hasattr(soc, "batch") and callable(soc.batch):
        return  # already attached

    def _bound_batch() -> Any:
        return batch(soc)

    _try_setattr(soc, "batch", _bound_batch)
