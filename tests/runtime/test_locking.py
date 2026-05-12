"""Tests for the synchronous SoC-wide mutex (sketch §13.8).

The lock is a cooperative :class:`threading.RLock` attached as
``soc.lock()``. These tests cover:

* Entering / exiting a ``with soc.lock(): ...`` block on a fake SoC.
* Re-entrancy from the same thread (the whole point of using
  :class:`RLock` rather than :class:`Lock`).
* Mutual exclusion between two threads, verified with
  :class:`threading.Event` to keep the test reliable rather than flaky.
* Identity: repeated ``soc.lock()`` calls return the same primitive.
* Cleanup: exiting the context manager releases the lock so a competing
  thread can acquire immediately.

Pure-Python -- no cmake, no generated module. The fake SoC is any object
that accepts dynamic attribute assignment, which is the same precondition
the runtime relies on for ``soc.batch()``, ``soc.tree()``, etc.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from peakrdl_pybind11.runtime.locking import (
    _LOCK_ATTR,
    _PUBLIC_METHOD,
    _RLockType,
    attach_lock,
    get_lock,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSoc:
    """Minimal dynamic-attr SoC stand-in.

    Accepts ``setattr`` for arbitrary names -- this is the same shape any
    generated SoC built with ``py::dynamic_attr()`` exposes, and the
    same shape every other post-create hook in the runtime tests uses.
    """


# A slotted variant for the "reject ``setattr``" branch -- ensures the
# fallback cache path still produces a working, re-entrant lock.
class _SlottedSoc:
    __slots__ = ()


@pytest.fixture()
def soc() -> Any:
    """Fresh ``_FakeSoc`` with ``soc.lock()`` attached."""
    instance = _FakeSoc()
    attach_lock(instance)
    return instance


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


class TestAttach:
    """``attach_lock`` installs the public method idempotently."""

    def test_attaches_callable(self) -> None:
        soc = _FakeSoc()
        attach_lock(soc)
        assert callable(getattr(soc, _PUBLIC_METHOD))

    def test_attach_returns_soc(self) -> None:
        soc = _FakeSoc()
        result = attach_lock(soc)
        assert result is soc

    def test_idempotent(self) -> None:
        soc = _FakeSoc()
        attach_lock(soc)
        first = soc.lock
        attach_lock(soc)
        second = soc.lock
        # Re-attaching must not stomp the existing binding.
        assert first is second

    def test_existing_method_preserved(self) -> None:
        soc = _FakeSoc()
        sentinel = lambda: "user-supplied"  # noqa: E731
        soc.lock = sentinel
        attach_lock(soc)
        # An existing callable must win -- we don't stomp generated
        # runtimes or other sibling units' bindings.
        assert soc.lock is sentinel

    def test_slotted_soc_skips_silently(self) -> None:
        soc = _SlottedSoc()
        # Must not raise even though ``setattr`` would fail.
        attach_lock(soc)
        assert not hasattr(soc, _PUBLIC_METHOD)


# ---------------------------------------------------------------------------
# Context manager behaviour
# ---------------------------------------------------------------------------


class TestContextManager:
    """``with soc.lock(): ...`` enters and exits cleanly."""

    def test_enters_and_exits(self, soc: Any) -> None:
        with soc.lock():
            pass  # No deadlock, no exception.

    def test_returns_rlock(self, soc: Any) -> None:
        lock = soc.lock()
        assert isinstance(lock, _RLockType)

    def test_stashes_on_soc(self, soc: Any) -> None:
        _ = soc.lock()
        # The rlock is cached as ``_peakrdl_lock`` so subsequent calls
        # return the same primitive (key for re-entrancy guarantees).
        assert isinstance(getattr(soc, _LOCK_ATTR), _RLockType)

    def test_lock_released_after_exit(self, soc: Any) -> None:
        lock = soc.lock()
        with lock:
            pass
        # ``acquire(blocking=False)`` succeeds only if the lock is free.
        acquired = lock.acquire(blocking=False)
        assert acquired is True
        lock.release()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestIdentity:
    """Repeated ``soc.lock()`` calls hand back the same primitive."""

    def test_same_instance_across_calls(self, soc: Any) -> None:
        first = soc.lock()
        second = soc.lock()
        third = soc.lock()
        assert first is second is third

    def test_separate_socs_get_separate_locks(self) -> None:
        soc_a = _FakeSoc()
        soc_b = _FakeSoc()
        attach_lock(soc_a)
        attach_lock(soc_b)
        assert soc_a.lock() is not soc_b.lock()

    def test_get_lock_helper(self) -> None:
        soc = _FakeSoc()
        lock = get_lock(soc)
        assert isinstance(lock, _RLockType)
        # Calling again returns the same primitive.
        assert get_lock(soc) is lock

    def test_slotted_soc_lock_is_stable(self) -> None:
        soc = _SlottedSoc()
        # No public method, but ``get_lock`` still works via the
        # fallback cache -- the rlock must be stable per instance for
        # re-entrancy to hold.
        first = get_lock(soc)
        second = get_lock(soc)
        assert first is second
        assert isinstance(first, _RLockType)


# ---------------------------------------------------------------------------
# Re-entrancy
# ---------------------------------------------------------------------------


class TestReentrancy:
    """Nested ``with soc.lock(): ...`` must not deadlock from one thread."""

    def test_nested_two_deep(self, soc: Any) -> None:
        # ``RLock`` permits the same thread to re-acquire. A plain
        # ``Lock`` would deadlock here -- the assertion is implicit: if
        # this completes without timing out, the lock is re-entrant.
        with soc.lock():
            with soc.lock():
                pass

    def test_nested_three_deep(self, soc: Any) -> None:
        with soc.lock():
            with soc.lock():
                with soc.lock():
                    pass

    def test_nested_releases_in_lifo_order(self, soc: Any) -> None:
        lock = soc.lock()
        with lock:
            with lock:
                pass
            # After the inner exit, the lock is still held by this
            # thread (single re-entry remaining).
            assert lock.acquire(blocking=False)
            lock.release()
        # Both layers released; an outside acquirer wins immediately.
        assert lock.acquire(blocking=False)
        lock.release()

    def test_reentrancy_with_runtime_timeout(self, soc: Any) -> None:
        # Guard against a regression that swaps :class:`RLock` for
        # :class:`Lock`. A plain ``Lock`` would deadlock the second
        # acquire and the timeout below would fire.
        done = threading.Event()

        def _worker() -> None:
            with soc.lock():
                with soc.lock():
                    pass
            done.set()

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join(timeout=1.0)
        # If re-entrancy regressed, ``done`` is unset and the thread is
        # still alive -- the test fails loudly rather than hanging
        # forever.
        assert done.is_set(), "re-entrant acquire deadlocked"
        assert not thread.is_alive()


# ---------------------------------------------------------------------------
# Mutual exclusion across threads
# ---------------------------------------------------------------------------


class TestMutualExclusion:
    """Two threads must serialize through ``with soc.lock(): ...``."""

    def test_second_thread_blocks_while_first_holds(self, soc: Any) -> None:
        # Reliability strategy: use events to chain "first thread is
        # inside the cm", "second thread tried to acquire", "first
        # thread releases". No ``time.sleep`` for ordering -- only as a
        # cap to prove the second thread *would* have blocked.
        first_inside = threading.Event()
        first_may_exit = threading.Event()
        second_acquired = threading.Event()
        second_started = threading.Event()

        def _first() -> None:
            with soc.lock():
                first_inside.set()
                first_may_exit.wait(timeout=2.0)

        def _second() -> None:
            second_started.set()
            with soc.lock():
                second_acquired.set()

        t1 = threading.Thread(target=_first)
        t2 = threading.Thread(target=_second)
        t1.start()

        # Wait until t1 actually holds the lock before launching t2.
        assert first_inside.wait(timeout=2.0), "first thread never acquired"

        t2.start()
        # Give t2 a generous window to *try* to acquire. The window
        # being long means a flaky scheduler can't fail us -- t2 has
        # plenty of time to make the call. The actual assertion is the
        # opposite: t2 must NOT have entered the cm.
        assert second_started.wait(timeout=2.0), "second thread never started"
        # Poll for a short, bounded interval to confirm t2 is blocked
        # rather than just slow. 200ms is comfortably above OS scheduler
        # granularity (10ms on Linux/macOS) and avoids real-world noise.
        time.sleep(0.2)
        assert not second_acquired.is_set(), "second thread acquired while first held lock"

        # Release the first thread; the second must then proceed.
        first_may_exit.set()
        t1.join(timeout=2.0)
        # Second thread should acquire promptly once the lock frees.
        assert second_acquired.wait(timeout=2.0), "second thread never acquired after release"
        t2.join(timeout=2.0)
        assert not t1.is_alive()
        assert not t2.is_alive()

    def test_lock_released_lets_second_thread_acquire_immediately(self, soc: Any) -> None:
        # Exit the cm fully, then verify a second thread can acquire
        # right away (no leftover state, no half-released ref count).
        with soc.lock():
            pass

        acquired = threading.Event()

        def _worker() -> None:
            # ``blocking=False``: if the lock weren't cleanly released,
            # this would return ``False`` and the test would fail.
            lock = soc.lock()
            if lock.acquire(blocking=False):
                acquired.set()
                lock.release()

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join(timeout=2.0)
        assert acquired.is_set(), "lock was not cleanly released"

    def test_serialized_increments(self, soc: Any) -> None:
        # A classic counter race: without the lock, concurrent
        # increments would drop updates. We don't claim Python's GIL
        # makes ``+=`` atomic on a list element (it doesn't, in
        # general), but we DO claim that two threads using the cm see
        # exclusion. This is the strongest end-to-end signal we get
        # without instrumenting the lock itself.
        shared: list[int] = [0]
        iterations = 200
        threads = 4

        def _bump() -> None:
            for _ in range(iterations):
                with soc.lock():
                    current = shared[0]
                    # Force a yield point: even on CPython, this is
                    # where another thread would interleave if the
                    # lock weren't held.
                    time.sleep(0)
                    shared[0] = current + 1

        workers = [threading.Thread(target=_bump) for _ in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=5.0)
        for w in workers:
            assert not w.is_alive(), "worker did not finish in time"
        assert shared[0] == iterations * threads
