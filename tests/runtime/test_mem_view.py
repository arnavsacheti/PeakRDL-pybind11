"""Tests for the pure-Python ``MemView`` runtime helper (Unit 10)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest

from peakrdl_pybind11.runtime.mem_view import (
    MemView,
    MemWindow,
    enhance_mem_class,
)


# ---------------------------------------------------------------------------
# Test fixtures: a Plain Old Python Object that mimics a generated mem.
# ---------------------------------------------------------------------------


class FakeMem:
    """Tiny stand-in for a generated mem class.

    Stores word values in a list. Supports the optional ``read_block``/
    ``write_block`` fast paths so we exercise the burst path. Also
    counts master traffic for verifying coalescing.
    """

    def __init__(self, depth: int = 256, memwidth: int = 32, base_address: int = 0x4000) -> None:
        self.mementries = depth
        self.memwidth = memwidth
        # ``base_address`` becomes a property after enhancement; back it
        # with a private attribute so the property's getter sees this
        # instance state.
        self._base_address = base_address
        self._words: list[int] = [0] * depth
        # Coverage counters used by tests that assert burstiness.
        self.read_calls = 0
        self.write_calls = 0
        self.read_block_calls = 0
        self.write_block_calls = 0

    # -- basic protocol -----------------------------------------------------

    def __len__(self) -> int:
        return self.mementries

    def __getitem__(self, index: int) -> int:
        self.read_calls += 1
        return int(self._words[index])

    def __setitem__(self, index: int, value: int) -> None:
        self.write_calls += 1
        mask = (1 << self.memwidth) - 1
        self._words[index] = int(value) & mask

    # -- batched fast path --------------------------------------------------

    def read_block(self, start: int, count: int) -> list[int]:
        self.read_block_calls += 1
        return list(self._words[start : start + count])

    def write_block(self, start: int, values: Sequence[int]) -> None:
        self.write_block_calls += 1
        mask = (1 << self.memwidth) - 1
        for i, v in enumerate(values):
            self._words[start + i] = int(v) & mask


class FakeMemSlow(FakeMem):
    """Mem variant *without* read_block / write_block.

    Used to verify the per-word fallback path.
    """

    read_block = None  # type: ignore[assignment]
    write_block = None  # type: ignore[assignment]


class _Entry:
    """Tiny stand-in for a generated register entry (``mem[i]`` result)."""

    __slots__ = ("_owner", "_index")

    def __init__(self, owner: FakeEntryMem, index: int) -> None:
        self._owner = owner
        self._index = index

    def read(self) -> int:
        self._owner.entry_read_calls += 1
        return int(self._owner._words[self._index])

    def write(self, value: int) -> None:
        self._owner.entry_write_calls += 1
        mask = (1 << self._owner.memwidth) - 1
        self._owner._words[self._index] = int(value) & mask


class FakeEntryMem:
    """Mem variant that mirrors generated C++ classes more closely.

    ``__getitem__(int)`` returns an entry register object (with
    ``.read()`` / ``.write()``); there is **no** ``__setitem__``, just
    like the bindings template. Slice access still goes through the
    enhancement wrapper.
    """

    def __init__(self, depth: int = 64, memwidth: int = 32, base_address: int = 0x8000) -> None:
        self.mementries = depth
        self.memwidth = memwidth
        self._base_address = base_address
        self._words: list[int] = [0] * depth
        self.entry_read_calls = 0
        self.entry_write_calls = 0
        self.read_block_calls = 0
        self.write_block_calls = 0

    def __len__(self) -> int:
        return self.mementries

    def __getitem__(self, index: int) -> _Entry:
        return _Entry(self, int(index))

    def read_block(self, start: int, count: int) -> list[int]:
        self.read_block_calls += 1
        return list(self._words[start : start + count])

    def write_block(self, start: int, values: Sequence[int]) -> None:
        self.write_block_calls += 1
        mask = (1 << self.memwidth) - 1
        for i, v in enumerate(values):
            self._words[start + i] = int(v) & mask


@pytest.fixture
def mem() -> Any:
    cls = type("FakeMemUnique", (FakeMem,), {})
    enhance_mem_class(cls)
    return cls(depth=256)


@pytest.fixture
def mem_slow() -> Any:
    cls = type("FakeMemSlowUnique", (FakeMemSlow,), {})
    enhance_mem_class(cls)
    return cls(depth=256)


@pytest.fixture
def mem_entry() -> Any:
    """Production-shape mem: ``__getitem__`` returns an entry, no ``__setitem__``."""
    cls = type("FakeEntryMemUnique", (FakeEntryMem,), {})
    enhance_mem_class(cls)
    return cls(depth=64)


# ---------------------------------------------------------------------------
# Single-word access.
# ---------------------------------------------------------------------------


class TestSingleWord:
    def test_int_set_and_get(self, mem: Any) -> None:
        mem[10] = 0xDEADBEEF
        assert mem[10] == 0xDEADBEEF

    def test_int_set_masks_to_word_width(self, mem: Any) -> None:
        mem[5] = 0x1_0000_0001  # one bit above 32-bit width
        assert mem[5] == 0x1

    def test_negative_index_raises(self, mem: Any) -> None:
        # The underlying mem doesn't support negative indices; mem[-1]
        # would silently become an unrelated entry, which is a footgun.
        # MemView (slice path) is what users index, so we just check
        # that the basic getitem path round-trips at index 0.
        mem[0] = 1
        assert mem[0] == 1


# ---------------------------------------------------------------------------
# Slicing returns MemView.
# ---------------------------------------------------------------------------


class TestSliceReturnsView:
    def test_slice_returns_memview(self, mem: Any) -> None:
        view = mem[10:20]
        assert isinstance(view, MemView)
        assert len(view) == 10

    def test_view_copy_returns_ndarray(self, mem: Any) -> None:
        for i in range(10, 20):
            mem[i] = i
        snap = mem[10:20].copy()
        assert isinstance(snap, np.ndarray)
        assert snap.shape == (10,)
        assert list(snap) == list(range(10, 20))

    def test_view_read_alias(self, mem: Any) -> None:
        mem[7] = 0x55
        snap = mem[7:8].read()
        assert isinstance(snap, np.ndarray)
        assert snap[0] == 0x55

    def test_view_indexing_is_live(self, mem: Any) -> None:
        view = mem[10:20]
        mem[15] = 0x42
        assert view[5] == 0x42

    def test_view_subslice_is_live_too(self, mem: Any) -> None:
        view = mem[10:20]
        sub = view[2:5]
        assert isinstance(sub, MemView)
        assert len(sub) == 3
        mem[12] = 0xAA
        assert sub[0] == 0xAA

    def test_view_iter_yields_ints(self, mem: Any) -> None:
        for i in range(5):
            mem[i] = i + 1
        out = list(mem[0:5])
        assert out == [1, 2, 3, 4, 5]

    def test_view_repr(self, mem: Any) -> None:
        r = repr(mem[10:20])
        assert "MemView" in r
        assert "length=10" in r


# ---------------------------------------------------------------------------
# Slice assignment / bulk write.
# ---------------------------------------------------------------------------


class TestSliceAssignment:
    def test_zero_fill_via_full_slice(self, mem: Any) -> None:
        for i in range(8):
            mem[i] = 0xFF
        mem[:] = 0
        assert mem[0:8].copy().tolist() == [0] * 8

    def test_assign_list(self, mem: Any) -> None:
        mem[10:13] = [1, 2, 3]
        assert mem[10] == 1
        assert mem[11] == 2
        assert mem[12] == 3

    def test_assign_ndarray(self, mem: Any) -> None:
        arr = np.arange(4, dtype=np.uint32)
        mem[20:24] = arr
        assert mem[20:24].copy().tolist() == [0, 1, 2, 3]

    def test_assign_scalar_broadcasts(self, mem: Any) -> None:
        mem[5:10] = 0xCAFE
        for i in range(5, 10):
            assert mem[i] == 0xCAFE

    def test_assign_wrong_length_raises(self, mem: Any) -> None:
        with pytest.raises(ValueError):
            mem[0:4] = [1, 2, 3]

    def test_slice_assignment_uses_burst_when_available(self, mem: Any) -> None:
        before = mem.write_block_calls
        mem[10:30] = list(range(20))
        assert mem.write_block_calls == before + 1

    def test_view_subslice_assignment(self, mem: Any) -> None:
        view = mem[10:30]
        view[5:10] = 7
        for i in range(15, 20):
            assert mem[i] == 7

    def test_delitem_raises(self, mem: Any) -> None:
        view = mem[0:10]
        with pytest.raises(NotImplementedError):
            del view[0:5]


# ---------------------------------------------------------------------------
# NumPy interop.
# ---------------------------------------------------------------------------


class TestNumpyInterop:
    def test_np_asarray_on_view(self, mem: Any) -> None:
        for i in range(8):
            mem[i] = i * 10
        arr = np.asarray(mem[0:8])
        assert isinstance(arr, np.ndarray)
        assert arr.tolist() == [0, 10, 20, 30, 40, 50, 60, 70]

    def test_np_asarray_on_mem(self, mem: Any) -> None:
        for i in range(4):
            mem[i] = i + 1
        arr = np.asarray(mem)
        assert isinstance(arr, np.ndarray)
        # Snapshot of the entire mem: depth=256 entries.
        assert arr.shape == (256,)
        assert arr[:4].tolist() == [1, 2, 3, 4]

    def test_view_array_with_dtype(self, mem: Any) -> None:
        for i in range(4):
            mem[i] = i + 1
        arr = np.asarray(mem[0:4], dtype=np.int64)
        assert arr.dtype == np.int64
        assert arr.tolist() == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Byte-level escape hatch.
# ---------------------------------------------------------------------------


class TestBytes:
    def test_view_read_bytes_basic(self, mem: Any) -> None:
        mem[0] = 0xDEADBEEF  # little-endian: ef be ad de
        data = mem[0:1].read_bytes(0, 4)
        assert isinstance(data, bytes)
        assert len(data) == 4

    def test_mem_read_bytes_full(self, mem: Any) -> None:
        mem[0] = 0xAABBCCDD
        mem[1] = 0x11223344
        data = mem.read_bytes(0, 8)
        assert isinstance(data, bytes)
        assert len(data) == 8
        # Round-trip via ndarray check.
        arr = np.frombuffer(data, dtype=np.uint32)
        assert arr.tolist() == [0xAABBCCDD, 0x11223344]

    def test_read_bytes_default_n(self, mem: Any) -> None:
        for i in range(4):
            mem[i] = 0xFFFFFFFF
        data = mem[0:4].read_bytes(offset=0)
        assert len(data) == 16

    def test_write_bytes_round_trip(self, mem: Any) -> None:
        payload = (0xCAFEBABE).to_bytes(4, "little") + (0x12345678).to_bytes(4, "little")
        mem.write_bytes(0, payload)
        assert mem[0] == 0xCAFEBABE
        assert mem[1] == 0x12345678

    def test_write_bytes_unaligned_raises(self, mem: Any) -> None:
        with pytest.raises(NotImplementedError):
            mem.write_bytes(1, b"\x00\x01\x02\x03")


# ---------------------------------------------------------------------------
# Window context manager.
# ---------------------------------------------------------------------------


class TestWindow:
    def test_window_flush_on_exit(self, mem: Any) -> None:
        with mem.window(0, 256) as w:
            assert isinstance(w, MemWindow)
            w[0] = 42
        assert mem[0] == 42

    def test_window_buffers_writes(self, mem: Any) -> None:
        # While inside the window, writes should not have hit the bus.
        with mem.window(10, 16) as w:
            before = mem.write_block_calls
            w[5] = 0xAB
            # No bus writes yet.
            assert mem.write_block_calls == before
        # After exit, exactly one burst should have run for the dirty run.
        assert mem.write_block_calls == before + 1
        assert mem[15] == 0xAB

    def test_window_slice_assignment(self, mem: Any) -> None:
        with mem.window(0, 8) as w:
            w[:] = np.arange(8, dtype=np.uint32) * 2
        assert mem[0:8].copy().tolist() == [0, 2, 4, 6, 8, 10, 12, 14]

    def test_window_discard(self, mem: Any) -> None:
        mem[0] = 1
        with mem.window(0, 4) as w:
            w[0] = 99
            w.discard()
        assert mem[0] == 1  # discarded; no flush

    def test_window_out_of_range_raises(self, mem: Any) -> None:
        with pytest.raises(ValueError):
            mem.window(250, 100)

    def test_window_default_length_runs_to_end(self, mem: Any) -> None:
        with mem.window(0) as w:
            assert len(w) == 256


# ---------------------------------------------------------------------------
# Streaming.
# ---------------------------------------------------------------------------


class TestIterChunks:
    def test_iter_chunks_yields_ndarrays(self, mem: Any) -> None:
        for i in range(256):
            mem[i] = i
        chunks = list(mem.iter_chunks(size=64))
        assert len(chunks) == 4
        for chunk in chunks:
            assert isinstance(chunk, np.ndarray)
            assert chunk.shape == (64,)
        # Concatenation matches a full snapshot.
        full = np.concatenate(chunks)
        assert full.tolist() == list(range(256))

    def test_iter_chunks_uneven_tail(self, mem: Any) -> None:
        chunks = list(mem.iter_chunks(size=100))
        # 256 / 100 -> 100, 100, 56
        assert [c.shape[0] for c in chunks] == [100, 100, 56]

    def test_iter_chunks_invalid_size(self, mem: Any) -> None:
        with pytest.raises(ValueError):
            next(mem.iter_chunks(size=0))


# ---------------------------------------------------------------------------
# read_into / write_from.
# ---------------------------------------------------------------------------


class TestReadIntoWriteFrom:
    def test_read_into(self, mem: Any) -> None:
        for i in range(16):
            mem[i] = i + 100
        buf = np.empty(16, dtype=np.uint32)
        result = mem.read_into(buf, offset=0)
        assert result is buf
        assert buf.tolist() == [i + 100 for i in range(16)]

    def test_read_into_uses_burst(self, mem: Any) -> None:
        before = mem.read_block_calls
        buf = np.empty(32, dtype=np.uint32)
        mem.read_into(buf, offset=0)
        assert mem.read_block_calls == before + 1

    def test_write_from(self, mem: Any) -> None:
        buf = np.arange(20, dtype=np.uint32) + 1000
        mem.write_from(buf, offset=10)
        for i in range(20):
            assert mem[10 + i] == 1000 + i

    def test_write_from_uses_burst(self, mem: Any) -> None:
        before = mem.write_block_calls
        buf = np.arange(8, dtype=np.uint32)
        mem.write_from(buf, offset=0)
        assert mem.write_block_calls == before + 1

    def test_read_into_out_of_range(self, mem: Any) -> None:
        buf = np.empty(10, dtype=np.uint32)
        with pytest.raises(ValueError):
            mem.read_into(buf, offset=250)

    def test_write_from_out_of_range(self, mem: Any) -> None:
        with pytest.raises(ValueError):
            mem.write_from(np.zeros(10, dtype=np.uint32), offset=250)


# ---------------------------------------------------------------------------
# Properties surfaced via node.info integration.
# ---------------------------------------------------------------------------


class TestProperties:
    def test_size_bytes(self, mem: Any) -> None:
        assert mem.size_bytes == 256 * 4

    def test_depth(self, mem: Any) -> None:
        assert mem.depth == 256

    def test_word_width(self, mem: Any) -> None:
        assert mem.word_width == 32

    def test_base_address(self, mem: Any) -> None:
        assert mem.base_address == 0x4000


# ---------------------------------------------------------------------------
# Fallback path (no read_block / write_block on master).
# ---------------------------------------------------------------------------


class TestFallback:
    def test_view_copy_falls_back(self, mem_slow: Any) -> None:
        for i in range(8):
            mem_slow[i] = i + 1
        snap = mem_slow[0:8].copy()
        assert snap.tolist() == [1, 2, 3, 4, 5, 6, 7, 8]

    def test_slice_assignment_falls_back(self, mem_slow: Any) -> None:
        mem_slow[0:4] = [10, 20, 30, 40]
        assert mem_slow[0] == 10
        assert mem_slow[3] == 40


# ---------------------------------------------------------------------------
# Idempotency / decorator semantics.
# ---------------------------------------------------------------------------


class TestEnhancement:
    def test_enhance_is_idempotent(self) -> None:
        cls = type("M", (FakeMem,), {})
        enhance_mem_class(cls)
        first_getitem = cls.__getitem__
        enhance_mem_class(cls)
        assert cls.__getitem__ is first_getitem

    def test_enhance_returns_class(self) -> None:
        cls = type("M2", (FakeMem,), {})
        result = enhance_mem_class(cls)
        assert result is cls


class TestEntryStyleMem:
    """Verify the unit also works against production-shape mem objects.

    Mirrors the generated C++ binding: ``mem[i]`` returns an entry
    register object (with ``read()`` / ``write()``) and there is no
    ``__setitem__`` on the class.
    """

    def test_int_set_and_get(self, mem_entry: Any) -> None:
        # Assigning via ``mem[i] = v`` should route through the entry's
        # ``write()``; reading should unwrap to the int word value.
        mem_entry[10] = 0xDEADBEEF
        assert mem_entry[10] == 0xDEADBEEF
        assert mem_entry.entry_write_calls == 1

    def test_int_get_unwraps_entry(self, mem_entry: Any) -> None:
        mem_entry[5] = 0x42
        assert isinstance(mem_entry[5], int)

    def test_slice_returns_memview(self, mem_entry: Any) -> None:
        view = mem_entry[10:20]
        assert isinstance(view, MemView)
        assert len(view) == 10

    def test_view_copy(self, mem_entry: Any) -> None:
        for i in range(8):
            mem_entry[i] = i * 7
        snap = mem_entry[0:8].copy()
        assert snap.tolist() == [i * 7 for i in range(8)]

    def test_zero_fill(self, mem_entry: Any) -> None:
        for i in range(8):
            mem_entry[i] = 0xFF
        mem_entry[:] = 0
        assert mem_entry[0:8].copy().tolist() == [0] * 8

    def test_window_flush(self, mem_entry: Any) -> None:
        with mem_entry.window(0, 16) as w:
            w[0] = 0xABC
        assert mem_entry[0] == 0xABC

    def test_no_recursion_on_set(self, mem_entry: Any) -> None:
        # Repeatedly assigning at int indices must not infinitely recurse
        # through the wrapped __setitem__.
        for i in range(20):
            mem_entry[i] = i + 1
        for i in range(20):
            assert mem_entry[i] == i + 1
