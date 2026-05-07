"""Live, NumPy-aware view onto a memory region (sketch §6).

This module provides :class:`MemView` -- a sliceable handle on a
contiguous range of words in a SystemRDL ``mem`` region -- plus the
``mem.window`` / ``mem.iter_chunks`` / ``mem.read_into`` /
``mem.write_from`` helpers that decorate the generated mem class.

Semantics
---------
* A ``MemView`` is **live**: every ``view[i]`` access hits the bus.
  It is *not* a buffer copy. Use :meth:`MemView.copy` (alias
  :meth:`MemView.read`) to take a one-burst snapshot ``ndarray``.
* Slicing a ``MemView`` returns another ``MemView`` (also live), so
  ``mem[10:20][2]`` is one single-word read at index ``12``.
* Bulk writes (``view[:] = arr``, ``view[a:b] = arr``) are coalesced
  into a single ``write_block`` / ``write_many`` call when the
  underlying mem object exposes one; otherwise they fall back to a
  per-word loop.
* :meth:`MemView.__array__` materializes a snapshot at conversion
  time, which is what ``np.asarray(mem)`` calls. Treat it as a copy --
  later writes to the array do not propagate back to the bus.

Performance
-----------
The implementation is **pure Python**. A C++ buffer-protocol fast
path (``mem.read_into(buf)`` zero-copy filling a pre-allocated NumPy
array via ``Py_buffer``) is deferred. ``mem.read_into`` /
``mem.write_from`` already use the master's batched
``read_block`` / ``write_block`` (which themselves call
``read_many`` / ``write_many`` on the master) when available, so
bursts go through a single transport round-trip; the Python overhead
is one allocation and an ``np.asarray`` round-trip.

Wiring
------
:func:`enhance_mem_class` decorates a generated mem class in-place with
the new methods (``window``, ``iter_chunks``, ``read_into``,
``write_from``, ``size_bytes``, ``depth``, ``word_width``,
``base_address``, ``__array__``). It also rewrites ``__getitem__`` /
``__setitem__`` so that slice access returns ``MemView`` instances
instead of lists. :func:`enhance_mem_instance` is the per-object
fallback for cases where a class cannot be patched.

If Unit 1's ``register_node_attribute`` registry is importable, this
module also registers itself there so the exporter pipeline can apply
the enhancement automatically. The wiring is best-effort and is
silently skipped if the registry seam is not present.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray


__all__ = [
    "MemLike",
    "MemView",
    "MemWindow",
    "enhance_mem_class",
    "enhance_mem_instance",
]


# ---------------------------------------------------------------------------
# Protocol describing what we need from the underlying mem object.
# ---------------------------------------------------------------------------


@runtime_checkable
class MemLike(Protocol):
    """Minimal interface a memory object must satisfy for :class:`MemView`.

    A generated ``mem`` class from the pybind11 exporter satisfies this
    naturally; tests can plug in any duck-typed equivalent.
    """

    def __len__(self) -> int: ...

    def __getitem__(self, index: Any) -> Any: ...

    def __setitem__(self, index: Any, value: Any) -> None: ...


# ---------------------------------------------------------------------------
# Helpers that work on any MemLike, falling back gracefully.
# ---------------------------------------------------------------------------


def _word_width_bits(mem: Any) -> int:
    """Return the memory word width in bits (default 32)."""
    for attr in ("memwidth", "word_width", "_memwidth"):
        v = getattr(mem, attr, None)
        if isinstance(v, int) and v > 0:
            return v
    return 32


def _word_width_bytes(mem: Any) -> int:
    bits = _word_width_bits(mem)
    return max(1, (bits + 7) // 8)


def _depth(mem: Any) -> int:
    """Number of word entries in the memory."""
    for attr in ("mementries", "depth", "num_entries"):
        v = getattr(mem, attr, None)
        if isinstance(v, int) and v > 0:
            return v
    return len(mem)


def _base_address(mem: Any) -> int:
    """Best-effort base address (zero if unknown)."""
    for attr in ("_base_address", "base_address", "address", "offset"):
        # Use ``__dict__`` lookup first to avoid recursing into a
        # ``base_address`` property that calls back into this helper.
        d = getattr(mem, "__dict__", None)
        if isinstance(d, dict) and attr in d:
            v = d[attr]
        else:
            v = getattr(mem, attr, None)
        if isinstance(v, int):
            return v
    return 0


def _np_dtype_for(mem: Any) -> np.dtype:
    """Pick an unsigned NumPy dtype that fits one word."""
    bits = _word_width_bits(mem)
    if bits <= 8:
        return np.dtype(np.uint8)
    if bits <= 16:
        return np.dtype(np.uint16)
    if bits <= 32:
        return np.dtype(np.uint32)
    return np.dtype(np.uint64)


def _read_block(mem: Any, start: int, count: int) -> list[int]:
    """Bulk-read ``count`` words starting at ``start``.

    Prefers ``mem.read_block`` (one master round-trip), then any
    available ``read_many`` on the attached master, and finally falls
    back to a per-word ``mem[i]`` loop.
    """
    if count <= 0:
        return []
    read_block = getattr(mem, "read_block", None)
    if callable(read_block):
        return [int(v) for v in read_block(start, count)]
    return [int(_get_word(mem, start + i)) for i in range(count)]


def _write_block(mem: Any, start: int, values: Sequence[int]) -> None:
    """Bulk-write ``values`` to consecutive words starting at ``start``.

    ``values`` must already be a Python ``list[int]`` -- the generated
    C++ ``write_block`` expects ``std::vector<uint64_t>`` and pybind11
    is strict about the element type.
    """
    if len(values) == 0:
        return
    write_block = getattr(mem, "write_block", None)
    if callable(write_block):
        write_block(start, values)
        return
    for i, v in enumerate(values):
        _set_word(mem, start + i, v)


def _unwrap_word(entry: Any) -> int:
    """Coerce a mem ``__getitem__`` result to an int word value.

    Generated C++ mem classes return an entry-register object whose
    ``read()`` produces the word; pure-Python test fixtures may return
    the int directly. Either is supported.
    """
    if isinstance(entry, (int, np.integer)):
        return int(entry)
    read = getattr(entry, "read", None)
    if callable(read):
        return int(read())
    return int(entry)


def _get_word(mem: Any, index: int) -> int:
    """Read a single word from ``mem[index]`` and unwrap to int."""
    return _unwrap_word(mem[index])


def _raw_get(mem: Any, index: int) -> Any:
    """Bypass any class-level ``__getitem__`` wrapping so we can reach the
    underlying entry object even on enhanced classes.
    """
    cls = type(mem)
    enhanced_getitem = cls.__dict__.get("__getitem__")
    raw = getattr(enhanced_getitem, "__peakrdl_orig_getitem__", None)
    if raw is not None:
        return raw(mem, index)
    return mem[index]


def _set_word(mem: Any, index: int, value: int) -> None:
    """Write a single word, dispatching to the original ``__setitem__``
    when one existed on the class, else to ``entry.write()``.

    Must not call ``mem[index] = ...`` directly on an *enhanced* class:
    that would re-enter the wrapper and recurse infinitely.
    """
    cls = type(mem)
    cls_setitem = cls.__dict__.get("__setitem__")
    if cls_setitem is not None and hasattr(cls_setitem, "__peakrdl_orig_setitem__"):
        # Enhanced class: pull the original off the tag (may be ``None``
        # if the class never defined one to begin with).
        orig_setitem = cls_setitem.__peakrdl_orig_setitem__
        if orig_setitem is not None:
            orig_setitem(mem, index, int(value))
            return
    elif cls_setitem is not None:
        # Plain (non-enhanced) class: just use it.
        cls_setitem(mem, index, int(value))
        return
    # No usable __setitem__ -- fall back to entry.write().
    entry = _raw_get(mem, index)
    write = getattr(entry, "write", None)
    if callable(write):
        write(int(value))
        return
    raise TypeError(
        f"mem object {type(mem).__name__!r} does not support int assignment "
        f"or entry.write(); cannot write to index {index}"
    )


def _normalize_slice(s: slice, length: int) -> tuple[int, int, int]:
    """Resolve a slice against ``length``; reject non-unit strides."""
    start, stop, step = s.indices(length)
    if step != 1:
        raise ValueError("MemView does not support non-unit slice strides")
    if stop < start:
        stop = start
    return start, stop, step


# ---------------------------------------------------------------------------
# MemView -- live slice handle.
# ---------------------------------------------------------------------------


class MemView:
    """Live, sliceable view onto a contiguous range of mem words.

    ``MemView`` objects do **not** cache reads. Each attribute access
    that consumes a word goes to the bus. Use :meth:`copy` /
    :meth:`read` to take a one-burst snapshot ndarray when you need a
    stable view to operate on with NumPy.
    """

    __slots__ = ("_mem", "_start", "_stop")

    def __init__(self, mem: Any, start: int, stop: int) -> None:
        if start < 0:
            raise ValueError(f"MemView start must be non-negative, got {start}")
        if stop < start:
            raise ValueError(f"MemView stop ({stop}) < start ({start})")
        self._mem = mem
        self._start = int(start)
        self._stop = int(stop)

    # -- core protocol ----------------------------------------------------

    def __len__(self) -> int:
        return self._stop - self._start

    def __iter__(self) -> Iterator[int]:
        # Each next() is one read; users who want a snapshot should
        # iterate over self.copy() instead.
        for i in range(self._start, self._stop):
            yield int(_get_word(self._mem, i))

    def __getitem__(self, index: int | slice) -> int | MemView:
        if isinstance(index, slice):
            substart, substop, _ = _normalize_slice(index, len(self))
            return MemView(self._mem, self._start + substart, self._start + substop)
        if not isinstance(index, (int, np.integer)):
            raise TypeError(
                f"MemView indices must be int or slice, not {type(index).__name__}"
            )
        i = int(index)
        if i < 0:
            i += len(self)
        if not 0 <= i < len(self):
            raise IndexError(f"MemView index {index} out of range [0, {len(self)})")
        return int(_get_word(self._mem, self._start + i))

    def __setitem__(self, index: int | slice, value: Any) -> None:
        if isinstance(index, slice):
            substart, substop, _ = _normalize_slice(index, len(self))
            count = substop - substart
            values = self._broadcast(value, count)
            _write_block(self._mem, self._start + substart, values)
            return
        if not isinstance(index, (int, np.integer)):
            raise TypeError(
                f"MemView indices must be int or slice, not {type(index).__name__}"
            )
        i = int(index)
        if i < 0:
            i += len(self)
        if not 0 <= i < len(self):
            raise IndexError(f"MemView index {index} out of range [0, {len(self)})")
        _set_word(self._mem, self._start + i, int(value))

    def __delitem__(self, index: int | slice) -> None:
        raise NotImplementedError("MemView does not support deletion (mem has no concept of it)")

    # -- snapshot / NumPy interop ----------------------------------------

    def copy(self) -> NDArray[np.unsignedinteger]:
        """One-burst snapshot of this view as an ``ndarray``.

        Equivalent to ``np.asarray(self)``; the returned array is a
        copy, so subsequent writes to it do *not* propagate back to
        the bus.
        """
        count = len(self)
        dtype = _np_dtype_for(self._mem)
        if count == 0:
            return np.empty(0, dtype=dtype)
        words = _read_block(self._mem, self._start, count)
        return np.asarray(words, dtype=dtype)

    def read(self) -> NDArray[np.unsignedinteger]:
        """Alias for :meth:`copy`, symmetric with ``reg.read()``."""
        return self.copy()

    def __array__(self, dtype: Any = None, copy: bool | None = None) -> NDArray[Any]:
        # NumPy passes ``copy`` (added in NumPy 2). We always produce
        # a fresh snapshot, so the parameter is informational here.
        del copy
        snapshot = self.copy()
        if dtype is None:
            return snapshot
        return snapshot.astype(np.dtype(dtype), copy=False)

    # -- byte-level escape hatch -----------------------------------------

    def read_bytes(self, offset: int = 0, n: int | None = None) -> bytes:
        """Read ``n`` bytes (default: rest of the view) starting at byte
        ``offset`` within this view.
        """
        wbytes = _word_width_bytes(self._mem)
        total = len(self) * wbytes
        if offset < 0 or offset > total:
            raise ValueError(f"byte offset {offset} out of range [0, {total}]")
        if n is None:
            n = total - offset
        if n < 0:
            raise ValueError(f"byte count must be non-negative, got {n}")
        if offset + n > total:
            raise ValueError(
                f"read_bytes(offset={offset}, n={n}) exceeds view size {total} bytes"
            )
        if n == 0:
            return b""
        first_word = offset // wbytes
        last_word = (offset + n + wbytes - 1) // wbytes
        snap = MemView(self._mem, self._start + first_word, self._start + last_word).copy()
        raw = snap.tobytes()
        local_offset = offset - first_word * wbytes
        return bytes(raw[local_offset : local_offset + n])

    def write_bytes(self, offset: int = 0, data: bytes | bytearray | memoryview = b"") -> None:
        """Write ``data`` starting at byte ``offset`` within this view.

        Currently requires ``offset`` and ``len(data)`` to be aligned to
        the word size; partial-word writes via read-modify-write are
        not yet supported.
        """
        wbytes = _word_width_bytes(self._mem)
        n = len(data)
        total = len(self) * wbytes
        if offset < 0 or offset + n > total:
            raise ValueError(
                f"write_bytes(offset={offset}, len={n}) out of range [0, {total}]"
            )
        if offset % wbytes != 0 or n % wbytes != 0:
            raise NotImplementedError(
                "MemView.write_bytes currently requires word-aligned offset and length; "
                f"got offset={offset}, len={n}, wbytes={wbytes}"
            )
        if n == 0:
            return
        dtype = _np_dtype_for(self._mem)
        arr = np.frombuffer(bytes(data), dtype=dtype)
        first_word = offset // wbytes
        _write_block(self._mem, self._start + first_word, arr.tolist())

    # -- internals --------------------------------------------------------

    def _broadcast(self, value: Any, count: int) -> list[int]:
        """Expand a scalar / array-like into ``count`` integers."""
        if isinstance(value, (int, np.integer)):
            return [int(value)] * count
        if isinstance(value, MemView):
            value = value.copy()
        if isinstance(value, np.ndarray):
            if value.size != count:
                raise ValueError(
                    f"cannot assign {value.size} values to MemView slice of length {count}"
                )
            return [int(v) for v in value.ravel()]
        if isinstance(value, (bytes, bytearray, memoryview)):
            dtype = _np_dtype_for(self._mem)
            arr = np.frombuffer(bytes(value), dtype=dtype)
            if arr.size != count:
                raise ValueError(
                    f"cannot assign {arr.size} words from buffer to MemView slice of length {count}"
                )
            return [int(v) for v in arr]
        try:
            seq = list(value)
        except TypeError as exc:
            raise TypeError(
                f"cannot assign object of type {type(value).__name__} to a MemView slice"
            ) from exc
        if len(seq) != count:
            raise ValueError(
                f"cannot assign {len(seq)} values to MemView slice of length {count}"
            )
        return [int(v) for v in seq]

    # -- diagnostics ------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"MemView(mem={type(self._mem).__name__}, "
            f"start={self._start}, stop={self._stop}, length={len(self)})"
        )


# ---------------------------------------------------------------------------
# MemWindow -- buffered context manager.
# ---------------------------------------------------------------------------


class MemWindow:
    """Buffered, write-back context returned by ``mem.window()``.

    Reads on entry are coalesced into one burst; writes are buffered
    and flushed in one burst on context exit. Inside the ``with``
    block the user sees a NumPy-array-like object that supports
    indexing, slicing, and assignment without touching the bus.

    On context-exit failure, only words that were written through
    this window are flushed (a clean abort would require additional
    journaling). Use :meth:`flush` or :meth:`discard` for explicit
    control.
    """

    __slots__ = ("_buffer", "_dirty", "_flushed", "_length", "_mem", "_start")

    def __init__(self, mem: Any, start: int, length: int) -> None:
        self._mem = mem
        self._start = int(start)
        self._length = int(length)
        self._buffer: NDArray[np.unsignedinteger] = np.empty(0, dtype=_np_dtype_for(mem))
        self._dirty: list[bool] = []
        self._flushed = False

    def __enter__(self) -> MemWindow:
        # Prime the buffer with one bulk read.
        if self._length:
            words = _read_block(self._mem, self._start, self._length)
            dtype = _np_dtype_for(self._mem)
            self._buffer = np.asarray(words, dtype=dtype)
        else:
            self._buffer = np.empty(0, dtype=_np_dtype_for(self._mem))
        self._dirty = [False] * self._length
        self._flushed = False
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # Flush whatever was buffered, even on exceptions, so writes
        # the user already executed are not silently dropped.
        if not self._flushed:
            self.flush()

    def __len__(self) -> int:
        return self._length

    def __getitem__(self, index: int | slice) -> Any:
        return self._buffer[index]

    def __setitem__(self, index: int | slice, value: Any) -> None:
        if isinstance(index, slice):
            start, stop, _ = _normalize_slice(index, self._length)
            self._buffer[start:stop] = value
            for i in range(start, stop):
                self._dirty[i] = True
            return
        if isinstance(index, (int, np.integer)):
            i = int(index)
            if i < 0:
                i += self._length
            self._buffer[i] = value
            self._dirty[i] = True
            return
        raise TypeError(
            f"MemWindow indices must be int or slice, not {type(index).__name__}"
        )

    def flush(self) -> None:
        """Write back every dirty word in one (coalesced) burst."""
        if self._length == 0:
            self._flushed = True
            return
        # Coalesce dirty runs to maximize burst length.
        i = 0
        while i < self._length:
            if not self._dirty[i]:
                i += 1
                continue
            j = i
            while j < self._length and self._dirty[j]:
                j += 1
            _write_block(self._mem, self._start + i, self._buffer[i:j].tolist())
            for k in range(i, j):
                self._dirty[k] = False
            i = j
        self._flushed = True

    def discard(self) -> None:
        """Forget all buffered writes without touching the bus."""
        for i in range(self._length):
            self._dirty[i] = False
        self._flushed = True

    @property
    def buffer(self) -> NDArray[np.unsignedinteger]:
        """Underlying numpy buffer (writable; mark words dirty manually)."""
        return self._buffer

    def __repr__(self) -> str:
        return (
            f"MemWindow(mem={type(self._mem).__name__}, start={self._start}, "
            f"length={self._length}, dirty={sum(self._dirty)})"
        )


# ---------------------------------------------------------------------------
# Methods grafted onto the mem class.
# ---------------------------------------------------------------------------


def _mem_size_bytes(self: Any) -> int:
    return _depth(self) * _word_width_bytes(self)


def _mem_depth(self: Any) -> int:
    return _depth(self)


def _mem_word_width(self: Any) -> int:
    return _word_width_bits(self)


def _mem_base_address(self: Any) -> int:
    return _base_address(self)


def _mem_window(self: Any, offset: int = 0, length: int | None = None) -> MemWindow:
    """Open a buffered, write-back window over ``[offset, offset+length)``.

    Use as a context manager::

        with mem.window(0, 256) as w:
            for i in range(256): w[i] = i
        # writes flushed on exit
    """
    depth = _depth(self)
    if length is None:
        length = depth - offset
    if offset < 0 or length < 0 or offset + length > depth:
        raise ValueError(
            f"window(offset={offset}, length={length}) out of range [0, {depth}]"
        )
    return MemWindow(self, offset, length)


def _mem_iter_chunks(self: Any, size: int = 4096) -> Iterator[NDArray[np.unsignedinteger]]:
    """Yield successive ``ndarray`` snapshots of ``size`` words.

    The final chunk may be shorter than ``size``.
    """
    if size <= 0:
        raise ValueError(f"chunk size must be positive, got {size}")
    depth = _depth(self)
    for start in range(0, depth, size):
        stop = min(start + size, depth)
        yield MemView(self, start, stop).copy()


def _mem_read_into(
    self: Any,
    buf: NDArray[Any],
    offset: int = 0,
) -> NDArray[Any]:
    """Fill a pre-allocated ``ndarray`` with ``len(buf)`` words from
    ``offset`` in one burst.

    The supplied buffer is returned for convenience. NumPy handles
    casting if its dtype differs from the memory word width.
    """
    if not isinstance(buf, np.ndarray):
        raise TypeError("read_into requires a numpy ndarray destination")
    if buf.ndim != 1:
        raise ValueError("read_into expects a 1-D destination buffer")
    count = int(buf.size)
    depth = _depth(self)
    if offset < 0 or offset + count > depth:
        raise ValueError(
            f"read_into(offset={offset}, count={count}) out of range [0, {depth}]"
        )
    if count == 0:
        return buf
    words = _read_block(self, offset, count)
    np.copyto(buf, np.asarray(words, dtype=_np_dtype_for(self)), casting="unsafe")
    return buf


def _mem_write_from(self: Any, buf: NDArray[Any], offset: int = 0) -> None:
    """Write ``len(buf)`` words from ``buf`` starting at ``offset`` in one burst."""
    if not isinstance(buf, np.ndarray):
        buf = np.asarray(buf, dtype=_np_dtype_for(self))
    if buf.ndim != 1:
        raise ValueError("write_from expects a 1-D source buffer")
    count = int(buf.size)
    depth = _depth(self)
    if offset < 0 or offset + count > depth:
        raise ValueError(
            f"write_from(offset={offset}, count={count}) out of range [0, {depth}]"
        )
    if count == 0:
        return
    _write_block(self, offset, buf.tolist())


def _mem_read_bytes(self: Any, offset: int = 0, n: int | None = None) -> bytes:
    return MemView(self, 0, _depth(self)).read_bytes(offset, n)


def _mem_write_bytes(self: Any, offset: int = 0, data: bytes = b"") -> None:
    MemView(self, 0, _depth(self)).write_bytes(offset, data)


def _mem_array(self: Any, dtype: Any = None, copy: bool | None = None) -> NDArray[Any]:
    del copy
    snap = MemView(self, 0, _depth(self)).copy()
    if dtype is None:
        return snap
    return snap.astype(np.dtype(dtype), copy=False)


def _mem_getitem(orig_getitem: Any) -> Any:
    """Wrap an existing ``__getitem__`` so slices return ``MemView`` and
    int access returns a plain word value (not the underlying entry).
    """

    def __getitem__(self: Any, index: int | slice) -> Any:
        if isinstance(index, slice):
            start, stop, _ = _normalize_slice(index, _depth(self))
            return MemView(self, start, stop)
        return _unwrap_word(orig_getitem(self, index))

    # Tag the wrapper so ``_raw_get`` / ``_set_word`` can reach the
    # original (unwrapped-entry-returning) ``__getitem__``.
    __getitem__.__peakrdl_orig_getitem__ = orig_getitem  # type: ignore[attr-defined]
    return __getitem__


def _mem_setitem(orig_setitem: Any) -> Any:
    """Wrap an existing ``__setitem__`` so slice assignment is coalesced."""

    def __setitem__(self: Any, index: int | slice, value: Any) -> None:
        if isinstance(index, slice):
            start, stop, _ = _normalize_slice(index, _depth(self))
            view = MemView(self, start, stop)
            values = view._broadcast(value, stop - start)
            _write_block(self, start, values)
            return
        if orig_setitem is not None:
            orig_setitem(self, index, value)
            return
        _set_word(self, int(index), int(value))

    __setitem__.__peakrdl_orig_setitem__ = orig_setitem  # type: ignore[attr-defined]
    return __setitem__


_ENHANCED_FLAG = "__peakrdl_mem_view_enhanced__"


def _grafts() -> dict[str, Any]:
    """Method dictionary applied by ``enhance_*`` helpers."""
    return {
        "size_bytes": property(_mem_size_bytes),
        "depth": property(_mem_depth),
        "word_width": property(_mem_word_width),
        "base_address": property(_mem_base_address),
        "window": _mem_window,
        "iter_chunks": _mem_iter_chunks,
        "read_into": _mem_read_into,
        "write_from": _mem_write_from,
        "read_bytes": _mem_read_bytes,
        "write_bytes": _mem_write_bytes,
        "__array__": _mem_array,
    }


def enhance_mem_class(cls: type) -> type:
    """Decorate a generated mem class in place with :class:`MemView` glue.

    Idempotent. Returns the same class for use as a decorator.
    """
    if getattr(cls, _ENHANCED_FLAG, False):
        return cls
    grafts = _grafts()
    for name, value in grafts.items():
        # Don't clobber a property/method that the class already defines
        # with a richer implementation; only shim what's missing.
        if name in cls.__dict__:
            continue
        setattr(cls, name, value)
    # Always rewrite getitem/setitem -- the existing ones return entry
    # objects / lists, but we want slice handling to produce MemView.
    orig_getitem = getattr(cls, "__getitem__", None)
    if orig_getitem is not None:
        cls.__getitem__ = _mem_getitem(orig_getitem)  # type: ignore[method-assign]
    orig_setitem = getattr(cls, "__setitem__", None)
    cls.__setitem__ = _mem_setitem(orig_setitem)  # type: ignore[method-assign]
    setattr(cls, _ENHANCED_FLAG, True)
    return cls


def enhance_mem_instance(mem: Any) -> Any:
    """Decorate a single mem instance (when the class can't be patched).

    Falls back to per-instance attribute assignment for objects whose
    class has ``__slots__`` or is otherwise immutable.
    """
    cls = type(mem)
    if not getattr(cls, _ENHANCED_FLAG, False):
        try:
            enhance_mem_class(cls)
        except (AttributeError, TypeError):
            # Frozen class (e.g. C extension): bind unbound functions to mem.
            for name, value in _grafts().items():
                if isinstance(value, property):
                    continue  # properties don't bind on instances
                with contextlib.suppress(AttributeError, TypeError):
                    setattr(mem, name, value.__get__(mem, cls))
    return mem


# ---------------------------------------------------------------------------
# Optional registry wiring (Unit 1's seam).
# ---------------------------------------------------------------------------


def _register_with_registry() -> None:
    """Register :func:`enhance_mem_class` with Unit 1's node-attribute hook,
    if that registry is importable. Silent no-op otherwise.
    """
    try:
        from .._registry import register_node_attribute  # type: ignore[attr-defined]
    except (ImportError, ModuleNotFoundError):
        return
    with contextlib.suppress(Exception):
        register_node_attribute("mem", enhance_mem_class)


_register_with_registry()
