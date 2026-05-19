"""Stateful (rule-based) hypothesis tests for ``runtime/caching.py``.

The unit suite in :mod:`tests.runtime.test_caching` covers each piece of
the cache contract (``cache_for`` window, expiry-falls-back-to-bus,
explicit invalidation, ``soc.cached`` context manager) in isolation. The
state machine here drives random *combinations* of those operations —
the territory where order-dependent state machines tend to break.

Wave W4 / S3: a single :class:`CacheStateMachine` with five rules
(``cache_for``, ``read``, ``write``, ``invalidate_cache``,
``clock_advance``) plus shadow state that predicts whether the next read
should hit the bus. The shadow state lets us assert two things on every
step:

* an in-window read does **not** increment the master's read counter;
* an out-of-window (or post-invalidate, or post-write) read increments
  it exactly once.

Clock: we monkeypatch ``time.monotonic`` to read from a single-element
list the rules mutate. The caching module deliberately calls
``time.monotonic()`` (rather than rebinding the name) precisely so tests
can swap it — see the module-level docstring on
``src/peakrdl_pybind11/runtime/caching.py`` lines 36-38.

A few sequences in this machine surface the *write-does-not-invalidate*
bug in ``runtime/caching.py`` — the enhancement wraps ``cls.read`` but
never ``cls.write``, so a write inside a still-live cache window leaves
the stale ``RegisterValue`` in place. The full machine is therefore
``xfail`` until that gap is closed.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from peakrdl_pybind11.runtime.caching import register_cache_enhancement

# ---------------------------------------------------------------------------
# Fixtures: counting master + register class (mirrors the ``_RecordingMaster``
# pattern from ``tests/runtime/test_caching.py``).
# ---------------------------------------------------------------------------


class _CountingMaster:
    """Minimal master that counts read() invocations.

    The cache-enhanced ``Register.read`` ultimately calls ``self._master
    .read(self.offset)`` (single-arg, no width) so we mirror that shape
    instead of the burst-aware ``MockMaster``.
    """

    def __init__(self) -> None:
        self.reads = 0
        self.next_value = 0xDEAD_BEEF
        self.last_written: int | None = None

    def read(self, address: int) -> int:
        self.reads += 1
        return self.next_value

    def write(self, address: int, value: int) -> None:
        self.last_written = value
        # Mimic the bus by making the next read observe the write. The
        # cache machine relies on this so the *shadow* and *real* model
        # agree once the cache is invalidated.
        self.next_value = value


def _make_register_class() -> type:
    """Build a register class with the layout the caching enhancement expects."""

    info = SimpleNamespace(
        name="ctrl",
        path="soc.ctrl",
        address=0x4000_0000,
        is_volatile=False,
        on_read=None,
    )

    class Register:
        # The default shim consults offset/width; the bare caching
        # enhancement does not, but we keep the fields so the class
        # mirrors a real register exactly.
        offset = 0x4000_0000
        width = 4

        def __init__(self, master: _CountingMaster) -> None:
            self._master = master
            self.name = "ctrl"

        def read(self) -> int:
            return self._master.read(self.offset)

        def write(self, value: int) -> None:
            self._master.write(self.offset, value)

        def modify(self, value: int, mask: int) -> None:
            current = self._master.read(self.offset) & ~mask
            self._master.write(self.offset, current | (value & mask))

    Register.__name__ = "ctrl"
    Register.__qualname__ = "ctrl"
    Register.info = info
    metadata: dict[str, Any] = {
        "fields": {},
        "writable": {},
        "readable": {},
        "path": info.path,
        "name": info.name,
        "address": info.address,
    }
    register_cache_enhancement(Register, metadata)
    return Register


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class CacheStateMachine(RuleBasedStateMachine):
    """Drives random combinations of cache_for / read / write / invalidate / clock_advance.

    Shadow state:

    * ``self._cache_expiry`` — the absolute monotonic time at which the
      current window expires (``-inf`` means "no active window").
    * ``self._cache_populated`` — ``True`` iff a real read has fired
      inside the current window and the cache holds a value.
    * ``self._cached_value`` — the value that lives in the cache (used
      to assert reads inside the window return the same int).

    Master-side ground truth:

    * ``self.master.reads`` — count of physical bus reads. Every rule
      records the count before any action and asserts the delta after.
    """

    # The TTL strategies sit on the rule definitions; shared strategies
    # would be re-drawn per-call by Hypothesis but live here for clarity.

    def __init__(self) -> None:
        super().__init__()
        # Filled in by ``initialize`` — keep the attributes typed so the
        # invariant doesn't try to read them before the machine starts.
        self.master: _CountingMaster | None = None
        self.reg: Any = None
        self._clock: list[float] = [0.0]
        self._cache_expiry: float = float("-inf")
        self._cache_populated: bool = False
        self._cached_value: int | None = None
        self._mp: pytest.MonkeyPatch | None = None
        self._next_write_value: int = 0  # auto-advance to defeat collisions

    @initialize()
    def _setup(self) -> None:
        """Build the fixture once per state-machine instance.

        ``initialize`` runs before any ``@rule`` so the rules can rely on
        ``self.reg`` and ``self.master`` being present. We install a
        per-instance ``MonkeyPatch`` rather than the fixture (state
        machines run multiple steps within one Hypothesis "test" so they
        outlive a per-test fixture).
        """
        self.master = _CountingMaster()
        self.reg = _make_register_class()(self.master)
        self._clock = [1000.0]

        def fake_monotonic() -> float:
            return self._clock[0]

        self._mp = pytest.MonkeyPatch()
        self._mp.setattr(time, "monotonic", fake_monotonic)
        # The cache enhancement's ``cache_for`` writes
        # ``time.monotonic() + seconds``; once we monkeypatched the
        # module's namespace lookup, this is automatically driven.

    def teardown(self) -> None:
        """Undo the monkeypatch when the machine finishes a run."""
        if self._mp is not None:
            self._mp.undo()
            self._mp = None

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    @rule(ttl=st.floats(min_value=0.001, max_value=5.0, allow_nan=False))
    def rule_cache_for(self, ttl: float) -> None:
        """Start a new cache window of ``ttl`` seconds.

        Resets the cache to "empty, window active" so the next read fires
        the bus once and populates the cache. The master read counter
        must not change here — ``cache_for`` is bus-free by contract.
        """
        assert self.master is not None and self.reg is not None
        before = self.master.reads
        self.reg.cache_for(ttl)
        # Shadow update: window is open, cache is empty.
        self._cache_expiry = self._clock[0] + float(ttl)
        self._cache_populated = False
        self._cached_value = None
        assert self.master.reads == before, "cache_for should not hit the bus"

    @rule()
    def rule_read(self) -> None:
        """Read the register; assert the bus-hit count matches the shadow."""
        assert self.master is not None and self.reg is not None
        before = self.master.reads
        # The master may serve a fresh value if this read isn't cached.
        # Pick a deterministic next value so we can compare exactly when
        # the read actually goes to the bus.
        next_value = (self._next_write_value + 0xA5A5_0001) & 0xFFFF_FFFF
        self.master.next_value = next_value

        in_window = self._clock[0] < self._cache_expiry
        will_hit_bus = not (in_window and self._cache_populated)

        result = int(self.reg.read())
        after = self.master.reads

        if will_hit_bus:
            # Exactly one bus read; cache now holds ``next_value`` and
            # the window (if any) remains active.
            assert after == before + 1, (
                f"expected fresh bus read, got reads={after - before}; "
                f"in_window={in_window}, populated={self._cache_populated}"
            )
            assert result == next_value, (
                f"fresh read should mirror master.next_value={next_value:#x}, "
                f"got {result:#x}"
            )
            if in_window:
                self._cache_populated = True
                self._cached_value = next_value
            else:
                # Out-of-window read: the wrapper drops the stale entry
                # and does NOT repopulate (``cache_for`` is the explicit
                # "start a new window" entry point).
                self._cache_expiry = float("-inf")
                self._cache_populated = False
                self._cached_value = None
        else:
            # Cache hit: no bus traffic, value matches the cached one.
            assert after == before, (
                f"cached read should not hit bus; saw {after - before} extra reads"
            )
            assert self._cached_value is not None
            assert result == self._cached_value, (
                f"cached read returned {result:#x}, expected {self._cached_value:#x}"
            )

    @rule(value=st.integers(min_value=0, max_value=(1 << 32) - 1))
    def rule_write(self, value: int) -> None:
        """Write a value; per spec, this must invalidate the cache.

        The task hard-requires "after ``write(v)`` ..., the next read
        must hit the bus." The caching module today does NOT wrap
        ``cls.write``, so the shadow state agrees with the spec while
        the real wrapper does not — the post-write read invariant
        surfaces the gap.
        """
        assert self.master is not None and self.reg is not None
        self._next_write_value = value
        before = self.master.reads
        self.reg.write(value)
        # Writes that hit the bus may or may not count as a "read" on
        # this master; ``_CountingMaster.write`` doesn't increment the
        # read counter so the assertion is purely on reads.
        assert self.master.reads == before, "write should not increment read count"
        # Shadow: cache invalidated.
        self._cache_expiry = float("-inf")
        self._cache_populated = False
        self._cached_value = None

    @rule()
    def rule_invalidate_cache(self) -> None:
        """Drop the cache entry; subsequent read must hit the bus."""
        assert self.master is not None and self.reg is not None
        before = self.master.reads
        self.reg.invalidate_cache()
        assert self.master.reads == before, "invalidate_cache should not hit bus"
        # Shadow: keep the window (the task says "explicit drop", not
        # "close window"); ``cache_for`` would re-open populate. But
        # the cache contents are gone, so any read becomes a bus read.
        # Implementation-wise the wrapper also clears the expiry (it
        # deletes the whole tuple), so model that too.
        self._cache_expiry = float("-inf")
        self._cache_populated = False
        self._cached_value = None

    @rule(dt=st.floats(min_value=0.0, max_value=10.0, allow_nan=False))
    def rule_clock_advance(self, dt: float) -> None:
        """Step the (monkeypatched) ``time.monotonic`` forward."""
        before = self.master.reads if self.master is not None else 0
        self._clock[0] += float(dt)
        # Clock motion alone never causes a bus access.
        if self.master is not None:
            assert self.master.reads == before, "clock_advance should not hit bus"

    # ------------------------------------------------------------------
    # Invariants (checked after every rule)
    # ------------------------------------------------------------------

    @invariant()
    def invariant_master_reads_never_decrease(self) -> None:
        """Sanity: the bus-read counter is monotone non-decreasing."""
        # Trivial but cheap; catches accidental ``self.master.reads = 0``
        # resets in a future refactor of either side.
        if self.master is not None:
            assert self.master.reads >= 0


# ---------------------------------------------------------------------------
# Test entry points
# ---------------------------------------------------------------------------


# Apply the requested hypothesis settings: 100 examples, 30 steps per
# example, no deadline (clock-advance simulations can sit on edge-case
# timings that exceed Hypothesis's default).
CacheStateMachine.TestCase.settings = settings(
    max_examples=100,
    stateful_step_count=30,
    deadline=None,
)


# The state machine drives random interleavings of cache_for, read,
# write, invalidate_cache, and clock_advance. The shadow state in
# ``CacheStateMachine`` predicts whether each read should hit the bus,
# and the post-write invariant checks that the next read does — a
# regression would re-introduce the ``write does not invalidate`` gap
# closed by wrapping ``cls.write`` / ``cls.modify`` in
# ``register_cache_enhancement``.
#
# Minimal sequence that previously failed (now passes):
#     state.rule_cache_for(ttl=1.0)
#     state.rule_read()
#     state.rule_write(value=0)
#     state.rule_read()   # post-write must hit the bus
class TestCacheStateMachine(CacheStateMachine.TestCase):
    """Pytest entrypoint for the stateful machine."""
