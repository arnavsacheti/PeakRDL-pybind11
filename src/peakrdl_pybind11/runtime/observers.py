"""Unified read/write observer hook chain.

Implements §16.2 of ``docs/IDEAL_API_SKETCH.md``: every read and every write
the runtime performs against a master is dispatched to a single chain of
subscribers. Coverage tools, audit logs, assertion frameworks, and the live
notebook widget all subscribe to the same stream, so the master itself stays
unwrapped and the chain stays composable.

Hooks fire **after** the master operation completes, so the resulting
``value`` is always available on the :class:`Event`.

Performance note
----------------
Hooks add per-transaction Python overhead: each registered callback runs on
every matching event. For tight inner loops -- bulk memory sweeps, large
register arrays, performance benchmarks -- prefer
``Snapshot.diff()``-style state capture, which performs O(N) reads with O(1)
Python dispatch instead of O(N) hook invocations.

Observers cost nothing until at least one is registered: with no hooks the
master wrapper skips the :class:`Event` construction entirely after a single
truthiness check on the chain's hook list.
"""

from __future__ import annotations

import fnmatch
import time
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Master(Protocol):
    """Subset of :class:`MasterBase` exercised by :func:`register_master_extension`.

    Declared here as a structural protocol so the wrapper accepts any object
    that quacks like a master -- including the pybind11 ``MockMaster`` /
    ``CallbackMaster`` natives shipped in generated modules, which don't
    inherit from the Python base class.
    """

    def read(self, address: int, width: int) -> int: ...
    def write(self, address: int, value: int, width: int) -> None: ...


__all__ = [
    "CoverageReport",
    "Event",
    "ObserverChain",
    "ObserverScope",
    "register_master_extension",
    "register_post_create",
]


Op = Literal["read", "write"]
"""Operation kind carried on an :class:`Event`."""

Predicate = Callable[["Event"], bool]
"""Callable predicate accepted by ``where=``; returns ``True`` to accept."""

Where = str | Predicate | None
"""Filter spec accepted by :meth:`ObserverChain.add_read` / ``add_write``."""

Hook = Callable[["Event"], None]
"""Observer callback signature."""


@dataclass(frozen=True, slots=True)
class Event:
    """One read or write transaction observed on the bus.

    Attributes:
        path: Dotted RDL path of the node touched (e.g. ``"uart.control"``).
            Empty string when the runtime has no path information (e.g.
            address-only ops issued via the raw master interface).
        address: Absolute byte address of the underlying register.
        value: Value read from -- or written to -- ``address``.
        op: ``"read"`` or ``"write"``.
        timestamp: ``time.monotonic()`` snapshot taken just before dispatch.
    """

    path: str
    address: int
    value: int
    op: Op
    timestamp: float


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Summary returned by :meth:`ObserverScope.coverage_report`.

    Attributes:
        nodes_read: Set of unique paths that observed at least one read.
        nodes_written: Set of unique paths that observed at least one write.
        total_reads: Number of read events captured.
        total_writes: Number of write events captured.
        paths_by_frequency: ``[(path, count), ...]`` sorted with the busiest
            paths first. Counts include both reads and writes for that path.
    """

    nodes_read: frozenset[str]
    nodes_written: frozenset[str]
    total_reads: int
    total_writes: int
    paths_by_frequency: tuple[tuple[str, int], ...]

    def __str__(self) -> str:
        return (
            f"CoverageReport(reads={self.total_reads}, writes={self.total_writes}, "
            f"nodes_read={len(self.nodes_read)}, nodes_written={len(self.nodes_written)})"
        )


# A subscription is just (hook, predicate-or-None). Tuples avoid a class
# allocation on every add_read/add_write -- material when the chain runs
# inside a tight register loop.
_Subscription = tuple[Hook, "Predicate | None"]


def _coerce_predicate(where: Where) -> Predicate | None:
    """Turn a ``where=`` spec into a predicate, or ``None`` if unfiltered."""
    if where is None:
        return None
    if callable(where):
        return where  # type: ignore[return-value]
    if isinstance(where, str):
        pattern = where
        return lambda evt: fnmatch.fnmatchcase(evt.path, pattern)
    raise TypeError(
        f"where= must be a glob string, a callable, or None; got {type(where).__name__}"
    )


def _remove_first(hooks: list[_Subscription], fn: Hook) -> bool:
    """Remove the first subscription whose hook is ``fn``; report success."""
    for i, (hook, _) in enumerate(hooks):
        if hook is fn:
            del hooks[i]
            return True
    return False


class ObserverChain:
    """Registry of read/write observer callbacks.

    Hooks are stored in the order they are registered. Each hook may carry
    a ``where=`` filter; when set, the hook only fires on events whose
    ``path`` matches the filter. ``where`` accepts:

    * ``None`` (the default) -- match every event.
    * a glob string -- matched against ``Event.path`` with
      :func:`fnmatch.fnmatchcase`. Patterns like ``"uart.*"`` select the
      direct children of ``uart``; ``"uart.**"`` is *not* automatically
      recursive (use ``"uart.*"`` and rely on ``fnmatch`` semantics, or pass
      a callable for fully custom filtering).
    * a callable ``Event -> bool`` -- return ``True`` to fire.

    The chain is intentionally simple and synchronous: hooks run on the
    same thread as the read/write op, so a hook that raises will propagate
    out of the master call. Wrap your hooks if you need fault isolation.
    """

    __slots__ = ("_read_hooks", "_write_hooks")

    def __init__(self) -> None:
        self._read_hooks: list[_Subscription] = []
        self._write_hooks: list[_Subscription] = []

    # -- registration ----------------------------------------------------

    def add_read(self, fn: Hook, where: Where = None) -> Hook:
        """Register ``fn`` as a read observer.

        Returns the function unchanged, so this method works as a
        decorator. Pass ``where=`` (a glob string or predicate) to
        restrict the hook to matching events.
        """
        self._read_hooks.append((fn, _coerce_predicate(where)))
        return fn

    def add_write(self, fn: Hook, where: Where = None) -> Hook:
        """Register ``fn`` as a write observer. See :meth:`add_read`."""
        self._write_hooks.append((fn, _coerce_predicate(where)))
        return fn

    def remove_read(self, fn: Hook) -> bool:
        """Remove a previously-registered read observer.

        Returns ``True`` if a matching hook was removed, ``False`` if
        ``fn`` was not registered. Removes only the first occurrence so
        that a hook registered twice can be detached one subscription at
        a time.
        """
        return _remove_first(self._read_hooks, fn)

    def remove_write(self, fn: Hook) -> bool:
        """Remove a previously-registered write observer.

        See :meth:`remove_read` for return-value semantics.
        """
        return _remove_first(self._write_hooks, fn)

    def clear(self) -> None:
        """Drop every registered observer. Mainly useful in tests."""
        self._read_hooks.clear()
        self._write_hooks.clear()

    # -- introspection ---------------------------------------------------

    def __len__(self) -> int:
        return len(self._read_hooks) + len(self._write_hooks)

    def __bool__(self) -> bool:
        return bool(self._read_hooks or self._write_hooks)

    @property
    def read_hooks(self) -> tuple[Hook, ...]:
        """Tuple of registered read hooks, in registration order."""
        return tuple(fn for fn, _ in self._read_hooks)

    @property
    def write_hooks(self) -> tuple[Hook, ...]:
        """Tuple of registered write hooks, in registration order."""
        return tuple(fn for fn, _ in self._write_hooks)

    # -- dispatch (called from the master extension wrapper) -------------
    #
    # An exception raised by a hook stops the chain and propagates -- an
    # assertion-framework observer wants test failures to halt the run.

    def _dispatch_read(self, evt: Event) -> None:
        """Fan ``evt`` out to every matching read hook."""
        for fn, pred in self._read_hooks:
            if pred is None or pred(evt):
                fn(evt)

    def _dispatch_write(self, evt: Event) -> None:
        """Fan ``evt`` out to every matching write hook."""
        for fn, pred in self._write_hooks:
            if pred is None or pred(evt):
                fn(evt)

    # -- scoped capture --------------------------------------------------

    @contextmanager
    def observe(self) -> Iterator[ObserverScope]:
        """Capture every event for the duration of a ``with`` block.

        Equivalent to ``soc.observe()``. Yields a :class:`ObserverScope`
        whose ``events`` list is appended to as transactions occur. The
        scope's hooks are detached on exit, even if the block raises.
        """
        scope = ObserverScope()
        scope._attach(self)
        try:
            yield scope
        finally:
            scope._detach(self)


@dataclass(slots=True)
class ObserverScope:
    """Captures events in a ``with soc.observe() as obs:`` block.

    Returned by :meth:`ObserverChain.observe`. The ``events`` list grows
    in the order events fire; ``coverage_report()`` summarises the run.
    """

    events: list[Event] = field(default_factory=list)
    _hook: Hook | None = field(default=None, init=False, repr=False)

    def _attach(self, chain: ObserverChain) -> None:
        hook: Hook = self.events.append
        chain.add_read(hook)
        chain.add_write(hook)
        self._hook = hook

    def _detach(self, chain: ObserverChain) -> None:
        if self._hook is None:
            return
        chain.remove_read(self._hook)
        chain.remove_write(self._hook)
        self._hook = None

    def coverage_report(self) -> CoverageReport:
        """Summarise the events captured so far.

        Multiple events for the same path (e.g. several reads of the same
        register) collapse to a single entry in ``nodes_read`` /
        ``nodes_written`` but are counted separately for ``total_reads`` /
        ``total_writes`` and ``paths_by_frequency``.
        """
        nodes_read: set[str] = set()
        nodes_written: set[str] = set()
        total_reads = 0
        total_writes = 0
        counts: Counter[str] = Counter()
        for evt in self.events:
            counts[evt.path] += 1
            if evt.op == "read":
                nodes_read.add(evt.path)
                total_reads += 1
            else:
                nodes_written.add(evt.path)
                total_writes += 1
        # Sort by count descending, breaking ties alphabetically so the
        # report is reproducible across runs.
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return CoverageReport(
            nodes_read=frozenset(nodes_read),
            nodes_written=frozenset(nodes_written),
            total_reads=total_reads,
            total_writes=total_writes,
            paths_by_frequency=tuple(ordered),
        )


# ---------------------------------------------------------------------------
# Wiring helpers (the seam used by Unit 1's `_registry`)
# ---------------------------------------------------------------------------
#
# The runtime registry (sibling Unit 1) calls ``register_master_extension``
# to wrap the bound master's ``read`` / ``write`` so every transaction goes
# through ``ObserverChain._dispatch_read`` / ``_dispatch_write`` after the
# master returns. The chain itself is attached to the SoC via
# ``register_post_create``, exposed as ``soc.observers`` and surfaced to the
# context-manager API as ``soc.observe()``.
#
# Both helpers are pure-Python and self-contained: they don't depend on the
# generated module's internals and can be invoked directly in tests.


def register_master_extension(
    master: _Master,
    chain: ObserverChain,
    *,
    path_resolver: Callable[[int, Op], str] | None = None,
) -> _Master:
    """Wrap ``master.read`` / ``master.write`` to dispatch through ``chain``.

    The wrapper preserves the master's existing behavior -- including the
    return value of ``read`` -- and only fires the chain on the way out, so
    a hook always sees the value the master actually returned (read) or was
    about to commit (write).

    ``path_resolver`` translates an ``(address, op)`` tuple to a dotted
    path. The runtime registry installs a resolver that consults the
    address-routing table; tests can pass a dict-backed resolver or
    ``None`` (paths are then the empty string).

    Returns the same ``master`` argument, mutated in place. The wrapper
    is idempotent: calling ``register_master_extension`` again with the
    same ``master`` and ``chain`` simply rebinds the methods, leaving a
    single layer of dispatch.
    """
    # Stash the originals on the first call so a second call rebinds onto
    # the unwrapped methods rather than piling up wrappers.
    inner_read = getattr(master, "_observer_inner_read", master.read)
    inner_write = getattr(master, "_observer_inner_write", master.write)
    resolve: Callable[[int, Op], str] = path_resolver or (lambda _addr, _op: "")

    def read(address: int, width: int) -> int:
        value = inner_read(address, width)
        if chain._read_hooks:
            chain._dispatch_read(
                Event(resolve(address, "read"), address, int(value), "read", time.monotonic())
            )
        return value

    def write(address: int, value: int, width: int) -> None:
        inner_write(address, value, width)
        if chain._write_hooks:
            chain._dispatch_write(
                Event(resolve(address, "write"), address, int(value), "write", time.monotonic())
            )

    master._observer_inner_read = inner_read  # type: ignore[attr-defined]
    master._observer_inner_write = inner_write  # type: ignore[attr-defined]
    master.read = read  # type: ignore[method-assign]
    master.write = write  # type: ignore[method-assign]
    return master


def register_post_create(soc: object, chain: ObserverChain | None = None) -> ObserverChain:
    """Attach an :class:`ObserverChain` to ``soc`` and wire ``soc.observe``.

    Sets ``soc.observers`` to ``chain`` (created if not provided) and binds
    ``soc.observe`` to a zero-argument callable returning the chain's
    :meth:`ObserverChain.observe` context manager. Returns the chain so
    callers can keep a reference.

    Idempotent: re-attaching is a no-op when ``soc.observers is chain``.
    """
    existing = getattr(soc, "observers", None)
    if chain is None:
        chain = existing if isinstance(existing, ObserverChain) else ObserverChain()
    if existing is chain and getattr(soc, "observe", None) is not None:
        return chain
    soc.observers = chain  # type: ignore[attr-defined]
    soc.observe = chain.observe  # type: ignore[attr-defined,method-assign]
    return chain
