"""Tests for ``peakrdl_pybind11.runtime.specialized`` (sketch §12)."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from peakrdl_pybind11.runtime.specialized import (
    Counter,
    LockController,
    NotSupportedError,
    ResetMixin,
    attach_counter,
    attach_lock_controller,
    attach_post_create,
    attach_pulse,
    attach_reset_helpers,
    attach_specialized,
    is_at_reset,
    pulse,
    reset_all_regfile,
    reset_all_soc,
    reset_value,
)


# ---------------------------------------------------------------------------
# Mock infrastructure
# ---------------------------------------------------------------------------


@dataclass
class FakeBus:
    """Track every read / write that goes through the mock plumbing."""

    log: list[tuple[str, str, int]] = field(default_factory=list)

    def record_read(self, path: str, value: int) -> None:
        self.log.append(("read", path, value))

    def record_write(self, path: str, value: int) -> None:
        self.log.append(("write", path, value))

    def writes(self) -> list[tuple[str, int]]:
        return [(p, v) for op, p, v in self.log if op == "write"]


@dataclass
class FakeField:
    """A field-like object with read/write that talks to ``FakeBus``."""

    bus: FakeBus
    path: str
    lsb: int
    width: int = 1
    state: int = 0
    info: SimpleNamespace = field(default_factory=lambda: SimpleNamespace())

    def __post_init__(self) -> None:
        if not getattr(self.info, "path", ""):
            self.info.path = self.path
        if not hasattr(self.info, "tags"):
            self.info.tags = SimpleNamespace()

    def read(self) -> int:
        self.bus.record_read(self.path, self.state)
        return self.state

    def write(self, value: int) -> None:
        masked = int(value) & ((1 << self.width) - 1)
        self.state = masked
        self.bus.record_write(self.path, masked)


class FakeRegister:
    """A register-like object that records writes and supports modify."""

    def __init__(
        self,
        bus: FakeBus,
        path: str,
        *,
        reset: int = 0,
        access: str = "rw",
        regwidth: int = 32,
        fields_info: dict[str, Any] | None = None,
        tags: SimpleNamespace | None = None,
    ) -> None:
        self.bus = bus
        self.path = path
        self.state = int(reset)
        self.info = SimpleNamespace(
            path=path,
            name=path.rsplit(".", 1)[-1],
            address=0,
            reset=int(reset),
            access=access,
            regwidth=regwidth,
            fields=fields_info or {},
            tags=tags or SimpleNamespace(),
        )

    def read(self) -> int:
        self.bus.record_read(self.path, self.state)
        return self.state

    def write(self, value: int) -> None:
        self.state = int(value)
        self.bus.record_write(self.path, self.state)

    def modify(self, **kwargs: Any) -> None:
        # Sum of named-field updates. Each child field is set; the host's
        # state is updated to reflect the new combined value.
        for name, value in kwargs.items():
            target = getattr(self, name, None)
            if target is None:
                raise KeyError(name)
            target.state = int(value) & ((1 << target.width) - 1)
        # Recompose state.
        new_state = 0
        for attr_name, attr_val in self.__dict__.items():
            if isinstance(attr_val, FakeField):
                new_state |= (attr_val.state & ((1 << attr_val.width) - 1)) << attr_val.lsb
        self.state = new_state
        self.bus.record_write(self.path, new_state)

    def attach_field(self, name: str, lsb: int, width: int = 1) -> FakeField:
        f = FakeField(self.bus, f"{self.path}.{name}", lsb, width)
        setattr(self, name, f)
        return f


class FakeRegfile:
    """A regfile-like container holding a few registers."""

    def __init__(self, bus: FakeBus, path: str) -> None:
        self.bus = bus
        self.path = path
        self.info = SimpleNamespace(path=path, name=path, tags=SimpleNamespace())
        self._registers: list[FakeRegister] = []

    def attach_register(
        self,
        name: str,
        *,
        reset: int = 0,
        access: str = "rw",
        regwidth: int = 32,
        fields_info: dict[str, Any] | None = None,
    ) -> FakeRegister:
        reg = FakeRegister(
            self.bus,
            f"{self.path}.{name}",
            reset=reset,
            access=access,
            regwidth=regwidth,
            fields_info=fields_info,
        )
        self._registers.append(reg)
        setattr(self, name, reg)
        return reg


class FakeSoc:
    """An SoC-like top-level container holding regfiles."""

    def __init__(self, bus: FakeBus) -> None:
        self.bus = bus
        self.info = SimpleNamespace(path="soc", name="soc", tags=SimpleNamespace())
        self._regfiles: list[FakeRegfile] = []

    def attach_regfile(self, name: str) -> FakeRegfile:
        rf = FakeRegfile(self.bus, name)
        self._regfiles.append(rf)
        setattr(self, name, rf)
        return rf


# ---------------------------------------------------------------------------
# Counter — value / reset / increment
# ---------------------------------------------------------------------------


class TestCounter:
    def test_value_returns_current_count_via_one_read(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "p.event_counter")
        host.state = 7
        c = Counter(host)
        assert c.value() == 7
        assert bus.log == [("read", "p.event_counter", 7)]

    def test_reset_writes_zero_by_default(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "p.event_counter")
        host.state = 99
        Counter(host).reset()
        assert host.state == 0
        assert bus.writes() == [("p.event_counter", 0)]

    def test_reset_uses_custom_reset_method(self) -> None:
        called: list[int] = []
        bus = FakeBus()
        host = FakeRegister(bus, "p.cnt")

        def custom_reset() -> None:
            called.append(1)
            host.write(0xDEAD)

        Counter(host, reset_method=custom_reset).reset()
        assert called == [1]
        assert bus.writes() == [("p.cnt", 0xDEAD)]

    def test_increment_with_named_field_and_amount(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "p.cnt")
        host.attach_field("incrvalue", lsb=8, width=8)

        c = Counter(host, can_increment=True, incrvalue_field="incrvalue")
        c.increment(by=2)

        # increment via modify() -> records one host-level write.
        writes = bus.writes()
        assert ("p.cnt", 2 << 8) in writes

    def test_increment_default_by_one(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "p.cnt")
        host.attach_field("incrvalue", lsb=0, width=4)
        c = Counter(host, can_increment=True, incrvalue_field="incrvalue")
        c.increment()
        assert ("p.cnt", 1) in bus.writes()

    def test_increment_raises_when_unsupported(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "p.cnt")
        c = Counter(host, can_increment=False)
        with pytest.raises(NotSupportedError, match="software increment"):
            c.increment()

    def test_increment_negative_amount_rejected(self) -> None:
        c = Counter(FakeRegister(FakeBus(), "p.cnt"), can_increment=True)
        with pytest.raises(ValueError):
            c.increment(by=-1)

    def test_decrement_writes_correct_amount(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "p.cnt")
        host.attach_field("decrvalue", lsb=16, width=8)
        c = Counter(host, can_decrement=True, decrvalue_field="decrvalue")
        c.decrement(by=3)
        assert ("p.cnt", 3 << 16) in bus.writes()

    def test_decrement_raises_when_unsupported(self) -> None:
        c = Counter(FakeRegister(FakeBus(), "p.cnt"), can_decrement=False)
        with pytest.raises(NotSupportedError, match="software decrement"):
            c.decrement()

    def test_threshold_returns_static_value(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "p.cnt")
        c = Counter(host, threshold=True, threshold_value=42)
        assert c.threshold() == 42

    def test_threshold_raises_without_property(self) -> None:
        c = Counter(FakeRegister(FakeBus(), "p.cnt"))
        with pytest.raises(NotSupportedError, match="incrthreshold"):
            c.threshold()

    def test_is_saturated_compares_against_explicit_ceiling(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "p.cnt")
        host.state = 0xFE
        c = Counter(host, saturate=True, saturate_value=0xFF)
        assert c.is_saturated() is False
        host.state = 0xFF
        assert c.is_saturated() is True

    def test_is_saturated_uses_regwidth_when_no_explicit_ceiling(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "p.cnt", regwidth=8)
        host.state = 0xFF
        c = Counter(host, saturate=True)
        assert c.is_saturated() is True

    def test_is_saturated_raises_without_property(self) -> None:
        c = Counter(FakeRegister(FakeBus(), "p.cnt"))
        with pytest.raises(NotSupportedError, match="incrsaturate"):
            c.is_saturated()

    def test_increment_with_no_modify_falls_back_to_attribute_write(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "p.cnt")
        host.attach_field("incrvalue", lsb=0, width=8)
        # Strip ``modify`` so the wrapper falls back to attribute-style.
        host.modify = None  # type: ignore[assignment]
        Counter(host, can_increment=True, incrvalue_field="incrvalue").increment(by=4)
        # The fallback writes through the named field directly.
        assert ("p.cnt.incrvalue", 4) in bus.writes()

    def test_attach_counter_binds_wrapper_on_node(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "p.cnt")
        wrapper = attach_counter(host, can_increment=True)
        assert host.counter is wrapper
        assert isinstance(wrapper, Counter)
        assert "Counter" in repr(wrapper)


# ---------------------------------------------------------------------------
# Singlepulse
# ---------------------------------------------------------------------------


class TestPulse:
    def test_pulse_function_writes_one(self) -> None:
        bus = FakeBus()
        f = FakeField(bus, "p.start", lsb=0)
        pulse(f)
        assert bus.writes() == [("p.start", 1)]

    def test_attach_pulse_skips_when_not_singlepulse(self) -> None:
        class FieldClass:
            pass

        attached = attach_pulse(FieldClass)
        assert attached is False
        assert getattr(FieldClass, "pulse", None) is None

    def test_attach_pulse_via_class_attribute(self) -> None:
        class FieldClass:
            singlepulse = True

            def __init__(self) -> None:
                self.bus = FakeBus()

            def write(self, value: int) -> None:
                self.bus.record_write("f", int(value))

        assert attach_pulse(FieldClass) is True
        instance = FieldClass()
        instance.pulse()
        assert instance.bus.writes() == [("f", 1)]

    def test_attach_pulse_via_info_tags(self) -> None:
        class FieldClass:
            info = SimpleNamespace(tags=SimpleNamespace(singlepulse=True))

            def __init__(self) -> None:
                self.bus = FakeBus()

            def write(self, value: int) -> None:
                self.bus.record_write("f", int(value))

        assert attach_pulse(FieldClass) is True

    def test_attach_pulse_idempotent(self) -> None:
        class FieldClass:
            singlepulse = True

            def write(self, value: int) -> None:
                pass

        assert attach_pulse(FieldClass) is True
        # Second call: pulse already attached -> no-op.
        assert attach_pulse(FieldClass) is False


# ---------------------------------------------------------------------------
# Reset semantics
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_value_reads_info(self) -> None:
        reg = FakeRegister(FakeBus(), "uart.control", reset=0xDEAD)
        assert reset_value(reg) == 0xDEAD

    def test_reset_value_falls_back_to_zero(self) -> None:
        reg = SimpleNamespace()  # no .info, no _reset_value
        assert reset_value(reg) == 0

    def test_is_at_reset_compares_to_static(self) -> None:
        reg = FakeRegister(FakeBus(), "uart.control", reset=0x1234)
        # state initialised to reset → matches.
        assert is_at_reset(reg) is True
        reg.state = 0x9999
        assert is_at_reset(reg) is False

    def test_attach_reset_helpers_adds_property_and_method(self) -> None:
        class _RegClass:
            def __init__(self) -> None:
                self.info = SimpleNamespace(reset=0x42)
                self.state = 0x42

            def read(self) -> int:
                return self.state

        attach_reset_helpers(_RegClass)
        instance = _RegClass()
        assert instance.reset_value == 0x42
        assert instance.is_at_reset() is True

    def test_reset_all_regfile_writes_to_each_writable_reg(self) -> None:
        bus = FakeBus()
        rf = FakeRegfile(bus, "uart")
        rf.attach_register("control", reset=0xDEAD)
        rf.attach_register("status", reset=0x0, access="r")  # read-only
        rf.attach_register("baudrate", reset=0xBEEF)

        # mutate state away from reset before resetting
        rf.control.state = 0
        rf.baudrate.state = 0

        written = reset_all_regfile(rf, rw_only=True)
        assert written == 2  # control + baudrate; status skipped (ro)

        # control + baudrate should be at reset; status untouched.
        writes = bus.writes()
        assert ("uart.control", 0xDEAD) in writes
        assert ("uart.baudrate", 0xBEEF) in writes
        assert all(p != "uart.status" for p, _ in writes)

    def test_reset_all_regfile_ignores_rw_only_when_false(self) -> None:
        bus = FakeBus()
        rf = FakeRegfile(bus, "uart")
        rf.attach_register("status", reset=0x0, access="r")
        written = reset_all_regfile(rf, rw_only=False)
        # Even read-only registers attempted; FakeRegister has write so it succeeds.
        assert written == 1

    def test_reset_all_regfile_handles_missing_write(self) -> None:
        bus = FakeBus()
        rf = FakeRegfile(bus, "uart")
        rf.attach_register("control", reset=0x1)
        # Replace .write with a callable that raises.
        original_write = rf.control.write

        def boom(_value: int) -> None:
            raise RuntimeError("simulated bus failure")

        rf.control.write = boom  # type: ignore[assignment]
        written = reset_all_regfile(rf, rw_only=False)
        assert written == 0  # write raised; counted as skipped
        # Restore so teardown doesn't affect other tests
        rf.control.write = original_write  # type: ignore[assignment]

    def test_reset_all_soc_walks_full_tree(self) -> None:
        bus = FakeBus()
        soc = FakeSoc(bus)
        rf_a = soc.attach_regfile("uart")
        rf_a.attach_register("control", reset=0x1)
        rf_a.attach_register("baudrate", reset=0x2)
        rf_b = soc.attach_regfile("spi")
        rf_b.attach_register("control", reset=0x3)

        rf_a.control.state = 0
        rf_a.baudrate.state = 0
        rf_b.control.state = 0

        written = reset_all_soc(soc)
        assert written == 3
        writes = dict(bus.writes())
        assert writes["uart.control"] == 0x1
        assert writes["uart.baudrate"] == 0x2
        assert writes["spi.control"] == 0x3

    def test_reset_all_soc_warns_on_rclr_rw_field(self) -> None:
        bus = FakeBus()
        soc = FakeSoc(bus)
        rf = soc.attach_regfile("uart")
        # Build a register whose info reports an RW field with on_read=rclr.
        reg = rf.attach_register(
            "status",
            reset=0x0,
            fields_info={
                "rx_overrun": SimpleNamespace(on_read="rclr", access="rw"),
            },
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            reset_all_soc(soc)
        messages = [str(w.message) for w in caught]
        assert any("uart.status" in m for m in messages)
        # Sanity: we still issue the writes.
        assert ("uart.status", 0x0) in bus.writes()

    def test_attach_post_create_binds_reset_all_on_soc(self) -> None:
        bus = FakeBus()
        soc = FakeSoc(bus)
        rf = soc.attach_regfile("uart")
        rf.attach_register("control", reset=0x55)
        rf.control.state = 0
        attach_post_create(soc)
        assert callable(soc.reset_all)
        soc.reset_all()
        assert ("uart.control", 0x55) in bus.writes()

    def test_reset_mixin_methods(self) -> None:
        class _Reg(ResetMixin):
            def __init__(self) -> None:
                self.info = SimpleNamespace(reset=0xAA)
                self.state = 0xAA

            def read(self) -> int:
                return self.state

        r = _Reg()
        assert r.reset_value == 0xAA
        assert r.is_at_reset() is True


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


class TestLockController:
    def _build(
        self,
        bus: FakeBus | None = None,
        *,
        unlock: Callable[[Any], None] | None = None,
    ) -> tuple[FakeRegister, LockController]:
        bus = bus or FakeBus()
        host = FakeRegister(bus, "gpio_a.lckr")
        host.attach_field("pin0", lsb=0)
        host.attach_field("pin5", lsb=5)
        host.attach_field("LCKK", lsb=16)
        controller = LockController(
            host,
            key_field="LCKK",
            key_sequence=(1, 0, 1),
            unlock_sequence_fn=unlock,
        )
        return host, controller

    def test_lock_writes_lock_bits_and_runs_key_sequence(self) -> None:
        bus = FakeBus()
        host, controller = self._build(bus)
        controller.lock(["pin0"])

        writes = bus.writes()
        # Lock-bits write (host-level via modify) + 3 key-field writes.
        host_writes = [(p, v) for p, v in writes if p == "gpio_a.lckr"]
        assert len(host_writes) == 1  # one lock-bits write through modify()
        key_writes = [(p, v) for p, v in writes if p == "gpio_a.lckr.LCKK"]
        assert key_writes == [
            ("gpio_a.lckr.LCKK", 1),
            ("gpio_a.lckr.LCKK", 0),
            ("gpio_a.lckr.LCKK", 1),
        ]

    def test_lock_minimum_two_writes_with_default_key_sequence(self) -> None:
        # Minimum bus traffic per sketch §12.4: at least one lock-bit write +
        # one key-field write = two writes.
        bus = FakeBus()
        host = FakeRegister(bus, "gpio_a.lckr")
        host.attach_field("pin0", lsb=0)
        host.attach_field("LCKK", lsb=16)
        LockController(host, key_field="LCKK", key_sequence=(1,)).lock(["pin0"])
        writes = bus.writes()
        # Exactly: one host modify + one key write = 2.
        assert len(writes) == 2

    def test_is_locked_returns_field_state(self) -> None:
        host, controller = self._build()
        host.pin0.state = 1
        host.pin5.state = 0
        assert controller.is_locked("pin0") is True
        assert controller.is_locked("pin5") is False

    def test_is_locked_after_lock_call(self) -> None:
        bus = FakeBus()
        host, controller = self._build(bus)
        controller.lock(["pin0"])
        # FakeRegister.modify() sets pin0.state=1
        assert controller.is_locked("pin0") is True

    def test_is_locked_unknown_field_raises(self) -> None:
        _, controller = self._build()
        with pytest.raises(AttributeError):
            controller.is_locked("nonexistent")

    def test_lock_empty_names_rejected(self) -> None:
        _, controller = self._build()
        with pytest.raises(ValueError):
            controller.lock([])

    def test_unlock_sequence_default_raises(self) -> None:
        _, controller = self._build()
        with pytest.raises(NotSupportedError, match="unlock_sequence"):
            controller.unlock_sequence()

    def test_unlock_sequence_custom_runs(self) -> None:
        called: list[Any] = []

        def runner(host: Any) -> None:
            called.append(host)

        host, controller = self._build(unlock=runner)
        controller.unlock_sequence()
        assert called == [host]

    def test_lock_with_field_setter_callback(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "gpio_a.lckr")
        host.attach_field("pin0", lsb=0)
        host.attach_field("LCKK", lsb=16)

        seen: list[tuple[Any, list[str], bool]] = []

        def setter(target: Any, names: list[str], on: bool) -> None:
            seen.append((target, list(names), on))
            target.write(0xCAFE)  # one host write of our choosing

        controller = LockController(
            host,
            key_field="LCKK",
            key_sequence=(1,),
            field_setter=setter,
        )
        controller.lock(["pin0"])
        assert seen == [(host, ["pin0"], True)]
        assert ("gpio_a.lckr", 0xCAFE) in bus.writes()

    def test_attach_lock_controller_binds_methods(self) -> None:
        bus = FakeBus()
        host = FakeRegister(bus, "gpio_a.lckr")
        host.attach_field("pin0", lsb=0)
        host.attach_field("LCKK", lsb=16)
        attach_lock_controller(host, key_field="LCKK", key_sequence=(1,))
        assert callable(host.lock)
        assert callable(host.is_locked)
        assert callable(host.unlock_sequence)
        host.lock(["pin0"])
        assert host.is_locked("pin0") is True


# ---------------------------------------------------------------------------
# Registry-seam integration (attach_specialized) — exercises the per-class
# wiring used by the generated runtime.
# ---------------------------------------------------------------------------


class TestAttachSpecialized:
    def test_reset_helpers_always_attached(self) -> None:
        class _RegClass:
            pass

        attach_specialized(_RegClass, {})
        assert "reset_value" in _RegClass.__dict__
        assert "is_at_reset" in _RegClass.__dict__

    def test_counter_property_attached_when_metadata_present(self) -> None:
        class _CntReg:
            def __init__(self) -> None:
                self.bus = FakeBus()
                self.state = 0

            def read(self) -> int:
                return self.state

            def write(self, value: int) -> None:
                self.state = int(value)
                self.bus.record_write("cnt", self.state)

            def modify(self, **kwargs: Any) -> None:
                # Single-field counter: increment writes the named field.
                # We accept whatever name matches incrvalue_field.
                self.state = int(next(iter(kwargs.values())))
                self.bus.record_write("cnt", self.state)

        attach_specialized(
            _CntReg,
            {"counter": {"can_increment": True, "incrvalue_field": "incrvalue"}},
        )
        instance = _CntReg()
        assert isinstance(instance.counter, Counter)
        instance.counter.increment(by=5)
        assert ("cnt", 5) in instance.bus.writes()
        # Property is cached: same wrapper on second access.
        assert instance.counter is instance.counter

    def test_lock_methods_attached_when_metadata_present(self) -> None:
        class _LockReg:
            def __init__(self) -> None:
                self.bus = FakeBus()
                self.state = 0
                self.pin0 = FakeField(self.bus, "lckr.pin0", lsb=0)
                self.LCKK = FakeField(self.bus, "lckr.LCKK", lsb=16)

            def read(self) -> int:
                return self.state

            def write(self, value: int) -> None:
                self.state = int(value)
                self.bus.record_write("lckr", self.state)

            def modify(self, **kwargs: Any) -> None:
                for name, value in kwargs.items():
                    target = getattr(self, name)
                    target.state = int(value) & ((1 << target.width) - 1)
                self.bus.record_write("lckr", -1)  # marker for combined write

        attach_specialized(
            _LockReg,
            {"lock": {"key_field": "LCKK", "key_sequence": (1,)}},
        )
        instance = _LockReg()
        assert callable(instance.lock)
        instance.lock(["pin0"])
        assert instance.is_locked("pin0") is True

    def test_singlepulse_field_pulse_attached(self) -> None:
        class _Field:
            singlepulse = True

            def __init__(self) -> None:
                self.bus = FakeBus()

            def write(self, value: int) -> None:
                self.bus.record_write("start", int(value))

        class _Reg:
            start = _Field

        attach_specialized(_Reg, {"singlepulse_fields": ("start",)})
        # The field class should now have a pulse method attached.
        assert callable(getattr(_Reg.start, "pulse", None))


# ---------------------------------------------------------------------------
# Smoke test: the public API can be imported and exercised end-to-end.
# ---------------------------------------------------------------------------


def test_public_api_smoke() -> None:
    bus = FakeBus()
    soc = FakeSoc(bus)
    uart = soc.attach_regfile("uart")
    uart.attach_register("control", reset=0xAB)
    uart.control.state = 0
    attach_post_create(soc)

    soc.reset_all()
    assert is_at_reset(uart.control) is True
    assert reset_value(uart.control) == 0xAB
