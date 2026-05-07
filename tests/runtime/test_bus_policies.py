"""Tests for the bus-policy bundle (Unit 17).

Covers §13.3 (barriers), §13.4 (cache) and §13.5 (retry) of
``docs/IDEAL_API_SKETCH.md``. The tests build small, deterministic
fixtures rather than depend on the generated SoC tree — the policy bundle
is the unit under test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from peakrdl_pybind11.masters.base import MasterBase
from peakrdl_pybind11.runtime._errors import (
    BusError,
    DisconnectError,
    NackError,
    NotSupportedError,
    TimeoutError,
)
from peakrdl_pybind11.runtime._registry import (
    attach_master_extension,
    get_master_extension_factory,
)
from peakrdl_pybind11.runtime.bus_policies import (
    BusPolicies,
    CallOverride,
    install,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class CountingMaster(MasterBase):
    """Mock master that records calls and lets tests script failure modes."""

    def __init__(self) -> None:
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, int, int]] = []
        self.barriers: int = 0
        self._read_value: int = 0xDEAD_BEEF
        # A queue of exceptions to raise on subsequent reads; once empty,
        # reads succeed and return self._read_value.
        self.read_failures: list[BaseException] = []
        self.write_failures: list[BaseException] = []

    def set_read_value(self, value: int) -> None:
        self._read_value = value

    def queue_read_failures(self, *excs: BaseException) -> None:
        self.read_failures.extend(excs)

    def queue_write_failures(self, *excs: BaseException) -> None:
        self.write_failures.extend(excs)

    def read(self, address: int, width: int) -> int:
        if self.read_failures:
            raise self.read_failures.pop(0)
        self.reads.append((address, width))
        return self._read_value

    def write(self, address: int, value: int, width: int) -> None:
        if self.write_failures:
            raise self.write_failures.pop(0)
        self.writes.append((address, value, width))

    def barrier(self) -> None:
        self.barriers += 1


@dataclass
class _Info:
    """Stand-in for Unit 4's exporter info object (just the bits we need)."""

    is_volatile: bool = False
    on_read: Callable[..., int] | None = None


@pytest.fixture
def master() -> CountingMaster:
    return CountingMaster()


@pytest.fixture
def policies(master: CountingMaster) -> BusPolicies:
    p = install(master)
    # Speed up the retry path so the suite never sleeps for real.
    p.retry._set_sleep(lambda _delay: None)
    return p


# ---------------------------------------------------------------------------
# Barriers (§13.3)
# ---------------------------------------------------------------------------


class TestBarrier:
    def test_explicit_barrier_calls_master(self, master: CountingMaster, policies: BusPolicies) -> None:
        # The user-facing surface is `soc.barrier()`; here we exercise the
        # policy directly. The policy calls master.barrier() once.
        policies.barriers.barrier()
        assert master.barriers == 1

    def test_global_barrier_alias(self, master: CountingMaster, policies: BusPolicies) -> None:
        policies.barriers.global_barrier()
        assert master.barriers == 1

    def test_auto_mode_fences_read_after_write(self, master: CountingMaster, policies: BusPolicies) -> None:
        # Default policy is "auto": same-master barrier before any read
        # that follows a write.
        policies.master.write(0x1000, 0xAA, 4)
        assert master.barriers == 0  # write did not fence
        _ = policies.master.read(0x1000, 4)
        # Exactly one barrier: the implicit fence before the read.
        assert master.barriers == 1

    def test_auto_mode_no_fence_for_consecutive_reads(
        self, master: CountingMaster, policies: BusPolicies
    ) -> None:
        _ = policies.master.read(0x1000, 4)
        _ = policies.master.read(0x1004, 4)
        # No write in between → no implicit barrier.
        assert master.barriers == 0

    def test_none_mode_disables_implicit_fences(self, master: CountingMaster, policies: BusPolicies) -> None:
        policies.barriers.set_mode("none")
        policies.master.write(0x1000, 0xAA, 4)
        _ = policies.master.read(0x1000, 4)
        assert master.barriers == 0

    def test_strict_mode_fences_every_op(self, master: CountingMaster, policies: BusPolicies) -> None:
        policies.barriers.set_mode("strict")
        policies.master.write(0x1000, 0xAA, 4)
        _ = policies.master.read(0x1000, 4)
        # One fence before the write, one before the read.
        assert master.barriers == 2

    def test_invalid_mode_raises(self, policies: BusPolicies) -> None:
        with pytest.raises(ValueError, match="barrier policy"):
            policies.barriers.set_mode("bogus")  # type: ignore[arg-type]

    def test_global_scope_fences_every_peer(self, master: CountingMaster, policies: BusPolicies) -> None:
        peer = CountingMaster()
        policies.barriers.attach_peer(peer)
        policies.barriers.barrier(scope="all")
        assert master.barriers == 1
        assert peer.barriers == 1


# ---------------------------------------------------------------------------
# Cache (§13.4)
# ---------------------------------------------------------------------------


class TestCache:
    def test_cache_for_dedupes_consecutive_reads(self, master: CountingMaster, policies: BusPolicies) -> None:
        master.set_read_value(0x42)
        policies.cache.attach_slot(0x4000, ttl=0.1)
        # Two consecutive reads: only the first should hit the bus.
        v1 = policies.master.read(0x4000, 4)
        v2 = policies.master.read(0x4000, 4)
        assert v1 == 0x42
        assert v2 == 0x42
        assert len(master.reads) == 1

    def test_cache_for_rclr_raises_not_supported(self, policies: BusPolicies) -> None:
        info = _Info(is_volatile=True)  # rclr is volatile by definition
        with pytest.raises(NotSupportedError, match="read side effects"):
            policies.cache.attach_slot(0x5000, ttl=0.1, info=info)

    def test_cache_for_on_read_callback_raises(self, policies: BusPolicies) -> None:
        info = _Info(on_read=lambda *_: 0)
        with pytest.raises(NotSupportedError):
            policies.cache.attach_slot(0x5000, ttl=0.1, info=info)

    def test_cache_invalidate_forces_bus_read(self, master: CountingMaster, policies: BusPolicies) -> None:
        master.set_read_value(0x10)
        policies.cache.attach_slot(0x4000, ttl=10.0)
        _ = policies.master.read(0x4000, 4)
        master.set_read_value(0x20)
        policies.cache.invalidate(0x4000)
        v = policies.master.read(0x4000, 4)
        assert v == 0x20
        assert len(master.reads) == 2

    def test_write_invalidates_cache(self, master: CountingMaster, policies: BusPolicies) -> None:
        master.set_read_value(0x10)
        policies.cache.attach_slot(0x4000, ttl=10.0)
        _ = policies.master.read(0x4000, 4)
        # Writes invalidate the cached value at the same address.
        policies.master.write(0x4000, 0x55, 4)
        master.set_read_value(0x55)
        v = policies.master.read(0x4000, 4)
        assert v == 0x55
        assert len(master.reads) == 2

    def test_block_scope_window(self, master: CountingMaster, policies: BusPolicies) -> None:
        master.set_read_value(0x77)
        # No explicit slot; rely on the block window.
        with policies.cache.cached_window(window=10.0):
            _ = policies.master.read(0x4000, 4)
            _ = policies.master.read(0x4000, 4)
        assert len(master.reads) == 1
        # After exiting the window, the next read goes through.
        _ = policies.master.read(0x4000, 4)
        assert len(master.reads) == 2

    def test_invalidate_all(self, master: CountingMaster, policies: BusPolicies) -> None:
        master.set_read_value(0xA1)
        policies.cache.attach_slot(0x1000, ttl=10.0)
        policies.cache.attach_slot(0x2000, ttl=10.0)
        _ = policies.master.read(0x1000, 4)
        _ = policies.master.read(0x2000, 4)
        assert len(master.reads) == 2
        policies.cache.invalidate(None)  # all slots
        _ = policies.master.read(0x1000, 4)
        _ = policies.master.read(0x2000, 4)
        assert len(master.reads) == 4

    def test_zero_ttl_rejected(self, policies: BusPolicies) -> None:
        with pytest.raises(ValueError, match="cache_for"):
            policies.cache.attach_slot(0x1000, ttl=0.0)


# ---------------------------------------------------------------------------
# Retry (§13.5)
# ---------------------------------------------------------------------------


class TestRetry:
    def test_one_retry_then_success(self, master: CountingMaster, policies: BusPolicies) -> None:
        master.set_read_value(0x99)
        master.queue_read_failures(TimeoutError())
        v = policies.master.read(0x1000, 4)
        assert v == 0x99
        assert policies.retry.last_retry_count == 1

    def test_retries_zero_raises_immediately(self, master: CountingMaster, policies: BusPolicies) -> None:
        policies.retry.configure(retries=0)
        master.queue_read_failures(TimeoutError())
        with pytest.raises(BusError) as ei:
            policies.master.read(0x1000, 4)
        err = ei.value
        assert err.retries == 0
        assert err.op == "read"
        assert err.addr == 0x1000
        assert isinstance(err.underlying, TimeoutError)

    def test_per_call_retry_override(self, master: CountingMaster, policies: BusPolicies) -> None:
        # Default retries=3. Override per-call to retries=0 → first failure
        # propagates as BusError.
        master.queue_read_failures(TimeoutError())
        with pytest.raises(BusError):
            policies.master.read(0x1000, 4, _pe_override=CallOverride(retries=0))

    def test_per_call_higher_retry_override(self, master: CountingMaster, policies: BusPolicies) -> None:
        # Configure default to 0 retries, then override on the call to 3.
        policies.retry.configure(retries=0)
        master.queue_read_failures(TimeoutError(), TimeoutError(), TimeoutError())
        master.set_read_value(0xBE)
        v = policies.master.read(0x1000, 4, _pe_override=CallOverride(retries=3))
        assert v == 0xBE
        assert policies.retry.last_retry_count == 3

    def test_retry_exhaustion_raises_bus_error(self, master: CountingMaster, policies: BusPolicies) -> None:
        policies.retry.configure(retries=2, backoff=0.0)
        master.queue_read_failures(TimeoutError(), TimeoutError(), TimeoutError())
        with pytest.raises(BusError) as ei:
            policies.master.read(0x1000, 4)
        # 2 retries means 3 attempts; on the 3rd failure we give up at
        # attempt index 2.
        assert ei.value.retries == 2

    def test_retry_skips_unmatched_kinds(self, master: CountingMaster, policies: BusPolicies) -> None:
        # Default `on=("timeout", "nack")`. A ValueError is not retryable.
        master.queue_read_failures(ValueError("not a transport error"))
        with pytest.raises(BusError) as ei:
            policies.master.read(0x1000, 4)
        # Zero retries actually performed because the kind never matched.
        assert ei.value.retries == 0
        assert isinstance(ei.value.underlying, ValueError)

    def test_retry_on_nack(self, master: CountingMaster, policies: BusPolicies) -> None:
        master.queue_read_failures(NackError())
        master.set_read_value(0x55)
        v = policies.master.read(0x1000, 4)
        assert v == 0x55
        assert policies.retry.last_retry_count == 1

    def test_on_giveup_log_returns_default(self, master: CountingMaster, policies: BusPolicies) -> None:
        policies.retry.configure(retries=0, on_giveup="log")
        master.queue_read_failures(TimeoutError())
        v = policies.master.read(0x1000, 4)
        assert v == 0  # log mode swallows; read returns 0
        assert policies.retry.last_retry_count == 0

    def test_invalid_on_giveup_rejected(self, policies: BusPolicies) -> None:
        with pytest.raises(ValueError, match="on_giveup"):
            policies.retry.configure(on_giveup="abort")  # type: ignore[arg-type]

    def test_invalid_retries_rejected(self, policies: BusPolicies) -> None:
        with pytest.raises(ValueError, match="retries"):
            policies.retry.configure(retries=-1)


# ---------------------------------------------------------------------------
# on_disconnect (§13.5)
# ---------------------------------------------------------------------------


class TestDisconnect:
    def test_on_disconnect_fires_on_disconnect_kind(
        self, master: CountingMaster, policies: BusPolicies
    ) -> None:
        seen: list[MasterBase] = []
        policies.retry.add_disconnect_callback(lambda m: seen.append(m))
        # disconnect is not in the default `on=` list, so the call still
        # gives up — but the callback runs first.
        master.queue_read_failures(DisconnectError())
        with pytest.raises(BusError):
            policies.master.read(0x1000, 4)
        assert seen == [master]

    def test_on_disconnect_does_not_fire_on_other_kinds(
        self, master: CountingMaster, policies: BusPolicies
    ) -> None:
        seen: list[MasterBase] = []
        policies.retry.add_disconnect_callback(lambda m: seen.append(m))
        master.queue_read_failures(TimeoutError())  # not a disconnect
        master.set_read_value(0x10)
        _ = policies.master.read(0x1000, 4)
        assert seen == []

    def test_disconnect_can_be_added_to_retry_set(
        self, master: CountingMaster, policies: BusPolicies
    ) -> None:
        # Users who want disconnects to retry add them to `on=`.
        policies.retry.configure(on=("disconnect",))
        master.queue_read_failures(DisconnectError())
        master.set_read_value(0xAB)
        v = policies.master.read(0x1000, 4)
        assert v == 0xAB
        assert policies.retry.last_retry_count == 1

    def test_disconnect_callback_failure_does_not_mask_bus_error(
        self, master: CountingMaster, policies: BusPolicies
    ) -> None:
        def bad_cb(_m: MasterBase) -> None:
            raise RuntimeError("reconnect failed")

        policies.retry.add_disconnect_callback(bad_cb)
        master.queue_read_failures(DisconnectError())
        with pytest.raises(BusError):
            policies.master.read(0x1000, 4)


# ---------------------------------------------------------------------------
# Registry seam — Unit 1's `register_master_extension` plumbs to us
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_extension_is_registered_at_import(self) -> None:
        # The module registers the bundle factory under the "bus_policies"
        # name at import time so the SoC layer can attach it without
        # depending on the class.
        factory = get_master_extension_factory("bus_policies")
        assert factory is not None

    def test_attach_master_extension_returns_bundle(self, master: CountingMaster) -> None:
        bundle = attach_master_extension("bus_policies", master)
        assert isinstance(bundle, BusPolicies)
        # Read goes through the wrapped surface now.
        bundle.retry._set_sleep(lambda _d: None)
        master.set_read_value(0x77)
        assert master.read(0x1000, 4) == 0x77


# ---------------------------------------------------------------------------
# Integration — multiple policies layered on the same call
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_cache_short_circuit_skips_retry_and_barrier(
        self, master: CountingMaster, policies: BusPolicies
    ) -> None:
        master.set_read_value(0xCAFE)
        policies.cache.attach_slot(0x4000, ttl=10.0)
        _ = policies.master.read(0x4000, 4)
        # Force a write so the auto-barrier policy would normally fence.
        policies.master.write(0x8000, 0x01, 4)
        assert master.barriers == 0  # writes don't fence in auto mode
        # The cached read at 0x4000 must not fence (cache short-circuits).
        # But the write invalidated 0x8000, not 0x4000, so we still get a
        # cache hit. The barrier count stays at zero.
        master.set_read_value(0xBAD)
        v = policies.master.read(0x4000, 4)
        assert v == 0xCAFE  # cached
        assert master.barriers == 0

    def test_retry_fences_between_attempts(self, master: CountingMaster, policies: BusPolicies) -> None:
        # In strict mode, every attempt fences before issuing the read.
        # The retry loop should still surface the right BusError if every
        # attempt fails.
        policies.barriers.set_mode("strict")
        policies.retry.configure(retries=1, backoff=0.0)
        master.queue_read_failures(TimeoutError(), TimeoutError())
        with pytest.raises(BusError):
            policies.master.read(0x1000, 4)
        # 2 attempts, both fenced, both raised before reaching `master.reads`.
        assert master.barriers == 2

    def test_write_followed_by_read_clears_cache_and_fences(
        self, master: CountingMaster, policies: BusPolicies
    ) -> None:
        policies.cache.attach_slot(0x4000, ttl=10.0)
        master.set_read_value(0x11)
        _ = policies.master.read(0x4000, 4)
        master.set_read_value(0x22)
        policies.master.write(0x4000, 0x99, 4)
        v = policies.master.read(0x4000, 4)
        assert v == 0x22
        # Cache miss → real read → barrier fired (auto mode: write→read).
        assert len(master.reads) == 2
        assert master.barriers == 1
