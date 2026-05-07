"""Bus policies — barriers, cache, retry — attached to masters as a unit.

This module implements §13.3 (barriers), §13.4 (cache) and §13.5 (retry)
of ``docs/IDEAL_API_SKETCH.md``. The three policies share a single seam
(:func:`register_master_extension` from :mod:`._registry`) so they wrap
``MasterBase.read`` / ``MasterBase.write`` once, in a defined order, and
without each policy having to know about the others.

Wrapper order (innermost first, which is also the order the user-facing
``read``/``write`` traverses):

  user call → cache lookup → retry loop → barrier ordering → master.read

* The cache short-circuits a read if a fresh value is on hand.
* The retry loop catches transport errors from the underlying master and
  re-issues the call, draining a barrier between attempts.
* The barrier policy fences before reads/writes per its mode and tracks
  the "last op was a write" bit so ``auto`` mode can fence read-after-write.

Each policy exposes a small object users compose against the SoC tree:

* :class:`BarrierPolicy` — ``set_barrier_policy``, ``barrier`` (per-master,
  per-subtree, SoC-wide), ``global_barrier``.
* :class:`CachePolicy` — ``cache_for``, ``invalidate_cache``,
  ``cached(window=...)`` block-scope.
* :class:`RetryPolicy` — ``set_retry_policy``, ``on_disconnect``, plus a
  per-call ``retries=`` override on ``read`` / ``write``.

The umbrella generated SoC binds these onto ``soc``, ``soc.master``, and
each register node so the public surface in the sketch resolves to the
methods on these policies.
"""

from __future__ import annotations

import builtins
import logging
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock
from typing import Literal

from ..masters.base import MasterBase
from ._errors import BusError, NotSupportedError, TransportError
from ._registry import register_master_extension

_logger = logging.getLogger("peakrdl_pybind11.runtime.bus_policies")


# ---------------------------------------------------------------------------
# Per-call override carrier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallOverride:
    """Per-call overrides plumbed through wrapped read/write.

    Bus policies accept keyword overrides on individual calls (e.g.
    ``reg.read(retries=10)``). The register-node layer collects those into a
    :class:`CallOverride` and forwards it to the wrapped master through the
    ``_pe_override`` keyword. Policy wrappers pop it off; native masters
    never see it.
    """

    retries: int | None = None
    bypass_cache: bool = False


# ---------------------------------------------------------------------------
# Barrier policy (§13.3)
# ---------------------------------------------------------------------------


BarrierMode = Literal["auto", "none", "strict", "auto-global"]
_BARRIER_MODES: tuple[str, ...] = ("auto", "none", "strict", "auto-global")


class BarrierPolicy:
    """Per-master barrier state and policy.

    The default policy is ``"auto"``: a barrier fires before any read that
    follows a write *on the same master*. ``"strict"`` fences before every
    transaction; ``"none"`` opts out (and ``master.barrier()`` only runs on
    explicit user calls); ``"auto-global"`` extends auto-mode across every
    master attached to the parent context.

    A ``BarrierPolicy`` keeps track of the master(s) it's attached to and
    a "last op was a write" bit; the SoC layer instantiates one per master
    (and a façade that fans out to every attached master for the SoC-wide
    surface).
    """

    def __init__(self, master: MasterBase) -> None:
        self.master = master
        self.mode: BarrierMode = "auto"
        # Peers participate in scope="all" / auto-global barriers; the SoC
        # layer keeps these consistent across attach_master calls.
        self._peers: list[MasterBase] = [master]
        # True if any write has fired since the last barrier on this master.
        self._dirty: bool = False
        self._lock = RLock()

    # -- Configuration -----------------------------------------------------

    def set_mode(self, mode: BarrierMode) -> None:
        if mode not in _BARRIER_MODES:
            raise ValueError(f"barrier policy must be one of {_BARRIER_MODES!r}, got {mode!r}")
        self.mode = mode

    def attach_peer(self, master: MasterBase) -> None:
        """Add ``master`` to the peer set used by ``scope='all'`` barriers."""
        with self._lock:
            if master not in self._peers:
                self._peers.append(master)

    # -- User-facing surface ----------------------------------------------

    def barrier(self, scope: Literal["self", "all"] = "self") -> None:
        """Drain in-flight writes.

        Args:
            scope: ``"self"`` (default) only barriers this master;
                ``"all"`` fences every peer master in turn.
        """
        with self._lock:
            if scope == "all":
                for m in self._peers:
                    self._do_barrier(m)
            else:
                self._do_barrier(self.master)
            self._dirty = False

    def global_barrier(self) -> None:
        """Alias for :meth:`barrier` with ``scope='all'`` — reads better in scripts."""
        self.barrier(scope="all")

    # -- Internal hooks invoked by the read/write wrappers ----------------

    def before_read(self) -> None:
        """Called by the wrapped master before issuing a read."""
        if self.mode == "strict":
            self._do_barrier(self.master)
        elif self.mode == "auto" and self._dirty:
            self._do_barrier(self.master)
            self._dirty = False
        elif self.mode == "auto-global" and self._dirty:
            for m in self._peers:
                self._do_barrier(m)
            self._dirty = False

    def before_write(self) -> None:
        """Called by the wrapped master before issuing a write."""
        if self.mode == "strict":
            self._do_barrier(self.master)

    def after_write(self) -> None:
        """Called by the wrapped master immediately after a successful write."""
        if self.mode in ("auto", "auto-global"):
            self._dirty = True

    @staticmethod
    def _do_barrier(master: MasterBase) -> None:
        # `barrier` is a no-op default on MasterBase; backends that buffer
        # writes override it. We tolerate masters that pre-date the seam
        # by checking attribute presence.
        fn = getattr(master, "barrier", None)
        if callable(fn):
            fn()


# ---------------------------------------------------------------------------
# Cache policy (§13.4)
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    """Per-address cache slot."""

    ttl: float
    value: int = 0
    expiry: float = 0.0  # monotonic timestamp; 0 means "no fresh value"


class CachePolicy:
    """Per-master read cache.

    Sketch §13.4. A register's :meth:`cache_for` attaches a TTL slot for its
    absolute address; subsequent reads within the TTL return the last value
    without re-issuing the bus transaction. Side-effecting reads (registers
    where ``info.is_volatile`` is set or ``info.on_read`` is non-``None``)
    are refused at attach time with :class:`NotSupportedError`.

    A :meth:`cached_window` block-scope mirrors the sketch's
    ``with soc.cached(window=10e-3): ...``. Inside the block, every read
    that does not already have its own ``cache_for`` slot is treated as if
    it had one with the given TTL.
    """

    def __init__(self, master: MasterBase) -> None:
        self.master = master
        # Slots attached explicitly via cache_for(). Keyed by absolute address
        # (a register's cache is independent of access width).
        self._slots: dict[int, _CacheEntry] = {}
        # Stack of active cached_window() TTLs; nesting takes the innermost.
        self._window_stack: list[float] = []
        # Implicit slots populated under a cached_window for addresses that
        # have no explicit cache_for; cleared when the outermost window exits.
        self._block_slots: dict[int, _CacheEntry] = {}
        self._lock = RLock()

    # -- Slot management ---------------------------------------------------

    def attach_slot(self, address: int, ttl: float, info: object | None = None) -> None:
        """Attach a TTL cache slot for ``address``.

        Args:
            address: Absolute address of the register being cached.
            ttl: Time-to-live in seconds.
            info: Optional exporter info object. If present and
                ``info.is_volatile`` or ``info.on_read`` is truthy, raises
                :class:`NotSupportedError`. Typed as ``object`` so this code
                does not depend on the (sibling unit's) info schema; the
                check is duck-typed via :func:`getattr`.
        """
        if ttl <= 0:
            raise ValueError(f"cache_for(ttl) must be > 0, got {ttl!r}")
        if info is not None:
            is_volatile = bool(getattr(info, "is_volatile", False))
            on_read = getattr(info, "on_read", None)
            if is_volatile or on_read is not None:
                raise NotSupportedError(
                    f"cannot cache @0x{address:08x}: register has read side effects "
                    f"(is_volatile={is_volatile}, on_read={on_read!r})"
                )
        with self._lock:
            self._slots[address] = _CacheEntry(ttl=ttl)

    def detach_slot(self, address: int) -> None:
        """Remove the explicit cache slot for ``address``, if any."""
        with self._lock:
            self._slots.pop(address, None)
            self._block_slots.pop(address, None)

    def invalidate(self, address: int | None = None) -> None:
        """Invalidate one slot (``address``) or every slot (``address=None``)."""
        with self._lock:
            if address is None:
                for slot in self._slots.values():
                    slot.expiry = 0.0
                self._block_slots.clear()
            else:
                slot = self._slots.get(address)
                if slot is not None:
                    slot.expiry = 0.0
                self._block_slots.pop(address, None)

    # -- Block-scope window ------------------------------------------------

    @contextmanager
    def cached_window(self, window: float) -> Iterator[None]:
        """Treat every read inside the ``with`` block as if it had ``cache_for(window)``."""
        if window <= 0:
            raise ValueError(f"cached(window=) must be > 0, got {window!r}")
        with self._lock:
            self._window_stack.append(window)
        try:
            yield
        finally:
            with self._lock:
                self._window_stack.pop()
                # Discard implicit slots only when the outermost window exits;
                # nested windows share the same hit set.
                if not self._window_stack:
                    self._block_slots.clear()

    # -- Internal hooks invoked by the wrapped master ---------------------

    def before_read(self, address: int, width: int) -> tuple[bool, int]:
        """Probe the cache for ``(address, width)``.

        Returns ``(hit, value)``. Caller skips the bus on a hit.
        """
        now = time.monotonic()
        with self._lock:
            slot = self._slots.get(address)
            if slot is not None and slot.expiry > now:
                return True, slot.value
            if self._window_stack:
                bslot = self._block_slots.get(address)
                if bslot is not None and bslot.expiry > now:
                    return True, bslot.value
        return False, 0

    def after_read(self, address: int, width: int, value: int) -> None:
        """Refresh the cache slot for ``(address, width)`` after a real read."""
        now = time.monotonic()
        with self._lock:
            slot = self._slots.get(address)
            if slot is not None:
                slot.value = value
                slot.expiry = now + slot.ttl
            elif self._window_stack:
                ttl = self._window_stack[-1]
                self._block_slots[address] = _CacheEntry(ttl=ttl, value=value, expiry=now + ttl)

    def after_write(self, address: int, width: int) -> None:
        """Invalidate any cached value at ``address`` after a write hits the bus."""
        with self._lock:
            slot = self._slots.get(address)
            if slot is not None:
                slot.expiry = 0.0
            self._block_slots.pop(address, None)


# ---------------------------------------------------------------------------
# Retry policy (§13.5)
# ---------------------------------------------------------------------------


_DEFAULT_KINDS: tuple[str, ...] = ("timeout", "nack")


def _kind_of(exc: BaseException) -> str | None:
    """Best-effort retrieval of the transport-error tag for ``exc``.

    Looks at an explicit ``kind`` attribute first (set by every
    :class:`TransportError` subclass), then falls back to type sniffing for
    bare builtin exceptions a native master might raise.
    """
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str):
        return kind
    if isinstance(exc, builtins.TimeoutError):
        return "timeout"
    if isinstance(exc, TransportError):
        return exc.kind
    return None


@dataclass
class _RetryConfig:
    retries: int = 3
    backoff: float = 0.05
    on: tuple[str, ...] = _DEFAULT_KINDS
    on_giveup: Literal["raise", "log", "panic"] = "raise"


class RetryPolicy:
    """Per-master retry policy.

    Sketch §13.5. Wraps ``read`` / ``write`` with exponential-backoff retries
    on tagged transport errors, an optional ``on_disconnect`` chain that
    fires when the master raises a ``"disconnect"``-tagged exception, and a
    ``BusError`` envelope for the give-up path.

    Per-call ``retries=`` overrides come through the :class:`CallOverride`
    forwarded by the register node.
    """

    def __init__(self, master: MasterBase) -> None:
        self.master = master
        self._cfg = _RetryConfig()
        self._disconnect_callbacks: list[Callable[[MasterBase], object]] = []
        # Captured so tests can stub backoff sleeps without monkey-patching
        # the time module.
        self._sleep: Callable[[float], None] = time.sleep
        self._lock = RLock()
        # Number of retries actually performed on the most recent call. Read
        # by tests to confirm fast-path retry behavior.
        self.last_retry_count: int = 0

    # -- Configuration -----------------------------------------------------

    def configure(
        self,
        *,
        retries: int | None = None,
        backoff: float | None = None,
        on: Iterable[str] | None = None,
        on_giveup: Literal["raise", "log", "panic"] | None = None,
    ) -> None:
        with self._lock:
            cfg = self._cfg
            if retries is not None:
                if retries < 0:
                    raise ValueError(f"retries must be >= 0, got {retries!r}")
                cfg.retries = retries
            if backoff is not None:
                if backoff < 0:
                    raise ValueError(f"backoff must be >= 0, got {backoff!r}")
                cfg.backoff = backoff
            if on is not None:
                cfg.on = tuple(on)
            if on_giveup is not None:
                if on_giveup not in ("raise", "log", "panic"):
                    raise ValueError(f"on_giveup must be 'raise', 'log', or 'panic', got {on_giveup!r}")
                cfg.on_giveup = on_giveup

    def add_disconnect_callback(self, cb: Callable[[MasterBase], object]) -> None:
        with self._lock:
            self._disconnect_callbacks.append(cb)

    # Test-only: replace the sleep function so backoffs don't slow CI.
    def _set_sleep(self, fn: Callable[[float], None]) -> None:
        self._sleep = fn

    # -- Internal -- the actual retry loop --------------------------------

    def run(
        self,
        op_name: str,
        addr: int,
        fn: Callable[[], int | None],
        *,
        override_retries: int | None = None,
    ) -> int | None:
        """Invoke ``fn``. Retry on configured kinds. Wrap the give-up path.

        Args:
            op_name: ``"read"`` or ``"write"``.
            addr: Absolute address (carried in ``BusError``).
            fn: Zero-arg callable that performs the actual master operation.
            override_retries: Per-call override for the configured retries.

        Returns:
            Whatever ``fn`` returns on success.

        Raises:
            BusError: If retries are exhausted and ``on_giveup="raise"``.
        """
        cfg = self._cfg
        retries = cfg.retries if override_retries is None else override_retries
        if retries < 0:
            raise ValueError(f"retries must be >= 0, got {retries!r}")
        attempt = 0
        while True:
            try:
                result = fn()
            except BaseException as exc:
                kind = _kind_of(exc)
                if kind == "disconnect":
                    self._fire_disconnect()
                retryable = kind is not None and kind in cfg.on
                if not retryable or attempt >= retries:
                    self.last_retry_count = attempt
                    return self._giveup(op_name, addr, attempt, exc)
                self._sleep(cfg.backoff * (2**attempt))
                attempt += 1
                continue
            self.last_retry_count = attempt
            return result

    def _giveup(
        self,
        op_name: str,
        addr: int,
        attempt: int,
        exc: BaseException,
    ) -> int | None:
        cfg = self._cfg
        bus_err = BusError(addr, op_name, self.master, attempt, exc)
        if cfg.on_giveup == "raise":
            raise bus_err from exc
        if cfg.on_giveup == "log":
            # "log" mode swallows the failure: read returns 0, write returns
            # None. Users who pick it accept that the wire state is unknown.
            _logger.error("%s", bus_err)
            return 0 if op_name == "read" else None
        # "panic" — escalate by firing the disconnect chain, then raise.
        self._fire_disconnect()
        raise bus_err from exc

    def _fire_disconnect(self) -> None:
        with self._lock:
            callbacks = list(self._disconnect_callbacks)
        for cb in callbacks:
            try:
                cb(self.master)
            except BaseException:
                # A failing reconnect callback must not mask the original bus
                # failure; record at debug and move on.
                _logger.debug("on_disconnect callback raised", exc_info=True)


# ---------------------------------------------------------------------------
# Combined policy bundle — registered via the master-extension seam
# ---------------------------------------------------------------------------


@dataclass
class BusPolicies:
    """Container that wraps a single :class:`MasterBase`.

    The wrapping is monkey-patch style: instantiating a :class:`BusPolicies`
    replaces the master's ``read`` and ``write`` with delegations that route
    through cache → retry → barrier → master. This keeps the seam single
    and matches sketch §13.1's "every read and write goes through the
    master" claim.

    The original methods are preserved on the policy bundle as
    :attr:`_inner_read` / :attr:`_inner_write`; tests, traces, and replay
    masters can use the policies' own ``inner_*`` to bypass the policy
    chain.
    """

    master: MasterBase
    barriers: BarrierPolicy = field(init=False)
    cache: CachePolicy = field(init=False)
    retry: RetryPolicy = field(init=False)
    _inner_read: Callable[..., int] = field(init=False, repr=False)
    _inner_write: Callable[..., None] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.barriers = BarrierPolicy(self.master)
        self.cache = CachePolicy(self.master)
        self.retry = RetryPolicy(self.master)
        self._inner_read = self.master.read
        self._inner_write = self.master.write
        # Replace public methods so any pre-existing reference to
        # `master.read` picks up the policy chain transparently.
        self.master.read = self._wrapped_read  # type: ignore[method-assign]
        self.master.write = self._wrapped_write  # type: ignore[method-assign]

    # -- Wrapped surface --------------------------------------------------

    def _wrapped_read(
        self,
        address: int,
        width: int,
        _pe_override: CallOverride | None = None,
    ) -> int:
        bypass_cache = bool(_pe_override and _pe_override.bypass_cache)

        if not bypass_cache:
            hit, cached = self.cache.before_read(address, width)
            if hit:
                return cached

        def do_read() -> int:
            self.barriers.before_read()
            return self._inner_read(address, width)

        result = self.retry.run(
            "read",
            address,
            do_read,
            override_retries=(_pe_override.retries if _pe_override else None),
        )
        # `run` returns int | None; "log" giveup yields 0 for reads, never None.
        value = 0 if result is None else int(result)

        if not bypass_cache:
            # TODO(umbrella PR): on `on_giveup="log"` this caches the
            # default-zero return as a real read. Should skip the slot
            # refresh when the call gave up. Out of scope for Unit 17.
            self.cache.after_read(address, width, value)
        return value

    def _wrapped_write(
        self,
        address: int,
        value: int,
        width: int,
        _pe_override: CallOverride | None = None,
    ) -> None:
        def do_write() -> None:
            self.barriers.before_write()
            self._inner_write(address, value, width)

        self.retry.run(
            "write",
            address,
            do_write,
            override_retries=(_pe_override.retries if _pe_override else None),
        )
        self.barriers.after_write()
        self.cache.after_write(address, width)


# ---------------------------------------------------------------------------
# Public convenience: install the unified bundle as the registered extension
# ---------------------------------------------------------------------------


_EXTENSION_NAME = "bus_policies"


def install(master: MasterBase) -> BusPolicies:
    """Wrap ``master`` with the combined bus policies and return the bundle.

    Equivalent to calling the registered extension factory directly. Not
    idempotent: a second :func:`install` on the same master double-wraps,
    because :class:`BusPolicies` captures whatever ``master.read`` /
    ``master.write`` already point at. Callers that need to re-install must
    reconstruct the master first.
    """
    return BusPolicies(master=master)


# Register the factory at import time so the SoC layer can rely on a
# single, named seam rather than importing the class directly.
register_master_extension(_EXTENSION_NAME, install)


__all__ = [
    "BarrierPolicy",
    "BusPolicies",
    "CachePolicy",
    "CallOverride",
    "RetryPolicy",
    "install",
]
