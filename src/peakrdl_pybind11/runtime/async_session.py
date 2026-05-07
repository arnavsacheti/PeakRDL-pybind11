"""Async dual surface (Unit 22).

Implements the *async session* described in ``docs/IDEAL_API_SKETCH.md``
§13.8. The sketch (§22.3) explicitly resolves the sync-vs-async tradeoff
in favor of *sync first*: there are **no native async transports**. Async
is therefore a thin wrapper around the existing sync surface, dispatching
each call to a :class:`concurrent.futures.ThreadPoolExecutor` via
``loop.run_in_executor``.

Design notes
------------
* :class:`AsyncSession` is itself the dual surface. Entering the
  ``async with`` returns the session, and attribute access lazily mirrors
  the wrapped SoC's tree as :class:`_AsyncNode` proxies. Lazy is the spec
  -- a 10k-node design must not pay for an unused subtree.
* Each proxy exposes ``aread`` / ``awrite`` / ``amodify`` / ``aiowait``
  async methods. They forward ``*args, **kwargs`` to their sync
  counterparts and return whatever the sync call returns.
* Executor ownership is tracked: a session that *creates* its executor
  shuts it down on ``__aexit__``; one that *received* an executor leaves
  it alone (the caller's lifecycle wins).
* ``register_post_create(soc, ...)`` is the seam Unit 1 uses to install
  the feature on every generated SoC. Calling it binds
  ``soc.async_session`` to a zero-argument callable returning a fresh
  :class:`AsyncSession`. Idempotent.

The implementation is intentionally pure-Python and self-contained: it
doesn't depend on the generated module's internals and can be exercised
with any duck-typed object that exposes ``read`` / ``write`` / ``modify``
/ ``wait`` callables on its accessor nodes.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

__all__ = [
    "AsyncSession",
    "register_post_create",
]


# Async-to-sync method mapping for the four canonical bus operations.
# Order is preserved for ``__dir__``-style introspection so REPL completion
# lists the methods in their familiar (read/write/modify/wait) sequence.
_ASYNC_TO_SYNC: dict[str, str] = {
    "aread": "read",
    "awrite": "write",
    "amodify": "modify",
    "aiowait": "wait",
}


class AsyncSession:
    """Async dual of a synchronous SoC.

    Wraps an existing ``soc`` and exposes a parallel namespace where every
    node has ``aread`` / ``awrite`` / ``amodify`` / ``aiowait`` async
    methods. Calls dispatch to a thread-pool executor; concurrency is
    therefore bounded by the executor's worker count (default 1).

    Use as an async context manager::

        async with soc.async_session() as s:
            v = await s.uart.control.aread()
            await s.uart.control.awrite(0x42)

    Or take ownership of the executor lifecycle yourself::

        executor = ThreadPoolExecutor(max_workers=4)
        async with soc.async_session(executor=executor) as s:
            ...
        # caller owns ``executor``; AsyncSession does not shut it down

    The session lazily mirrors the SoC's tree -- accessing
    ``s.uart.control`` walks one attribute deep on the wrapped SoC and
    wraps the result in an :class:`_AsyncNode`. No traversal happens up
    front, and only the path you touch becomes a proxy.
    """

    __slots__ = ("_executor", "_node_cache", "_owns_executor", "_soc")

    def __init__(
        self,
        soc: object,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._soc = soc
        if executor is None:
            # Default to a single worker; the sketch §22.3 specifies that
            # the dual surface is "sync wrapped in an executor" -- the
            # primary win is async ergonomics, not parallelism. Callers
            # that want concurrent bus ops pass their own pool.
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="peakrdl-async-session")
            self._owns_executor = True
        else:
            self._executor = executor
            self._owns_executor = False
        self._node_cache: dict[str, _AsyncNode] = {}

    # ------------------------------------------------------------------
    # Async context manager protocol
    # ------------------------------------------------------------------

    async def __aenter__(self) -> AsyncSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        if self._owns_executor:
            # Hand off the blocking shutdown to a thread so we don't stall
            # the event loop on an active worker. ``wait=True`` ensures
            # any in-flight bus op finishes cleanly before the executor
            # disappears.
            loop = asyncio.get_running_loop()
            executor = self._executor
            self._owns_executor = False  # one-shot guard; idempotent close
            await loop.run_in_executor(None, lambda: executor.shutdown(wait=True))

    # ------------------------------------------------------------------
    # Lazy mirror of the SoC's tree
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        # ``Any`` because the dual mirrors a generated tree whose
        # accessors aren't visible to the type checker at this level;
        # consumers wanting strict typing use the generated ``.pyi``.
        if name.startswith("_"):
            raise AttributeError(name)
        cache = self._node_cache
        cached = cache.get(name)
        if cached is not None:
            return cached
        try:
            sync_value = getattr(self._soc, name)
        except AttributeError:
            raise AttributeError(f"{type(self._soc).__name__!s} has no attribute {name!r}") from None
        # Wrap regfiles/addrmaps even when they don't themselves expose
        # ``read`` -- they contain registers that do. Primitives on the
        # SoC (version strings, scalar config) flow through so consumers
        # can read them without await ceremony.
        if _is_primitive(sync_value):
            return sync_value
        proxy = _AsyncNode(sync_value, self._executor)
        cache[name] = proxy
        return proxy

    def __dir__(self) -> list[str]:
        # Mirrors the SoC for tab-completion. Public attrs only -- the
        # dual surface should not advertise private state.
        sync_attrs = [a for a in dir(self._soc) if not a.startswith("_")]
        own = [a for a in type(self).__dict__ if not a.startswith("_")]
        return sorted(set(sync_attrs) | set(own))

    @property
    def soc(self) -> Any:  # noqa: ANN401
        """The wrapped synchronous SoC. Useful for falling back to sync."""
        return self._soc

    @property
    def executor(self) -> ThreadPoolExecutor:
        """The executor backing async dispatch."""
        return self._executor


class _AsyncNode:
    """Lazy async proxy around a sync accessor node.

    Holds a strong reference to the underlying ``sync_node`` and the
    executor. Attribute access produces child proxies on demand, sharing
    the same executor; method access produces async wrappers for the four
    canonical bus operations (``aread`` / ``awrite`` / ``amodify`` /
    ``aiowait``) and falls back to plain attribute lookup for everything
    else (so users can still inspect ``.path``, ``.address``, etc.).
    """

    __slots__ = ("_child_cache", "_executor", "_forwarder_cache", "_sync_node")

    def __init__(self, sync_node: object, executor: ThreadPoolExecutor) -> None:
        self._sync_node = sync_node
        self._executor = executor
        self._child_cache: dict[str, _AsyncNode] = {}
        # Forwarders are stable per (node, async-name): caching lets a
        # tight poll loop reuse the same closure rather than rebuild it
        # on every ``s.uart.control.aread()`` access.
        self._forwarder_cache: dict[str, Callable[..., Any]] = {}

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        if name.startswith("_"):
            # Pickling, ``hasattr('__getstate__')``, and similar dunder
            # probes must not spin up bogus proxies.
            raise AttributeError(name)

        sync_name = _ASYNC_TO_SYNC.get(name)
        if sync_name is not None:
            return self._async_forwarder(name, sync_name)

        cache = self._child_cache
        cached = cache.get(name)
        if cached is not None:
            return cached

        try:
            value = getattr(self._sync_node, name)
        except AttributeError:
            raise AttributeError(f"{type(self._sync_node).__name__!s} has no attribute {name!r}") from None

        if _is_node_like(value):
            proxy = _AsyncNode(value, self._executor)
            cache[name] = proxy
            return proxy
        return value

    def __dir__(self) -> list[str]:
        sync_attrs = [a for a in dir(self._sync_node) if not a.startswith("_")]
        return sorted(set(sync_attrs) | _ASYNC_TO_SYNC.keys())

    @property
    def sync(self) -> Any:  # noqa: ANN401
        """The wrapped sync node, for callers that want to drop back."""
        return self._sync_node

    # ------------------------------------------------------------------
    # Async dispatch
    # ------------------------------------------------------------------

    def _async_forwarder(self, async_name: str, sync_name: str) -> Callable[..., Any]:
        cached = self._forwarder_cache.get(async_name)
        if cached is not None:
            return cached
        forwarder = self._build_async_forwarder(async_name, sync_name)
        self._forwarder_cache[async_name] = forwarder
        return forwarder

    def _build_async_forwarder(self, async_name: str, sync_name: str) -> Callable[..., Any]:
        sync_node = self._sync_node
        executor = self._executor

        try:
            sync_callable = getattr(sync_node, sync_name)
        except AttributeError:
            # Mirrors how the sync surface would behave if a node simply
            # doesn't support an op (e.g. a write-only register has no
            # ``read``).
            raise AttributeError(
                f"{type(sync_node).__name__!s}.{async_name}: underlying node has no {sync_name!r} method"
            ) from None

        if not callable(sync_callable):
            raise TypeError(
                f"{type(sync_node).__name__!s}.{sync_name} is not callable; cannot expose as {async_name!r}"
            )

        async def _forward(*args: object, **kwargs: object) -> Any:  # noqa: ANN401
            # ``Any`` return because the wrapped op may produce a
            # ``RegisterValue``, an int, an ndarray, ``None`` for writes,
            # or any caller-defined value.
            loop = asyncio.get_running_loop()

            def _call() -> object:
                return sync_callable(*args, **kwargs)

            return await loop.run_in_executor(executor, _call)

        # Stamp metadata so ``inspect.signature`` and ``help()`` see the
        # underlying op, not the generic forwarder.
        _forward.__name__ = async_name
        try:
            _forward.__signature__ = inspect.signature(sync_callable)  # type: ignore[attr-defined]
        except (TypeError, ValueError):
            # Some C-extension callables don't expose a signature.
            pass
        sync_doc = getattr(sync_callable, "__doc__", None)
        if sync_doc:
            _forward.__doc__ = (
                f"Async wrapper around {type(sync_node).__name__}."
                f"{sync_name}; runs on the session's thread pool.\n\n"
                f"{sync_doc}"
            )
        return _forward


_PRIMITIVE_TYPES: tuple[type, ...] = (
    int,
    float,
    str,
    bytes,
    bytearray,
    bool,
    complex,
    list,
    tuple,
    dict,
    set,
    frozenset,
)


def _is_primitive(value: object) -> bool:
    """Return ``True`` if ``value`` is a leaf attribute the dual should
    return as-is rather than wrap.

    Used at the *session* layer, where the SoC may carry both registers
    (which we want to wrap) and bare configuration (path strings,
    version ints, callables). Anything that satisfies this predicate
    flows through unchanged.
    """
    if value is None:
        return True
    if isinstance(value, _PRIMITIVE_TYPES):
        return True
    if callable(value):
        return True
    return False


def _is_node_like(value: object) -> bool:
    """Return ``True`` if ``value`` should be wrapped as a child proxy.

    Used at the *node* layer, where we want a stricter test: an
    attribute that exposes one of the canonical bus methods is a child
    accessor; anything else (a path string, an int address, a metadata
    dict) flows through unchanged.

    Errs on the side of *not* wrapping: a false negative just means the
    attribute is returned as-is, which is the safe behavior for anything
    the runtime doesn't recognize.
    """
    if _is_primitive(value):
        return False
    # Any of the canonical bus methods is enough to flag a node-like
    # object. Keeps the heuristic tight without enumerating every
    # generated class type.
    return any(callable(getattr(value, sync_name, None)) for sync_name in _ASYNC_TO_SYNC.values())


# ---------------------------------------------------------------------------
# Wiring helper (the seam used by Unit 1's ``_registry``)
# ---------------------------------------------------------------------------
#
# The runtime registry (sibling Unit 1) calls ``register_post_create`` on
# every generated SoC during construction. This module's contribution is
# a single attribute: ``soc.async_session`` -- a zero-argument callable
# that returns a fresh :class:`AsyncSession`. Sessions are short-lived
# (the executor lifecycle ties to ``async with``), so we don't cache on
# the SoC; each call returns a new session.
#
# The helper is pure-Python and self-contained: it doesn't import from
# ``_registry``, so this module compiles cleanly on branches where Unit
# 1 has not yet landed.


def register_post_create(soc: object) -> object:
    """Attach :class:`AsyncSession` factory to ``soc.async_session``.

    Sets ``soc.async_session`` to a zero-argument callable that returns a
    fresh :class:`AsyncSession` wrapping ``soc``. Returns ``soc`` for
    chaining.

    Idempotent: re-registering rebinds the factory but keeps existing
    sessions alive. Generated modules call this once per SoC instance
    via Unit 1's seam; tests can invoke it directly on any duck-typed
    object.
    """

    def _factory(executor: ThreadPoolExecutor | None = None) -> AsyncSession:
        return AsyncSession(soc, executor=executor)

    _factory.__name__ = "async_session"
    _factory.__qualname__ = f"{type(soc).__name__}.async_session"
    _factory.__doc__ = (
        "Return a new :class:`AsyncSession` wrapping this SoC.\n\n"
        "Use as an async context manager::\n\n"
        "    async with soc.async_session() as s:\n"
        "        v = await s.uart.control.aread()\n"
    )
    soc.async_session = _factory  # type: ignore[attr-defined]
    return soc
