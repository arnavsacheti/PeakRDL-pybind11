"""Tests for :class:`MockMasterEx` (Unit 21 of the API overhaul).

These tests exercise the pure-Python hook surface — they never touch the
generated C++ extension, so they run on every platform without requiring
a build.  Numpy is optional (used only by ``preload``); if it's missing
those tests are skipped instead of erroring out.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from peakrdl_pybind11.masters import MockMasterEx

# ---- minimal stand-ins for the (still-WIP) reg/info handles -------------


@dataclass
class _Info:
    """The bits of ``reg.info`` we exercise here.

    Mirrors the surface from §11.2 of the API sketch — ``address`` plus
    the side-effect tags — without dragging in the full elaborated
    register handle, which would require a generated module.
    """

    address: int
    on_read: str | None = None
    on_write: str | None = None


@dataclass
class _Reg:
    """A test-double register handle exposing ``.info`` like the real one."""

    info: _Info


# ---- on_read ------------------------------------------------------------


class TestOnRead:
    def test_raw_address_hook_returns_synthetic_value(self) -> None:
        mock = MockMasterEx()
        mock.on_read(0x1000, lambda a: 0x42)
        assert mock.read(0x1000, 4) == 0x42

    def test_hook_receives_address(self) -> None:
        mock = MockMasterEx()
        captured: list[int] = []

        def hook(addr: int) -> int:
            captured.append(addr)
            return addr & 0xFF

        mock.on_read(0x2000, hook)
        assert mock.read(0x2000, 4) == 0x00
        assert captured == [0x2000]

    def test_hook_value_is_masked_to_width(self) -> None:
        mock = MockMasterEx()
        mock.on_read(0x3000, lambda a: 0x1_FFFF_FFFF)  # 33 bits
        assert mock.read(0x3000, 4) == 0xFFFF_FFFF
        assert mock.read(0x3000, 2) == 0xFFFF  # masked again on narrower read

    def test_addresses_without_hooks_fall_back_to_storage(self) -> None:
        mock = MockMasterEx()
        mock.write(0x4000, 0xDEAD_BEEF, 4)
        mock.on_read(0x5000, lambda a: 0x42)
        assert mock.read(0x4000, 4) == 0xDEAD_BEEF
        assert mock.read(0x5000, 4) == 0x42

    def test_register_handle_resolves_address(self) -> None:
        mock = MockMasterEx()
        reg = _Reg(info=_Info(address=0x6000))
        mock.on_read(reg, lambda a: 0xCAFE)
        assert mock.read(0x6000, 4) == 0xCAFE


# ---- on_write -----------------------------------------------------------


class TestOnWrite:
    def test_writes_are_captured(self) -> None:
        mock = MockMasterEx()
        capture: list[int] = []
        mock.on_write(0x1004, lambda a, v: capture.append(v))
        mock.write(0x1004, 0x11, 4)
        mock.write(0x1004, 0x22, 4)
        mock.write(0x1004, 0x33, 4)
        assert capture == [0x11, 0x22, 0x33]

    def test_hook_receives_address_and_value(self) -> None:
        mock = MockMasterEx()
        capture: list[tuple[int, int]] = []
        mock.on_write(0x2004, lambda a, v: capture.append((a, v)))
        mock.write(0x2004, 0xABCD, 4)
        assert capture == [(0x2004, 0xABCD)]

    def test_storage_still_updated_so_subsequent_read_observes_value(self) -> None:
        mock = MockMasterEx()
        mock.on_write(0x3004, lambda a, v: None)
        mock.write(0x3004, 0xBEEF, 4)
        assert mock.read(0x3004, 4) == 0xBEEF

    def test_register_handle_resolves_address(self) -> None:
        mock = MockMasterEx()
        capture: list[int] = []
        reg = _Reg(info=_Info(address=0x4004))
        mock.on_write(reg, lambda a, v: capture.append(v))
        mock.write(0x4004, 0x55, 4)
        assert capture == [0x55]


# ---- preload ------------------------------------------------------------


class TestPreload:
    def test_preload_with_plain_iterable(self) -> None:
        mock = MockMasterEx()
        mock.preload(0x4000, range(16))
        for i in range(16):
            assert mock.read(0x4000 + i * 4, 4) == i

    def test_preload_with_register_handle_uses_info_address(self) -> None:
        mock = MockMasterEx()
        mem = _Reg(info=_Info(address=0x8000))
        mock.preload(mem, [0x10, 0x20, 0x30])
        assert mock.read(0x8000, 4) == 0x10
        assert mock.read(0x8004, 4) == 0x20
        assert mock.read(0x8008, 4) == 0x30

    def test_preload_with_numpy_array(self) -> None:
        np = pytest.importorskip("numpy")
        mock = MockMasterEx()
        mock.preload(0x4000, np.arange(16, dtype=np.uint32))
        for i in range(16):
            assert mock.read(0x4000 + i * 4, 4) == i

    def test_preload_custom_word_size(self) -> None:
        mock = MockMasterEx()
        mock.preload(0x9000, [0x10, 0x20, 0x30], word_size=8)
        assert mock.read(0x9000, 8) == 0x10
        assert mock.read(0x9008, 8) == 0x20
        assert mock.read(0x9010, 8) == 0x30


# ---- rclr semantics -----------------------------------------------------


class TestRclrSemantics:
    def test_rclr_via_register_info(self) -> None:
        """First read returns the value, second read returns 0."""
        mock = MockMasterEx()
        reg = _Reg(info=_Info(address=0x100, on_read="rclr"))
        mock.on_read(reg, lambda a: 0xA5)
        assert mock.read(0x100, 4) == 0xA5
        assert mock.read(0x100, 4) == 0x00

    def test_rclr_via_explicit_mark(self) -> None:
        """Same semantics with raw addresses + ``mark_rclr``."""
        mock = MockMasterEx()
        mock.on_read(0x200, lambda a: 0xFF)
        mock.mark_rclr(0x200)
        assert mock.read(0x200, 4) == 0xFF
        assert mock.read(0x200, 4) == 0x00

    def test_rclr_without_hook_clears_storage(self) -> None:
        """rclr also applies when the value lives in the in-memory store."""
        mock = MockMasterEx()
        mock.write(0x300, 0xDEAD, 4)
        mock.mark_rclr(0x300)
        assert mock.read(0x300, 4) == 0xDEAD
        assert mock.read(0x300, 4) == 0x00

    def test_non_rclr_register_does_not_clear(self) -> None:
        mock = MockMasterEx()
        reg = _Reg(info=_Info(address=0x400, on_read=None))
        mock.on_read(reg, lambda a: 0x77)
        assert mock.read(0x400, 4) == 0x77
        assert mock.read(0x400, 4) == 0x77  # value persists


# ---- woclr semantics ----------------------------------------------------


class TestWoclrSemantics:
    def test_woclr_via_register_info(self) -> None:
        """Writing 1 to a bit clears it; writing 0 leaves it untouched."""
        mock = MockMasterEx()
        reg = _Reg(info=_Info(address=0x500, on_write="woclr"))
        # Pre-load the latched state via direct memory poke (simulating
        # a hardware-set status bit in production).
        mock.memory[0x500] = 0b1111
        mock.on_write(reg, lambda a, v: None)
        # Clear the low two bits.
        mock.write(0x500, 0b0011, 4)
        assert mock.read(0x500, 4) == 0b1100

    def test_woclr_via_explicit_mark(self) -> None:
        mock = MockMasterEx()
        mock.memory[0x600] = 0xFF
        mock.mark_woclr(0x600)
        mock.write(0x600, 0x0F, 4)  # clears low nibble
        assert mock.read(0x600, 4) == 0xF0

    def test_woclr_does_not_set_zero_bits(self) -> None:
        """A 0 bit in the write must leave the storage bit untouched —
        otherwise it'd be plain overwrite, not write-1-to-clear."""
        mock = MockMasterEx()
        mock.memory[0x700] = 0xAA
        mock.mark_woclr(0x700)
        mock.write(0x700, 0x00, 4)  # no bits set => no clears
        assert mock.read(0x700, 4) == 0xAA

    def test_woclr_hook_sees_raw_written_value(self) -> None:
        """Hooks observe the caller's intent, not the post-woclr storage."""
        mock = MockMasterEx()
        capture: list[int] = []
        reg = _Reg(info=_Info(address=0x800, on_write="woclr"))
        mock.memory[0x800] = 0xFF
        mock.on_write(reg, lambda a, v: capture.append(v))
        mock.write(0x800, 0x0F, 4)
        assert capture == [0x0F]
        assert mock.read(0x800, 4) == 0xF0

    def test_non_woclr_register_overwrites(self) -> None:
        mock = MockMasterEx()
        mock.memory[0x900] = 0xFF
        mock.write(0x900, 0x0F, 4)  # plain overwrite
        assert mock.read(0x900, 4) == 0x0F


# ---- batched paths honor hooks ------------------------------------------


class TestBatchedPaths:
    def test_read_many_dispatches_through_hooks(self) -> None:
        from peakrdl_pybind11.masters import AccessOp

        mock = MockMasterEx()
        mock.on_read(0x1000, lambda a: 0x11)
        mock.on_read(0x1004, lambda a: 0x22)
        mock.write(0x1008, 0x33, 4)
        ops = [
            AccessOp(address=0x1000, width=4),
            AccessOp(address=0x1004, width=4),
            AccessOp(address=0x1008, width=4),
        ]
        assert mock.read_many(ops) == [0x11, 0x22, 0x33]

    def test_write_many_invokes_hooks_and_woclr(self) -> None:
        from peakrdl_pybind11.masters import AccessOp

        mock = MockMasterEx()
        capture: list[tuple[int, int]] = []
        mock.on_write(0x2000, lambda a, v: capture.append((a, v)))
        mock.memory[0x2004] = 0xFF
        mock.mark_woclr(0x2004)
        ops = [
            AccessOp(address=0x2000, value=0xABCD, width=4),
            AccessOp(address=0x2004, value=0x0F, width=4),
        ]
        mock.write_many(ops)
        assert capture == [(0x2000, 0xABCD)]
        assert mock.read(0x2004, 4) == 0xF0


# ---- subclass compatibility ---------------------------------------------


def test_is_a_mockmaster() -> None:
    """``MockMasterEx`` must be drop-in-compatible with code that takes a
    ``MockMaster`` (so test fixtures can swap in extensions transparently)."""
    from peakrdl_pybind11.masters import MockMaster

    mock = MockMasterEx()
    assert isinstance(mock, MockMaster)


def test_reset_preserves_hooks() -> None:
    """A ``reset()`` should clear values but keep the hook config — this
    is the common pattern for re-running a parameterised test."""
    mock = MockMasterEx()
    mock.on_read(0x100, lambda a: 0x42)
    mock.write(0x100, 0xFF, 4)
    mock.reset()
    # Storage cleared, but the hook still wins on read.
    assert mock.read(0x100, 4) == 0x42


def test_resolve_address_rejects_unknown_handle() -> None:
    """Bare objects without ``info.address`` or ``address`` should fail
    loudly — silent zero-coercion would produce extremely confusing test
    failures down the line."""
    mock = MockMasterEx()

    class _Bogus:
        pass

    with pytest.raises(TypeError):
        mock.on_read(_Bogus(), lambda a: 0)
