"""Per-register read caching (sketch §13.4).

Implements the three caching surfaces described in
``docs/IDEAL_API_SKETCH.md`` §13.4:

* ``reg.cache_for(seconds)`` — within the window, ``reg.read()`` returns
  the cached value without going to the bus.
* ``reg.invalidate_cache()`` — drop the cached value and force a fresh
  read on the next ``reg.read()``.
* ``with soc.cached(window=seconds):`` — set a TTL for every cacheable
  register inside the block and clear them all on exit.

Cacheability is a property of the register, not the request: a register
whose ``info.is_volatile`` is true, or whose ``info.on_read`` carries a
side effect (``rclr`` / ``rset`` / ``ruser``), refuses the cache. The
per-register entry point raises :class:`NotSupportedError`; the
``soc.cached`` context manager silently skips those registers — pre-flight
filtering keeps a single side-effecting register in the tree from blowing
up the whole block.

Design notes
------------
The cache stores the :class:`RegisterValue` returned by the *inner*
``read`` (the already-enhanced one installed by
:mod:`peakrdl_pybind11.runtime._default_shims`). That avoids re-wrapping
the int in a fresh :class:`RegisterValue` on every cache hit: the inner
already produced a fully-decorated immutable value, and returning the
same instance is safe because :class:`RegisterValue` is immutable.

Per-instance state lives on the register instance as a plain attribute
(``_peakrdl_cache_state``). The generated register classes are declared
with ``py::dynamic_attr`` so attribute assignment is always available;
that is the same channel every other sibling unit uses (snapshot, trace,
observers).

Time is consulted via ``time.monotonic()`` rather than a bound
``from time import monotonic`` so tests that monkeypatch ``time.monotonic``
take effect inside this module too.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, cast

from . import _registry
from .errors import NotSupportedError

logger = logging.getLogger("peakrdl_pybind11.runtime.caching")

__all__ = [
    "attach_cached_context_manager",
    "invalidate_cache",
    "is_cacheable",
    "register_cache_enhancement",
]


# Sentinel attribute placed on enhanced read callables so we never wrap
# twice. Mirrors the ``__peakrdl_enhanced__`` marker used by the default
# shims so the two layers compose cleanly.
_CACHE_ENHANCED = "__peakrdl_cache_enhanced__"

# Per-instance attribute name used to stash the (expiry, value) tuple on
# a register instance. The default register shim uses ``__peakrdl_meta__``
# on the *class*; per-instance attachments like trace / snapshot use
# private attr names on the instance. We follow the latter pattern so the
# state is decoupled from class-level metadata.
_STATE_ATTR = "_peakrdl_cache_state"

# Side effects on read that disqualify a register from caching. The set
# mirrors :data:`peakrdl_pybind11.runtime.side_effects._DESTRUCTIVE_READ_EFFECTS`
# but is duplicated here to keep this module standalone (the side_effects
# module also depends on info / errors and we want to remain independent).
_DESTRUCTIVE_READ_EFFECTS = frozenset({"rclr", "rset", "ruser"})


# ---------------------------------------------------------------------------
# Cacheability check
# ---------------------------------------------------------------------------


def _path_of(reg: Any) -> str:
    """Best-effort path string for diagnostics."""
    info = getattr(reg, "info", None)
    if info is not None:
        path = getattr(info, "path", None)
        if isinstance(path, str) and path:
            return path
    name = getattr(reg, "name", None)
    return name if isinstance(name, str) and name else "<register>"


def _on_read_token(reg: Any) -> str | None:
    """Return the ``on_read`` side-effect token (or ``None``).

    Accepts either a string token (``"rclr"``) or an enum-like value that
    exposes a ``.name`` attribute. ``None`` / falsy collapses to ``None``.
    """
    info = getattr(reg, "info", None)
    if info is None:
        return None
    raw = getattr(info, "on_read", None)
    if raw is None:
        return None
    name = getattr(raw, "name", None)
    text = name if isinstance(name, str) else str(raw)
    text = text.strip().lower()
    if not text or text == "none":
        return None
    return text


def _is_volatile(reg: Any) -> bool:
    info = getattr(reg, "info", None)
    return bool(getattr(info, "is_volatile", False)) if info is not None else False


def is_cacheable(reg: Any) -> bool:
    """Return ``True`` iff ``reg`` may have a cache attached.

    A register is cacheable when its ``info.is_volatile`` is false *and*
    its ``info.on_read`` does not signal a destructive side effect.
    """
    if _is_volatile(reg):
        return False
    token = _on_read_token(reg)
    if token is not None and token in _DESTRUCTIVE_READ_EFFECTS:
        return False
    return True


def _refuse_uncacheable(reg: Any) -> None:
    """Raise :class:`NotSupportedError` describing why ``reg`` can't be cached."""
    path = _path_of(reg)
    reasons: list[str] = []
    if _is_volatile(reg):
        reasons.append("volatile")
    token = _on_read_token(reg)
    if token is not None and token in _DESTRUCTIVE_READ_EFFECTS:
        reasons.append(f"on_read={token}")
    detail = " / ".join(reasons) if reasons else "side-effecting read"
    raise NotSupportedError(f"cannot cache {path}: {detail}")


# ---------------------------------------------------------------------------
# Per-instance cache state
# ---------------------------------------------------------------------------


def _set_state(reg: Any, expiry: float, value: Any) -> None:
    """Store the ``(expiry, value)`` entry on the register instance.

    ``value`` is ``None`` until the first post-``cache_for`` read fills it.
    """
    try:
        setattr(reg, _STATE_ATTR, (expiry, value))
    except (AttributeError, TypeError):
        # Generated register classes use ``py::dynamic_attr`` so setattr
        # is always available; the guard exists for hand-rolled stubs in
        # tests that intentionally lack ``__dict__``.
        logger.debug("cannot install cache state on %r", reg)


def _get_state(reg: Any) -> tuple[float, Any] | None:
    return getattr(reg, _STATE_ATTR, None)


def _clear_state(reg: Any) -> None:
    """Drop the cache entry without touching the bus."""
    try:
        delattr(reg, _STATE_ATTR)
    except AttributeError:
        # No entry to clear -- treat as a no-op so callers can invalidate
        # unconditionally without first checking.
        pass


def invalidate_cache(reg: Any) -> None:
    """Drop ``reg``'s cache entry, if any. Public free-function form."""
    _clear_state(reg)


# ---------------------------------------------------------------------------
# Read wrapper
# ---------------------------------------------------------------------------


def _wrap_read(original_read: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap an already-enhanced ``read`` so it consults the cache first.

    ``raw=True`` is the explicit escape hatch: it bypasses the cache and
    delegates straight to the inner ``read``. Cached entries hold the
    :class:`RegisterValue` produced by the inner — returning the same
    instance on a hit avoids re-wrapping (the inner already did that
    work) and is safe because :class:`RegisterValue` is immutable.
    """

    def read(self: Any, *, raw: bool = False) -> Any:
        if raw:
            # Escape hatch: never consult or populate the cache.
            return original_read(self, raw=True)
        state = _get_state(self)
        if state is not None:
            expiry, cached_value = state
            now = time.monotonic()
            if now < expiry:
                if cached_value is not None:
                    return cached_value
                # Window is active but no value yet — do a real read and
                # populate the cache so subsequent reads inside the
                # window are served from memory.
                fresh = original_read(self)
                _set_state(self, expiry, fresh)
                return fresh
            # Expired — drop the stale entry and fall through to a
            # fresh read. We don't repopulate: ``cache_for`` is the
            # explicit "start a new window" entry point.
            _clear_state(self)
        return original_read(self)

    setattr(read, _CACHE_ENHANCED, True)
    return read


def _wrap_invalidating(original: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a mutating call (``write`` / ``modify``) so it invalidates the cache.

    Any successful write makes the cached :class:`RegisterValue` stale by
    construction. We clear the entry *before* delegating to the original so
    that an exception raised mid-write still leaves the cache dropped — the
    next read will go to the bus, which is the safe answer when the prior
    write's effect is uncertain.
    """

    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        _clear_state(self)
        return original(self, *args, **kwargs)

    setattr(wrapper, _CACHE_ENHANCED, True)
    return wrapper


# ---------------------------------------------------------------------------
# Register enhancement
# ---------------------------------------------------------------------------


@_registry.register_register_enhancement
def register_cache_enhancement(cls: type, metadata: dict) -> None:
    """Attach ``cache_for`` / ``invalidate_cache`` and wrap ``read``/``write``/``modify``.

    ``metadata`` is the dict the generated runtime passes in; we only
    consult it to bail cleanly on a stub class without ``read``. The real
    work is per-instance and runs at call time.

    ``write`` and ``modify`` are wrapped so any bus write invalidates the
    cached :class:`RegisterValue` before delegating to the original: a
    write makes the cached value stale by construction, so subsequent
    reads must hit the bus. Both wrappers carry the same
    ``__peakrdl_cache_enhanced__`` sentinel as the read wrapper so the
    enhancement remains idempotent.

    Idempotent: re-applying the enhancement is a no-op courtesy of the
    ``__peakrdl_cache_enhanced__`` sentinel on the wrapped ``read``.
    """
    _ = metadata  # unused -- per-instance state, not class metadata.

    raw_read = getattr(cls, "read", None)
    if raw_read is None:
        return
    if getattr(raw_read, _CACHE_ENHANCED, False):
        return  # already cache-enhanced

    cls.read = _wrap_read(raw_read)  # type: ignore[method-assign]

    raw_write = getattr(cls, "write", None)
    if raw_write is not None and not getattr(raw_write, _CACHE_ENHANCED, False):
        cls.write = _wrap_invalidating(raw_write)  # type: ignore[method-assign]

    raw_modify = getattr(cls, "modify", None)
    if raw_modify is not None and not getattr(raw_modify, _CACHE_ENHANCED, False):
        cls.modify = _wrap_invalidating(raw_modify)  # type: ignore[method-assign]

    def cache_for(self: Any, seconds: float) -> None:
        """Cache ``self.read()`` results for the next ``seconds``.

        The window starts now (``time.monotonic()``). The first read
        inside the window goes to the bus and populates the cache; every
        subsequent read until the window expires returns the cached
        value without touching the master.

        Raises :class:`NotSupportedError` if the register is volatile or
        its ``on_read`` carries a destructive side effect.
        """
        if not is_cacheable(self):
            _refuse_uncacheable(self)
        # ``None`` for the value means "fetch on first read, then store".
        _set_state(self, time.monotonic() + float(seconds), None)

    def invalidate_cache_method(self: Any) -> None:
        """Drop ``self``'s cache entry, forcing the next read to hit the bus."""
        _clear_state(self)

    cls.cache_for = cache_for  # type: ignore[attr-defined]
    cls.invalidate_cache = invalidate_cache_method  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# SoC-level ``cached`` context manager
# ---------------------------------------------------------------------------


def _iter_cacheable_registers(soc: Any) -> Iterator[Any]:
    """Yield every register in ``soc`` that ``is_cacheable`` accepts.

    Uses ``soc.walk(kind="reg")`` (installed by the discovery hook in
    :mod:`peakrdl_pybind11.runtime.routing`). Falls back to walking the
    tree with no filter and selecting nodes that look like registers
    (have ``cache_for``) if ``walk`` is absent.
    """
    walk = getattr(soc, "walk", None)
    if callable(walk):
        try:
            seq: Iterable[Any] = cast("Iterable[Any]", walk(kind="reg"))
        except TypeError:
            # Some walkers don't accept ``kind=``; fall back to unfiltered.
            seq = cast("Iterable[Any]", walk())
        for node in seq:
            if hasattr(node, "cache_for") and is_cacheable(node):
                yield node
        return
    # No walker — best-effort scan of the instance ``__dict__``.
    seen: set[int] = set()
    stack: list[Any] = [soc]
    while stack:
        cur = stack.pop()
        key = id(cur)
        if key in seen:
            continue
        seen.add(key)
        if hasattr(cur, "cache_for") and is_cacheable(cur):
            yield cur
        try:
            nested = vars(cur)
        except TypeError:
            continue
        for name, child in nested.items():
            if name.startswith("_") or child is cur:
                continue
            stack.append(child)


def attach_cached_context_manager(soc: Any) -> None:
    """Install ``soc.cached(window=seconds)`` as a context manager.

    On enter, calls ``reg.cache_for(window)`` on every cacheable register
    in the tree. On exit, calls ``reg.invalidate_cache()`` on the same
    set so any partially-populated entries are dropped, even if the
    block raised.

    Idempotent: re-installation overwrites the previous attribute but
    the behaviour is identical, so attaching twice is harmless.
    """

    @contextmanager
    def cached(window: float) -> Iterator[None]:
        # Materialise the cacheable set once so the exit path invalidates
        # exactly the registers it primed -- newly added cache_for calls
        # inside the block are the user's responsibility.
        targets = list(_iter_cacheable_registers(soc))
        for reg in targets:
            reg.cache_for(window)
        try:
            yield
        finally:
            for reg in targets:
                reg.invalidate_cache()

    try:
        soc.cached = cached  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        # Slotted SoC stub -- nothing to attach to. The post-create hook
        # is best-effort; tests using bare objects build the context
        # manager themselves.
        logger.debug("cannot attach soc.cached to %r", soc)


_registry.register_post_create(attach_cached_context_manager)
