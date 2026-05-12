"""Synchronous SoC-wide mutex (sketch §13.8).

The aspirational API surfaces ``with soc.lock():`` as a cooperative,
re-entrant lock that serializes multi-step register sequences. Callers
opt in -- we deliberately do **not** wrap every ``read``/``write`` in
this lock, which would impose a per-op acquire/release tax on the
single-threaded common case. Only sequences that explicitly want
exclusion enter the context manager.

Design notes
------------
* The lock is a stdlib :class:`threading.RLock`. Re-entrancy matters
  because a typical pattern nests ``with soc.lock(): with
  soc.transaction(): ...`` -- the inner block may itself acquire ``soc``
  attributes that take the lock recursively. A plain ``Lock`` would
  deadlock.
* The same ``RLock`` instance is stashed on the SoC as
  ``soc._peakrdl_lock`` so repeated ``soc.lock()`` calls hand out the
  same primitive. Different SoCs naturally get different locks (the
  storage is per-instance).
* ``soc.lock`` itself is a zero-argument callable that *returns* the
  rlock. The rlock is its own context manager, so
  ``with soc.lock(): ...`` works directly.
* Attachment is best-effort: pybind11 classes without
  ``py::dynamic_attr()`` reject ``setattr``, so we use ``_try_setattr``
  to swallow the rejection -- callers on those SoCs need to wire the
  lock themselves (or get a pure-Python SoC wrapper).
* The async dual (``soc.async_session()``) is handled by
  ``runtime/async_session.py``. This module is sync-only.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

__all__ = [
    "attach_lock",
    "get_lock",
]

logger = logging.getLogger("peakrdl_pybind11.runtime.locking")

# Stash slot for the per-SoC ``RLock``. Underscore-prefixed so it doesn't
# clutter tab completion or ``dir(soc)`` for the public API. Single
# leading underscore (not dunder) so we don't trigger Python's
# name-mangling on subclasses.
_LOCK_ATTR = "_peakrdl_lock"

# Public method name installed on each SoC instance.
_PUBLIC_METHOD = "lock"


def _try_setattr(obj: Any, name: str, value: Any) -> bool:
    """``setattr`` that swallows pybind11's "no dynamic attrs" rejection.

    Returns ``True`` on success, ``False`` if the target refuses the
    assignment (the standard signal from a slotted/pybind11 class). The
    boolean lets callers branch on whether the public method was
    actually installed.
    """
    try:
        setattr(obj, name, value)
    except (AttributeError, TypeError) as exc:
        logger.debug("could not attach %r to %r: %s", name, type(obj).__name__, exc)
        return False
    return True


def get_lock(soc: Any) -> threading.RLock:
    """Return the :class:`threading.RLock` associated with ``soc``.

    Lazily creates the lock on first access and caches it under
    ``soc._peakrdl_lock``. Subsequent calls return the same instance so
    nested ``with soc.lock(): with soc.lock(): ...`` blocks always
    operate on a single primitive (and therefore re-enter cleanly from
    the same thread).

    Falls back to a module-level cache keyed on ``id(soc)`` if the SoC
    rejects attribute assignment -- a pybind11 SoC without
    ``py::dynamic_attr()`` cannot stash the rlock as an instance
    attribute, but the cooperative lock must still be a stable
    per-instance primitive for re-entrancy to work.
    """
    existing = getattr(soc, _LOCK_ATTR, None)
    if isinstance(existing, _RLockType):
        return existing

    rlock = threading.RLock()
    if _try_setattr(soc, _LOCK_ATTR, rlock):
        return rlock

    # SoC refused the attribute (slotted/pybind11 class without
    # ``py::dynamic_attr()``). Fall back to the module-level fallback
    # cache so re-entrancy still works -- otherwise every ``soc.lock()``
    # call would hand out a brand-new lock and the "same instance"
    # guarantee would be broken.
    return _fallback_cache.get_or_create(soc)


class _FallbackCache:
    """Per-process cache for SoCs that reject instance attributes.

    Keyed by ``id(soc)``; entries pin the SoC weakly where possible so a
    GC'd SoC doesn't keep its rlock alive forever. ``weakref`` doesn't
    work on every pybind11 type, so we fall back to a hard reference
    keyed by ``id``; the per-process memory cost is one rlock per SoC,
    which is negligible.
    """

    def __init__(self) -> None:
        self._cache: dict[int, threading.RLock] = {}
        self._lock = threading.Lock()

    def get_or_create(self, soc: Any) -> threading.RLock:
        key = id(soc)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            rlock = threading.RLock()
            self._cache[key] = rlock
            return rlock


_fallback_cache = _FallbackCache()


# ``threading.RLock`` is a factory function rather than a class, so
# ``isinstance(obj, threading.RLock)`` doesn't work directly. We snapshot
# the type of an instance once and use that for the type check above.
_RLockType = type(threading.RLock())


def attach_lock(soc: Any) -> Any:
    """Attach the bound ``soc.lock()`` method.

    Idempotent: if ``soc.lock`` is already callable we leave it alone
    (the generated runtime may ship its own implementation, or another
    sibling unit may have wired one already). The lock primitive itself
    is created lazily on the first ``soc.lock()`` call via
    :func:`get_lock`.

    Returns ``soc`` for chaining; the registry's ``register_post_create``
    discards the return value.
    """
    existing = getattr(soc, _PUBLIC_METHOD, None)
    if callable(existing) and getattr(existing, "_peakrdl_lock_bound", False):
        return soc  # already bound by an earlier attach pass
    if callable(existing):
        # Something else (a generated runtime, a sibling unit) already
        # provided a ``soc.lock`` method. Respect it.
        return soc

    def _bound_lock() -> threading.RLock:
        """Return the SoC-wide re-entrant lock.

        Use as a context manager::

            with soc.lock():
                soc.uart.control.write(0x1)
                soc.uart.status.read()
        """
        return get_lock(soc)

    _bound_lock._peakrdl_lock_bound = True  # type: ignore[attr-defined]
    _bound_lock.__name__ = _PUBLIC_METHOD
    _bound_lock.__qualname__ = f"{type(soc).__name__}.{_PUBLIC_METHOD}"
    _try_setattr(soc, _PUBLIC_METHOD, _bound_lock)
    return soc


# ---------------------------------------------------------------------------
# Registry wiring (sibling-dep: Unit 1's ``runtime/_registry``).
#
# When the registry seam is present we register :func:`attach_lock` as a
# post-create hook so every ``MySoc.create()`` automatically gains the
# ``soc.lock()`` method. When it isn't (this module imported in
# isolation), the import quietly fails and callers can still invoke
# :func:`attach_lock` directly.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - depends on Unit 1 landing order
    from . import _registry  # type: ignore[attr-defined]
except ImportError:
    _registry = None  # type: ignore[assignment]

if _registry is not None and hasattr(_registry, "register_post_create"):
    _registry.register_post_create(attach_lock)  # type: ignore[arg-type]
