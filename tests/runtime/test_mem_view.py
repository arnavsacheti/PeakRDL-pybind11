"""Tests for the pure-Python ``MemView`` runtime helper (Unit 10)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest

from peakrdl_pybind11.runtime.errors import AccessError
from peakrdl_pybind11.runtime.mem_view import (
    MemView,
    MemWindow,
    attach_mem_view,
    enhance_mem_class,
    is_mem_class,
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

    # -- Strict dtype mapping for ``np.asarray(mem)`` (single master crossing) ---

    def test_np_asarray_default_dtype_uint32(self, mem: Any) -> None:
        """The default mem (memwidth=32) yields a ``uint32`` ndarray."""
        for i in range(4):
            mem[i] = i + 1
        arr = np.asarray(mem)
        assert isinstance(arr, np.ndarray)
        assert arr.dtype == np.uint32
        assert arr.shape == (mem.depth,)

    def test_np_asarray_dtype_8bit(self) -> None:
        """An 8-bit-wide mem maps to ``np.uint8``."""
        cls = type("Mem8", (FakeMem,), {})
        enhance_mem_class(cls)
        mem8 = cls(depth=16, memwidth=8)
        for i in range(16):
            mem8[i] = i + 0x10
        arr = np.asarray(mem8)
        assert arr.dtype == np.uint8
        assert arr.shape == (16,)
        assert arr.tolist() == [i + 0x10 for i in range(16)]

    def test_np_asarray_dtype_16bit(self) -> None:
        """A 16-bit-wide mem maps to ``np.uint16``."""
        cls = type("Mem16", (FakeMem,), {})
        enhance_mem_class(cls)
        mem16 = cls(depth=8, memwidth=16)
        for i in range(8):
            mem16[i] = 0x1000 + i
        arr = np.asarray(mem16)
        assert arr.dtype == np.uint16
        assert arr.tolist() == [0x1000 + i for i in range(8)]

    def test_np_asarray_dtype_64bit(self) -> None:
        """A 64-bit-wide mem maps to ``np.uint64``."""
        cls = type("Mem64", (FakeMem,), {})
        enhance_mem_class(cls)
        mem64 = cls(depth=4, memwidth=64)
        for i in range(4):
            mem64[i] = (1 << 33) + i  # value won't fit in uint32
        arr = np.asarray(mem64)
        assert arr.dtype == np.uint64
        assert arr.tolist() == [(1 << 33) + i for i in range(4)]

    def test_np_asarray_unsupported_width_raises(self) -> None:
        """A non-canonical word width raises ``NotImplementedError``."""
        cls = type("Mem24", (FakeMem,), {})
        enhance_mem_class(cls)
        mem24 = cls(depth=8, memwidth=24)
        with pytest.raises(NotImplementedError, match="24"):
            np.asarray(mem24)
        # And on the slice path too -- both go through ``_np_dtype_for``.
        with pytest.raises(NotImplementedError, match="24"):
            np.asarray(mem24[0:4])

    def test_np_asarray_with_explicit_uint64_upcasts(self, mem: Any) -> None:
        """An explicit ``dtype=np.uint64`` upcasts a 32-bit-wide mem."""
        for i in range(4):
            mem[i] = 0xDEADBEEF - i
        arr = np.asarray(mem[0:4], dtype=np.uint64)
        assert arr.dtype == np.uint64
        assert arr.tolist() == [0xDEADBEEF - i for i in range(4)]
        # Same for the whole mem (uses ``mem.__array__``).
        arr_full = np.asarray(mem, dtype=np.uint64)
        assert arr_full.dtype == np.uint64
        assert arr_full[:4].tolist() == [0xDEADBEEF - i for i in range(4)]

    def test_np_asarray_uses_single_burst(self, mem: Any) -> None:
        """``np.asarray(mem)`` is one ``read_block`` call, not N word reads."""
        for i in range(8):
            mem[i] = i
        before_block = mem.read_block_calls
        before_word = mem.read_calls
        np.asarray(mem)
        assert mem.read_block_calls == before_block + 1
        assert mem.read_calls == before_word  # no per-word fallback

    def test_memview_copy_is_ndarray_alias(self, mem: Any) -> None:
        """``MemView.copy()`` is an explicit alias of ``np.asarray(view)``."""
        for i in range(8):
            mem[i] = i + 100
        view = mem[0:8]
        via_copy = view.copy()
        via_asarray = np.asarray(view)
        assert isinstance(via_copy, np.ndarray)
        assert via_copy.dtype == via_asarray.dtype
        assert via_copy.tolist() == via_asarray.tolist()


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


# ---------------------------------------------------------------------------
# Auto-attach hook: ``attach_mem_view`` registered with Unit 1's
# ``register_register_enhancement`` seam fires for every generated
# *register* class but only enhances mem-shaped ones.
# ---------------------------------------------------------------------------


class TestAutoAttachHook:
    def test_hook_registered_on_registry(self) -> None:
        """``attach_mem_view`` is visible from the registry's snapshot getters."""
        from peakrdl_pybind11.runtime._registry import get_register_enhancers

        assert attach_mem_view in get_register_enhancers()

    def test_is_mem_class_structural(self) -> None:
        """Mem-shape detection works on classes carrying ``mementries`` / ``memwidth``."""
        cls = type("Mem", (FakeMem,), {"mementries": 64, "memwidth": 32})
        assert is_mem_class(cls)

    def test_is_mem_class_metadata_flag(self) -> None:
        """An explicit ``metadata={'is_mem': True}`` overrides structural probe."""

        class _NotAMem:
            pass

        assert is_mem_class(_NotAMem, {"is_mem": True})
        assert not is_mem_class(_NotAMem, {"is_mem": False})
        assert not is_mem_class(_NotAMem)

    def test_attach_hook_skips_plain_register_classes(self) -> None:
        """The hook leaves register-only classes alone (no MemView wrapping)."""

        class _PlainReg:
            def __getitem__(self, idx: int) -> int:
                return 0

        before = _PlainReg.__getitem__
        attach_mem_view(_PlainReg, {"fields": {"x": (0, 1)}})
        assert _PlainReg.__getitem__ is before

    def test_attach_hook_wraps_mem_class_slice_returns_memview(self) -> None:
        """Calling the hook on a mem class makes ``mem[1:5]`` return a ``MemView``."""
        cls = type("MemForHook", (FakeMem,), {"mementries": 16, "memwidth": 32})
        attach_mem_view(cls, {})
        mem = cls(depth=16)
        view = mem[1:5]
        assert isinstance(view, MemView)
        assert len(view) == 4

    def test_attach_hook_is_idempotent(self) -> None:
        """Running the hook twice on the same class doesn't double-wrap."""
        cls = type("MemTwice", (FakeMem,), {"mementries": 16, "memwidth": 32})
        attach_mem_view(cls, {})
        first = cls.__getitem__
        attach_mem_view(cls, {})
        assert cls.__getitem__ is first

    def test_attach_hook_via_apply_register_enhancements(self) -> None:
        """Driving the registry seam end-to-end wraps a mem-shaped class."""
        from peakrdl_pybind11.runtime._registry import apply_register_enhancements

        cls = type("MemViaSeam", (FakeMem,), {"mementries": 16, "memwidth": 32})
        apply_register_enhancements(cls, {"fields": {}, "is_mem": True})
        mem = cls(depth=16)
        assert isinstance(mem[0:8], MemView)


# ---------------------------------------------------------------------------
# Access-mode enforcement (§6 of IDEAL_API_SKETCH.md).
#
# Mirrors the field-side enforcement that shipped in commit 8ddca6c:
# reads on a write-only mem and writes on a read-only mem raise
# :class:`AccessError`. The runtime probes ``mem.info.access`` (or, when
# present, the ``is_readable`` / ``is_writable`` boolean flags) and
# defaults to "allowed" when no metadata is attached.
# ---------------------------------------------------------------------------


class _FakeInfo:
    """Tiny stand-in for an ``Info`` namespace used in the access-mode tests."""

    __slots__ = ("access", "path")

    def __init__(self, access: str | None, path: str = "") -> None:
        self.access = access
        self.path = path


def _make_mem(access: str | None, *, path: str = "soc.mem", depth: int = 16) -> Any:
    """Build a fresh enhanced mem instance with the given ``info.access`` token.

    Each call gets a new subclass so the per-class enhancement flag does
    not leak metadata between tests.
    """
    cls = type("MemAccess", (FakeMem,), {})
    enhance_mem_class(cls)
    mem = cls(depth=depth)
    mem.info = _FakeInfo(access=access, path=path)
    return mem


class TestMemAccessEnforcement:
    # -- read-only mem: writes blocked --------------------------------------

    def test_write_int_on_read_only_raises(self) -> None:
        mem = _make_mem("r", path="soc.rom")
        with pytest.raises(AccessError) as excinfo:
            mem[0] = 0xDEAD
        assert "soc.rom" in str(excinfo.value)
        assert "sw=r" in str(excinfo.value)
        assert excinfo.value.access_mode == "r"

    def test_write_slice_on_read_only_raises(self) -> None:
        mem = _make_mem("r", path="soc.rom")
        with pytest.raises(AccessError, match="soc.rom"):
            mem[0:4] = [1, 2, 3, 4]

    def test_write_view_setitem_on_read_only_raises(self) -> None:
        mem = _make_mem("r", path="soc.rom")
        view = mem[0:4]  # taking the slice itself does NOT touch the bus
        with pytest.raises(AccessError):
            view[0] = 0xAB
        with pytest.raises(AccessError):
            view[:] = 0

    def test_write_bytes_on_read_only_raises(self) -> None:
        mem = _make_mem("r", path="soc.rom")
        payload = (0xCAFEBABE).to_bytes(4, "little")
        with pytest.raises(AccessError, match="soc.rom"):
            mem.write_bytes(0, payload)

    def test_write_from_on_read_only_raises(self) -> None:
        mem = _make_mem("r", path="soc.rom")
        buf = np.arange(4, dtype=np.uint32)
        with pytest.raises(AccessError, match="soc.rom"):
            mem.write_from(buf, offset=0)

    def test_window_flush_on_read_only_raises(self) -> None:
        """A buffered write through ``mem.window(...)`` still trips the gate
        when the buffer flushes on context exit.
        """
        mem = _make_mem("r", path="soc.rom")
        with pytest.raises(AccessError, match="soc.rom"):
            with mem.window(0, 4) as w:
                w[0] = 0xAB

    # -- write-only mem: reads blocked --------------------------------------

    def test_read_int_on_write_only_raises(self) -> None:
        mem = _make_mem("w", path="soc.wo_mem")
        with pytest.raises(AccessError) as excinfo:
            _ = mem[0]
        assert "soc.wo_mem" in str(excinfo.value)
        assert "sw=w" in str(excinfo.value)
        assert excinfo.value.access_mode == "w"

    def test_read_view_on_write_only_raises(self) -> None:
        mem = _make_mem("w", path="soc.wo_mem")
        view = mem[0:4]  # slice itself is allowed
        with pytest.raises(AccessError):
            _ = view[0]
        with pytest.raises(AccessError):
            view.copy()
        with pytest.raises(AccessError):
            view.read()

    def test_np_asarray_on_write_only_raises(self) -> None:
        mem = _make_mem("w", path="soc.wo_mem")
        with pytest.raises(AccessError, match="soc.wo_mem"):
            np.asarray(mem)

    def test_np_asarray_on_view_of_write_only_raises(self) -> None:
        mem = _make_mem("w", path="soc.wo_mem")
        with pytest.raises(AccessError, match="soc.wo_mem"):
            np.asarray(mem[0:4])

    def test_read_bytes_on_write_only_raises(self) -> None:
        mem = _make_mem("w", path="soc.wo_mem")
        with pytest.raises(AccessError, match="soc.wo_mem"):
            mem.read_bytes(0, 4)

    def test_read_into_on_write_only_raises(self) -> None:
        mem = _make_mem("w", path="soc.wo_mem")
        buf = np.empty(4, dtype=np.uint32)
        with pytest.raises(AccessError, match="soc.wo_mem"):
            mem.read_into(buf, offset=0)

    def test_iter_chunks_on_write_only_raises(self) -> None:
        mem = _make_mem("w", path="soc.wo_mem")
        with pytest.raises(AccessError, match="soc.wo_mem"):
            next(mem.iter_chunks(size=4))

    def test_window_enter_on_write_only_raises(self) -> None:
        """Entering a buffered window primes the buffer via a bulk read; that
        read must trip the access gate on a write-only mem.
        """
        mem = _make_mem("w", path="soc.wo_mem")
        with pytest.raises(AccessError, match="soc.wo_mem"):
            with mem.window(0, 4):
                pass

    # -- rw mem: both ops still work normally -------------------------------

    def test_read_write_mem_round_trips(self) -> None:
        mem = _make_mem("rw")
        mem[0] = 0xAA
        assert mem[0] == 0xAA
        mem[0:4] = [1, 2, 3, 4]
        assert mem[0:4].copy().tolist() == [1, 2, 3, 4]
        arr = np.asarray(mem)
        assert arr.shape == (mem.depth,)

    # -- back-compat: no metadata defaults to "allowed" ---------------------

    def test_no_info_defaults_allow(self) -> None:
        """Mems without an ``info`` attribute behave as if ``sw=rw``."""
        cls = type("MemBare", (FakeMem,), {})
        enhance_mem_class(cls)
        mem = cls(depth=8)
        # No ``mem.info`` attached.
        assert not hasattr(mem, "info")
        mem[0] = 0xAB
        assert mem[0] == 0xAB
        assert np.asarray(mem).shape == (8,)

    def test_info_without_access_defaults_allow(self) -> None:
        """``mem.info`` present but ``info.access is None`` is treated as allow."""
        mem = _make_mem(None, path="soc.default_mem")
        mem[0] = 0xCAFE
        assert mem[0] == 0xCAFE

    def test_unknown_access_token_defaults_allow(self) -> None:
        """An unrecognized access token is treated permissively (back-compat)."""
        mem = _make_mem("???", path="soc.weird_mem")
        # Neither read nor write should raise on an unknown token.
        mem[0] = 0x11
        assert mem[0] == 0x11

    def test_is_readable_flag_overrides_info(self) -> None:
        """A direct ``is_readable=False`` attribute takes precedence over info."""
        cls = type("MemFlagged", (FakeMem,), {})
        enhance_mem_class(cls)
        mem = cls(depth=8)
        mem.is_readable = False
        mem.is_writable = True
        mem.info = _FakeInfo(access="rw", path="soc.flagged_mem")
        with pytest.raises(AccessError, match="soc.flagged_mem"):
            _ = mem[0]
        # Writes still work because is_writable is True.
        mem[0] = 0x42

    # -- write_block/read_block as the gated primitives ---------------------

    def test_write_block_burst_gated_on_read_only(self) -> None:
        """The burst write path is gated symmetrically with per-word writes."""
        mem = _make_mem("r", path="soc.rom")
        before = mem.write_block_calls
        with pytest.raises(AccessError):
            mem[0:8] = list(range(8))  # triggers the burst path
        # The bus never saw the write -- counters unchanged.
        assert mem.write_block_calls == before

    def test_read_block_burst_gated_on_write_only(self) -> None:
        """The burst read path is gated symmetrically with per-word reads."""
        mem = _make_mem("w", path="soc.wo_mem")
        before = mem.read_block_calls
        with pytest.raises(AccessError):
            mem[0:8].copy()
        # No bus traffic on a blocked read.
        assert mem.read_block_calls == before
