"""Tests for ``peakrdl_pybind11.runtime.errors`` (sketch §19, §13.5)."""

from __future__ import annotations

import pytest

from peakrdl_pybind11.runtime.errors import (
    AccessError,
    BusError,
    NotSupportedError,
    RoutingError,
    SideEffectError,
    StaleHandleError,
    WaitTimeoutError,
    did_you_mean,
)


# ---------------------------------------------------------------------------
# AccessError
# ---------------------------------------------------------------------------


class TestAccessError:
    def test_default_message_format(self) -> None:
        err = AccessError("uart.status.tx_ready", "r")
        assert err.node_path == "uart.status.tx_ready"
        assert err.access_mode == "r"
        # Sketch §19 example.
        assert str(err) == "uart.status.tx_ready is sw=r"

    def test_custom_message_overrides_default(self) -> None:
        err = AccessError("uart.status.tx_ready", "r", message="custom")
        assert str(err) == "custom"

    def test_is_exception(self) -> None:
        with pytest.raises(AccessError):
            raise AccessError("foo.bar", "w")


# ---------------------------------------------------------------------------
# SideEffectError
# ---------------------------------------------------------------------------


class TestSideEffectError:
    def test_instantiable_with_path(self) -> None:
        err = SideEffectError("uart.status.rx_overrun")
        assert err.node_path == "uart.status.rx_overrun"
        assert "uart.status.rx_overrun" in str(err)
        assert "no_side_effects" in str(err)

    def test_custom_message(self) -> None:
        err = SideEffectError("foo.bar", message="rclr inside no_side_effects()")
        assert str(err) == "rclr inside no_side_effects()"


# ---------------------------------------------------------------------------
# NotSupportedError
# ---------------------------------------------------------------------------


class TestNotSupportedError:
    def test_message_round_trip(self) -> None:
        err = NotSupportedError("master cannot peek rclr")
        assert err.message == "master cannot peek rclr"
        assert str(err) == "master cannot peek rclr"


# ---------------------------------------------------------------------------
# BusError
# ---------------------------------------------------------------------------


class _FakeMaster:
    def __init__(self, name: str) -> None:
        self.name = name


class TestBusError:
    def test_minimal_construction(self) -> None:
        err = BusError(0x4000_1000, "wr", _FakeMaster("ahb0"))
        assert err.address == 0x4000_1000
        assert err.op == "wr"
        assert err.retries == 0
        assert err.underlying is None

    def test_str_contains_address_in_hex(self) -> None:
        err = BusError(0xDEAD_BEEF, "rd", _FakeMaster("jtag"))
        # The contract: address rendered as 0x{:08x}.
        assert "0xdeadbeef" in str(err)

    def test_str_zero_pads_short_addresses(self) -> None:
        err = BusError(0x42, "wr", _FakeMaster("ahb0"))
        assert "0x00000042" in str(err)

    def test_str_includes_op_master_and_retries(self) -> None:
        err = BusError(0x100, "wr", _FakeMaster("ahb0"), retries=3)
        text = str(err)
        assert "wr" in text
        assert "ahb0" in text
        assert "3 retries" in text

    def test_retry_word_is_singular_for_one(self) -> None:
        err = BusError(0x100, "wr", _FakeMaster("ahb0"), retries=1)
        assert "1 retry" in str(err)

    def test_underlying_summary(self) -> None:
        underlying = TimeoutError("ack timed out")
        err = BusError(
            0x4000_1000,
            "rd",
            _FakeMaster("ahb0"),
            retries=2,
            underlying=underlying,
        )
        text = str(err)
        assert "TimeoutError" in text
        assert "ack timed out" in text

    def test_master_without_name_falls_back_to_type(self) -> None:
        class Anon:
            pass

        err = BusError(0x4000_1000, "wr", Anon())
        assert "Anon" in str(err)

    def test_master_none_renders_placeholder(self) -> None:
        err = BusError(0x4000_1000, "wr", None)
        assert "<no-master>" in str(err)


# ---------------------------------------------------------------------------
# RoutingError
# ---------------------------------------------------------------------------


class TestRoutingError:
    def test_default_message_includes_phrase_and_address(self) -> None:
        err = RoutingError(0xDEAD_BEEF)
        text = str(err)
        assert "no master attached" in text
        assert "0xdeadbeef" in text

    def test_custom_message_overrides_default(self) -> None:
        err = RoutingError(0xDEAD_BEEF, message="explicitly unmapped")
        assert str(err) == "explicitly unmapped"
        # Address still recoverable from the attribute.
        assert err.address == 0xDEAD_BEEF


# ---------------------------------------------------------------------------
# StaleHandleError
# ---------------------------------------------------------------------------


class TestStaleHandleError:
    def test_default_message(self) -> None:
        err = StaleHandleError("uart.control")
        text = str(err)
        assert "uart.control" in text
        assert "stale" in text

    def test_custom_message(self) -> None:
        err = StaleHandleError("uart.control", message="snapshot invalidated")
        assert str(err) == "snapshot invalidated"


# ---------------------------------------------------------------------------
# WaitTimeoutError
# ---------------------------------------------------------------------------


class TestWaitTimeoutError:
    def test_subclass_of_timeout_error(self) -> None:
        err = WaitTimeoutError("uart.status.tx_ready", expected=1, last_seen=0)
        assert isinstance(err, TimeoutError)

    def test_message_includes_expected_and_last_seen(self) -> None:
        err = WaitTimeoutError("uart.status.tx_ready", expected=1, last_seen=0)
        text = str(err)
        assert "uart.status.tx_ready" in text
        assert "1" in text
        assert "last_seen=0" in text

    def test_samples_are_recorded_when_provided(self) -> None:
        err = WaitTimeoutError(
            "uart.status.tx_ready",
            expected=1,
            last_seen=0,
            samples=[0, 0, 1, 0],
        )
        assert err.samples == (0, 0, 1, 0)
        assert "samples=" in str(err)

    def test_samples_default_none(self) -> None:
        err = WaitTimeoutError("uart.status.tx_ready", expected=1, last_seen=0)
        assert err.samples is None


# ---------------------------------------------------------------------------
# did_you_mean
# ---------------------------------------------------------------------------


class TestDidYouMean:
    def test_documented_example(self) -> None:
        # Pulled from the unit's spec.
        assert did_you_mean("enbale", ["enable", "baudrate", "parity"]) == "enable"

    def test_returns_empty_string_when_no_match(self) -> None:
        assert did_you_mean("xyzzy", ["enable", "baudrate", "parity"]) == ""

    def test_exact_match_returned(self) -> None:
        assert did_you_mean("enable", ["enable", "baudrate"]) == "enable"

    def test_empty_candidates_returns_empty_string(self) -> None:
        assert did_you_mean("enable", []) == ""

    def test_accepts_iterable_not_only_list(self) -> None:
        # difflib needs to materialise the candidates; the helper should
        # cope with one-shot iterables (e.g. dict_keys, generators).
        assert did_you_mean("enbale", iter(["enable", "baudrate"])) == "enable"


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_all_exports_listed() -> None:
    from peakrdl_pybind11.runtime import errors

    expected = {
        "AccessError",
        "BusError",
        "NotSupportedError",
        "RoutingError",
        "SideEffectError",
        "StaleHandleError",
        "WaitTimeoutError",
        "did_you_mean",
    }
    assert set(errors.__all__) == expected
    for name in expected:
        assert hasattr(errors, name)


def test_module_docstring_references_section_19() -> None:
    from peakrdl_pybind11.runtime import errors

    assert errors.__doc__ is not None
    assert "§19" in errors.__doc__
