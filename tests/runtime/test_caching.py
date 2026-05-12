"""Unit tests for ``peakrdl_pybind11.runtime.caching`` (sketch §13.4).

Builds a small fake SoC with two registers — one cacheable, one volatile —
on top of a recording master, then drives the caching enhancement
directly. Compose-with-the-default-shim is exercised in
``test_layers_compose_with_default_register_shim``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from peakrdl_pybind11.runtime import _registry
from peakrdl_pybind11.runtime._default_shims import _default_register_shim
from peakrdl_pybind11.runtime.caching import (
    attach_cached_context_manager,
    is_cacheable,
    register_cache_enhancement,
)
from peakrdl_pybind11.runtime.errors import NotSupportedError


# ---------------------------------------------------------------------------
# Fixtures: a tiny "bus" plus register classes that record reads.
# ---------------------------------------------------------------------------


class _RecordingMaster:
    """Minimal master used by the fake registers. Counts every ``read`` call."""

    def __init__(self) -> None:
        self.reads = 0
        self.next_value = 0xDEAD_BEEF

    def read(self, address: int) -> int:  # noqa: ARG002 -- address unused
        self.reads += 1
        return self.next_value

    def write(self, address: int, value: int) -> None:  # noqa: ARG002
        # No-op; the cache tests only care about reads.
        pass


def _make_register_class(
    *,
    name: str,
    path: str,
    is_volatile: bool = False,
    on_read: str | None = None,
) -> type:
    """Build a register class whose ``read`` goes through a master.

    The class is structured the way generated code structures registers:
    a ``read(self)``, ``write(self, value)``, ``modify(self, value, mask)``,
    plus ``info`` carrying side-effect metadata, an ``offset``, and a
    ``width`` (bytes) — enough for the default register shim to wrap it.
    """

    info = SimpleNamespace(
        name=name,
        path=path,
        address=0x4000_0000,
        is_volatile=is_volatile,
        on_read=on_read,
    )

    class Register:
        # The default shim consults ``self.offset`` and ``self.width`` (bytes)
        # when constructing the RegisterValue, so populate both.
        offset = 0x4000_0000
        width = 4  # bytes — 32-bit register

        def __init__(self, master: _RecordingMaster) -> None:
            self._master = master
            self.name = name

        def read(self) -> int:
            return self._master.read(self.offset)

        def write(self, value: int) -> None:
            self._master.write(self.offset, value)

        def modify(self, value: int, mask: int) -> None:
            # Read-modify-write through the master; not exercised by the
            # cache tests but the default shim insists the attribute is
            # present.
            current = self._master.read(self.offset) & ~mask
            self._master.write(self.offset, current | (value & mask))

        def write_fields(self, mask: int, value: int) -> None:
            self.modify(value, mask)

    Register.__name__ = name
    Register.__qualname__ = name
    Register.info = info
    return Register


def _attach_caching(reg_cls: type, *, with_default_shim: bool = False) -> None:
    """Run the caching enhancement (and optionally the default shim) on ``reg_cls``."""
    metadata: dict[str, Any] = {
        "fields": {},
        "writable": {},
        "readable": {},
        "path": getattr(reg_cls.info, "path", ""),
        "name": getattr(reg_cls.info, "name", ""),
        "address": getattr(reg_cls.info, "address", 0),
    }
    if with_default_shim:
        _default_register_shim(reg_cls, metadata)
    register_cache_enhancement(reg_cls, metadata)


@pytest.fixture
def master() -> _RecordingMaster:
    return _RecordingMaster()


@pytest.fixture
def cacheable_reg(master: _RecordingMaster) -> Any:
    cls = _make_register_class(name="control", path="soc.control")
    _attach_caching(cls)
    return cls(master)


@pytest.fixture
def volatile_reg(master: _RecordingMaster) -> Any:
    cls = _make_register_class(
        name="status", path="soc.status", is_volatile=True
    )
    _attach_caching(cls)
    return cls(master)


@pytest.fixture
def rclr_reg(master: _RecordingMaster) -> Any:
    cls = _make_register_class(
        name="intr_status", path="soc.intr_status", on_read="rclr"
    )
    _attach_caching(cls)
    return cls(master)


# ---------------------------------------------------------------------------
# Cacheability gating
# ---------------------------------------------------------------------------


class TestCacheability:
    def test_plain_register_is_cacheable(self, cacheable_reg: Any) -> None:
        assert is_cacheable(cacheable_reg) is True

    def test_volatile_register_is_not_cacheable(self, volatile_reg: Any) -> None:
        assert is_cacheable(volatile_reg) is False

    def test_rclr_register_is_not_cacheable(self, rclr_reg: Any) -> None:
        assert is_cacheable(rclr_reg) is False

    def test_cache_for_on_volatile_raises_with_clear_message(
        self, volatile_reg: Any
    ) -> None:
        with pytest.raises(NotSupportedError) as exc:
            volatile_reg.cache_for(1.0)
        msg = str(exc.value)
        assert "soc.status" in msg
        assert "volatile" in msg

    def test_cache_for_on_rclr_raises_with_clear_message(self, rclr_reg: Any) -> None:
        with pytest.raises(NotSupportedError) as exc:
            rclr_reg.cache_for(1.0)
        msg = str(exc.value)
        assert "soc.intr_status" in msg
        assert "rclr" in msg


# ---------------------------------------------------------------------------
# cache_for / invalidate_cache behaviour
# ---------------------------------------------------------------------------


class TestCacheForWindow:
    def test_two_reads_in_window_produce_one_master_read(
        self, master: _RecordingMaster, cacheable_reg: Any
    ) -> None:
        cacheable_reg.cache_for(1.0)
        first = cacheable_reg.read()
        second = cacheable_reg.read()
        assert master.reads == 1
        # Same observed value across both reads inside the window.
        assert int(first) == int(second)

    def test_read_returns_same_value_within_window(
        self, master: _RecordingMaster, cacheable_reg: Any
    ) -> None:
        cacheable_reg.cache_for(1.0)
        master.next_value = 0x1111_2222
        first = cacheable_reg.read()
        # Even if the underlying master would now report a different
        # value, we still see the cached one until expiry.
        master.next_value = 0xFFFF_FFFF
        second = cacheable_reg.read()
        third = cacheable_reg.read()
        assert int(first) == 0x1111_2222
        assert int(second) == 0x1111_2222
        assert int(third) == 0x1111_2222
        assert master.reads == 1

    def test_expired_window_falls_back_to_bus(
        self,
        master: _RecordingMaster,
        cacheable_reg: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Drive ``time.monotonic`` from a list of pre-baked timestamps so
        # we can step past the window expiry deterministically.
        clock = [1000.0]

        def fake_monotonic() -> float:
            return clock[0]

        monkeypatch.setattr(time, "monotonic", fake_monotonic)

        master.next_value = 0xAA
        cacheable_reg.cache_for(0.5)  # window expires at 1000.5
        cacheable_reg.read()
        assert master.reads == 1

        # Advance past the window; the next read must go to the bus.
        clock[0] = 1000.6
        master.next_value = 0xBB
        fresh = cacheable_reg.read()
        assert master.reads == 2
        assert int(fresh) == 0xBB

    def test_invalidate_cache_drops_entry(
        self, master: _RecordingMaster, cacheable_reg: Any
    ) -> None:
        cacheable_reg.cache_for(1.0)
        master.next_value = 0x11
        cacheable_reg.read()
        assert master.reads == 1

        cacheable_reg.invalidate_cache()
        master.next_value = 0x22
        fresh = cacheable_reg.read()
        # Invalidation forces a fresh bus read.
        assert master.reads == 2
        assert int(fresh) == 0x22

        # And caches do not magically re-populate after an invalidate.
        cacheable_reg.read()
        assert master.reads == 3

    def test_invalidate_cache_without_active_cache_is_a_noop(
        self, master: _RecordingMaster, cacheable_reg: Any
    ) -> None:
        # No ``cache_for`` first.
        cacheable_reg.invalidate_cache()
        cacheable_reg.read()
        assert master.reads == 1

    def test_no_cache_means_every_read_hits_the_bus(
        self, master: _RecordingMaster, cacheable_reg: Any
    ) -> None:
        cacheable_reg.read()
        cacheable_reg.read()
        cacheable_reg.read()
        assert master.reads == 3


# ---------------------------------------------------------------------------
# soc.cached(window=...) context manager
# ---------------------------------------------------------------------------


class _FakeSoc:
    """SoC stand-in that exposes child registers and a ``walk(kind=...)``."""

    def __init__(self, **registers: Any) -> None:
        for name, reg in registers.items():
            setattr(self, name, reg)
        self._registers = registers

    def walk(self, kind: str | None = None) -> Any:
        # The caching module's iterator passes ``kind="reg"``; we ignore
        # the actual kind filter here since every child IS a register.
        _ = kind
        return list(self._registers.values())


class TestSocCachedContextManager:
    def test_context_manager_caches_every_cacheable_register(
        self, master: _RecordingMaster
    ) -> None:
        reg_a_cls = _make_register_class(name="reg_a", path="soc.reg_a")
        reg_b_cls = _make_register_class(name="reg_b", path="soc.reg_b")
        _attach_caching(reg_a_cls)
        _attach_caching(reg_b_cls)
        reg_a = reg_a_cls(master)
        reg_b = reg_b_cls(master)
        soc = _FakeSoc(reg_a=reg_a, reg_b=reg_b)
        attach_cached_context_manager(soc)

        with soc.cached(window=0.5):
            reg_a.read()
            reg_a.read()
            reg_b.read()
            reg_b.read()
        # Two bus reads -- one to populate each cache; subsequent reads
        # inside the block came from the cache.
        assert master.reads == 2

    def test_context_manager_exits_invalidate_caches(
        self, master: _RecordingMaster
    ) -> None:
        reg_cls = _make_register_class(name="reg_a", path="soc.reg_a")
        _attach_caching(reg_cls)
        reg = reg_cls(master)
        soc = _FakeSoc(reg_a=reg)
        attach_cached_context_manager(soc)

        with soc.cached(window=10.0):
            reg.read()
        assert master.reads == 1
        # Cache cleared on exit: a read after the block goes to the bus.
        reg.read()
        assert master.reads == 2

    def test_context_manager_skips_uncacheable_registers(
        self, master: _RecordingMaster
    ) -> None:
        reg_ok_cls = _make_register_class(name="reg_ok", path="soc.reg_ok")
        reg_vol_cls = _make_register_class(
            name="reg_vol", path="soc.reg_vol", is_volatile=True
        )
        _attach_caching(reg_ok_cls)
        _attach_caching(reg_vol_cls)
        reg_ok = reg_ok_cls(master)
        reg_vol = reg_vol_cls(master)
        soc = _FakeSoc(reg_ok=reg_ok, reg_vol=reg_vol)
        attach_cached_context_manager(soc)

        # The volatile register is silently skipped -- no exception, even
        # though ``cache_for`` on it directly would raise.
        with soc.cached(window=0.5):
            reg_ok.read()
            reg_ok.read()
            reg_vol.read()
            reg_vol.read()
        # reg_ok: one bus read inside the cache. reg_vol: every read hits
        # the bus because it was never cached.
        assert master.reads == 1 + 2

    def test_context_manager_invalidates_on_exception(
        self, master: _RecordingMaster
    ) -> None:
        reg_cls = _make_register_class(name="reg_a", path="soc.reg_a")
        _attach_caching(reg_cls)
        reg = reg_cls(master)
        soc = _FakeSoc(reg_a=reg)
        attach_cached_context_manager(soc)

        with pytest.raises(RuntimeError):
            with soc.cached(window=10.0):
                reg.read()
                raise RuntimeError("boom")
        # Cache cleared by the ``finally`` clause.
        reg.read()
        # 1 inside the block, 1 after the block.
        assert master.reads == 2


# ---------------------------------------------------------------------------
# Composition with the default register shim
# ---------------------------------------------------------------------------


class TestLayering:
    def test_layers_compose_with_default_register_shim(
        self, master: _RecordingMaster
    ) -> None:
        """Cache enhancement must wrap the default shim's enhanced read.

        The default shim turns the bare C++ ``read(self)`` into one that
        returns a :class:`RegisterValue`; the cache enhancement layers
        on top. A cache hit must therefore yield a RegisterValue equal
        in value to the original read, without going to the bus.
        """
        from peakrdl_pybind11.runtime.values import RegisterValue

        cls = _make_register_class(name="ctrl", path="soc.ctrl")
        _attach_caching(cls, with_default_shim=True)
        reg = cls(master)
        master.next_value = 0x1234_5678

        reg.cache_for(1.0)
        first = reg.read()
        second = reg.read()
        assert master.reads == 1
        assert isinstance(first, RegisterValue)
        # The cache returns the same instance, not a re-wrapped one.
        assert second is first

    def test_raw_read_bypasses_cache(self, master: _RecordingMaster) -> None:
        """``raw=True`` is the documented escape hatch: never cached."""
        cls = _make_register_class(name="ctrl", path="soc.ctrl")
        _attach_caching(cls, with_default_shim=True)
        reg = cls(master)

        reg.cache_for(1.0)
        master.next_value = 0xAA
        first = reg.read()  # caches 0xAA
        assert master.reads == 1
        assert int(first) == 0xAA

        master.next_value = 0xBB
        raw = reg.read(raw=True)  # bypasses cache, hits the bus
        assert master.reads == 2
        assert raw == 0xBB

        # Cache still holds the original entry.
        again = reg.read()
        assert master.reads == 2
        assert int(again) == 0xAA


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


class TestRegistryWiring:
    def test_cache_enhancement_registered_as_register_hook(self) -> None:
        enhancers = _registry.get_register_enhancers()
        assert register_cache_enhancement in enhancers

    def test_cached_context_manager_registered_as_post_create_hook(self) -> None:
        hooks = _registry.get_post_create_hooks()
        assert attach_cached_context_manager in hooks
