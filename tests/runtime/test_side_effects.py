"""Unit tests for ``peakrdl_pybind11.runtime.side_effects`` (sketch §11).

These tests are intentionally pure-Python: every "field" / "register" / "master"
is a small mock built from ``types.SimpleNamespace`` so the side-effect helpers
can be exercised without compiling any generated C++ binding.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from peakrdl_pybind11.runtime.side_effects import (
    NotSupportedError,
    SideEffectError,
    acknowledge,
    check_read_allowed,
    clear,
    no_side_effects,
    peek,
    pulse,
    set_,
)


# ---------------------------------------------------------------------------
# Tiny helpers for building targets.
# ---------------------------------------------------------------------------
class _Master:
    """Minimal master that records calls; ``can_peek`` toggles peek support."""

    def __init__(self, *, can_peek: bool = False, peek_value: int = 0) -> None:
        self.can_peek = can_peek
        self.peek_value = peek_value
        self.peeks: list[tuple[int, int]] = []

    def peek(self, addr: int, width: int) -> int:
        self.peeks.append((addr, width))
        return self.peek_value


def _make_field(
    *,
    on_read: str | None = None,
    on_write: str | None = None,
    width: int = 1,
    lsb: int = 0,
    path: str = "soc.foo.bar",
    address: int | None = 0x1000,
    singlepulse: bool = False,
    master: Any = None,
) -> SimpleNamespace:
    """Build a duck-typed field with a recording ``write`` / ``read``."""
    calls: dict[str, list[Any]] = {"write": [], "read": []}

    def write(value: int) -> None:
        calls["write"].append(value)

    def read() -> int:
        calls["read"].append(None)
        return 0

    info = SimpleNamespace(
        on_read=on_read,
        on_write=on_write,
        width=width,
        lsb=lsb,
        path=path,
        address=address,
        singlepulse=singlepulse,
    )
    return SimpleNamespace(info=info, write=write, read=read, master=master, calls=calls)


# ---------------------------------------------------------------------------
# clear() dispatch
# ---------------------------------------------------------------------------
def test_clear_woclr_writes_one() -> None:
    f = _make_field(on_write="woclr")
    clear(f)
    assert f.calls["write"] == [1]


def test_clear_wclr_writes_all_ones() -> None:
    """``wclr`` accepts any write; we send all-ones for determinism."""
    f = _make_field(on_write="wclr", width=8)
    clear(f)
    assert f.calls["write"] == [0xFF]


def test_clear_wzc_writes_zero() -> None:
    f = _make_field(on_write="wzc")
    clear(f)
    assert f.calls["write"] == [0]


def test_clear_rclr_does_a_read() -> None:
    """When the only clear path is a destructive read, ``clear`` reads."""
    f = _make_field(on_read="rclr")
    clear(f)
    assert f.calls["read"] == [None]
    assert f.calls["write"] == []


def test_clear_no_path_raises() -> None:
    f = _make_field(on_read=None, on_write=None, path="soc.no_clear")
    with pytest.raises(NotSupportedError) as exc:
        clear(f)
    assert "soc.no_clear" in str(exc.value)
    assert "no clear path" in str(exc.value)


def test_acknowledge_is_alias_for_clear() -> None:
    f = _make_field(on_write="woclr")
    acknowledge(f)
    assert f.calls["write"] == [1]


# ---------------------------------------------------------------------------
# set_() dispatch
# ---------------------------------------------------------------------------
def test_set_woset_writes_one() -> None:
    f = _make_field(on_write="woset")
    set_(f)
    assert f.calls["write"] == [1]


def test_set_wset_writes_all_ones() -> None:
    f = _make_field(on_write="wset", width=4)
    set_(f)
    assert f.calls["write"] == [0xF]


def test_set_wzs_writes_zero() -> None:
    f = _make_field(on_write="wzs")
    set_(f)
    assert f.calls["write"] == [0]


def test_set_no_on_write_raises() -> None:
    f = _make_field(on_write=None, path="soc.unset")
    with pytest.raises(NotSupportedError) as exc:
        set_(f)
    assert "soc.unset" in str(exc.value)
    assert "no set path" in str(exc.value)


# ---------------------------------------------------------------------------
# pulse()
# ---------------------------------------------------------------------------
def test_pulse_singlepulse_writes_one() -> None:
    f = _make_field(singlepulse=True)
    pulse(f)
    assert f.calls["write"] == [1]


def test_pulse_non_singlepulse_raises() -> None:
    f = _make_field(path="soc.notpulse")
    with pytest.raises(NotSupportedError) as exc:
        pulse(f)
    assert "soc.notpulse" in str(exc.value)
    assert "singlepulse" in str(exc.value)


# ---------------------------------------------------------------------------
# peek()
# ---------------------------------------------------------------------------
def test_peek_master_without_peek_raises() -> None:
    """``rclr`` field on a master that cannot peek -> ``NotSupportedError``."""
    f = _make_field(on_read="rclr", master=_Master(can_peek=False))
    with pytest.raises(NotSupportedError) as exc:
        peek(f)
    assert "cannot peek" in str(exc.value)


def test_peek_master_with_peek_returns_value() -> None:
    """When the master can peek, ``peek`` slices the field bits out."""
    master = _Master(can_peek=True, peek_value=0b101_0)
    f = _make_field(on_read="rclr", width=4, lsb=0, master=master)
    # Field is bits [3:0] of register; peek_value = 0b1010 -> 10.
    assert peek(f) == 10
    assert master.peeks == [(0x1000, 4)]


def test_peek_no_side_effect_field_uses_read() -> None:
    """When there's no read-side effect, ``peek`` falls back to the field's
    own ``read()``. No master needed."""
    f = _make_field(on_read=None)
    # The mock read() returns 0; pulse() unrelated. Just exercise the path.
    assert peek(f) == 0


# ---------------------------------------------------------------------------
# no_side_effects() context manager
# ---------------------------------------------------------------------------
def test_no_side_effects_blocks_rclr_read() -> None:
    """Inside the guard, ``check_read_allowed`` on an rclr field raises."""
    f = _make_field(on_read="rclr", path="soc.intr_status.tx_done")
    with no_side_effects(soc=None):
        with pytest.raises(SideEffectError) as exc:
            check_read_allowed(f)
    assert "soc.intr_status.tx_done" in str(exc.value)
    assert "rclr" in str(exc.value)


def test_no_side_effects_does_not_block_clean_read() -> None:
    """Fields without a read-side effect are unaffected by the guard."""
    f = _make_field(on_read=None)
    with no_side_effects():
        check_read_allowed(f)  # must not raise


def test_no_side_effects_unwinds_on_exit() -> None:
    """The flag is restored even when the block raises."""
    f = _make_field(on_read="rclr")
    try:
        with no_side_effects():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # Outside the block: no guard, no exception.
    check_read_allowed(f)


def test_no_side_effects_nested_restores_outer_state() -> None:
    """Nested guards restore the prior flag value, not just ``False``."""
    f = _make_field(on_read="rclr")
    with no_side_effects():
        with no_side_effects():
            with pytest.raises(SideEffectError):
                check_read_allowed(f)
        # Inner block exited: outer guard still active.
        with pytest.raises(SideEffectError):
            check_read_allowed(f)
    # Outermost block exited: guard is off.
    check_read_allowed(f)


# ---------------------------------------------------------------------------
# Enhancement seam: ``cls.clear()`` etc. become methods on registers/fields.
# ---------------------------------------------------------------------------
def test_enhancement_binds_methods_on_class() -> None:
    """The decorator-style enhancement attaches the verbs to a class."""
    from peakrdl_pybind11.runtime.side_effects import _enhance

    class FakeReg:
        def __init__(self, info: Any) -> None:
            self.info = info
            self.writes: list[int] = []

        def write(self, value: int) -> None:
            self.writes.append(value)

        def read(self) -> int:
            return 0

    _enhance(FakeReg, metadata=None)
    info = SimpleNamespace(
        on_read=None, on_write="woclr", width=1, lsb=0, path="reg",
        address=0, singlepulse=False,
    )
    reg = FakeReg(info)
    reg.clear()
    assert reg.writes == [1]

    # ``set`` is bound as a method (not the builtin) -- exercise it via woset.
    info.on_write = "woset"
    reg.set()
    assert reg.writes == [1, 1]

    # ``pulse`` requires singlepulse metadata.
    info.singlepulse = True
    info.on_write = None
    reg.pulse()
    assert reg.writes == [1, 1, 1]
