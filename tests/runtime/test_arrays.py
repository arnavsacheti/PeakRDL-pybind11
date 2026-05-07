"""Unit tests for ``peakrdl_pybind11.runtime.arrays``.

The tests are deliberately *pure-Python*: they wire fake register/field
descriptors via simple dataclasses, exercise :class:`ArrayView` /
:class:`FieldArray` end-to-end, and assert against NumPy semantics
called out in ``docs/IDEAL_API_SKETCH.md`` §7.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from peakrdl_pybind11.runtime.arrays import (
    ArrayView,
    FieldArray,
    register_register_enhancement,
    reset_enhancements,
    wrap_array,
)


# ---------------------------------------------------------------------------
# Mock descriptors
# ---------------------------------------------------------------------------


class _MockField:
    """One field on a register: stores a value, exposes read/write."""

    def __init__(self, name: str, value: int = 0) -> None:
        self.name = name
        self._value = int(value)

    def read(self) -> int:
        return self._value

    def write(self, value: int) -> None:
        self._value = int(value)


class _MockBoolField(_MockField):
    """1-bit field: read returns ``bool``."""

    def read(self) -> bool:  # type: ignore[override]
        return bool(self._value)


class _MockConfig:
    """``reg.config.enable`` shape for field-projection tests."""

    def __init__(self, enable: bool = False, baudrate: int = 0) -> None:
        self.enable = _MockBoolField("enable", enable)
        self.baudrate = _MockField("baudrate", baudrate)


class _MockRegister:
    """A minimal register descriptor.

    Mirrors the surface used by :class:`ArrayView`: ``read`` / ``write``
    / ``modify`` plus an arbitrary ``.config`` sub-object that holds
    fields. The ``address``/``offset``/``width`` attributes let
    ``read_many`` / ``write_many`` plumbing kick in when a master is
    attached.
    """

    def __init__(
        self,
        address: int = 0,
        width: int = 4,
        value: int = 0,
        master: Any | None = None,
    ) -> None:
        self.address = address
        self.offset = address
        self.width = width
        self._value = int(value)
        self._master = master
        self.config = _MockConfig()

    def read(self) -> int:
        return self._value

    def write(self, value: int) -> None:
        self._value = int(value)

    def modify(self, **fields: Any) -> None:
        # 1 read + 1 write semantics; fields just shadow .config attrs.
        for name, value in fields.items():
            field = getattr(self.config, name)
            field.write(int(value))


class _CountingMaster:
    """Master that counts read_many / write_many invocations."""

    def __init__(self) -> None:
        self.read_calls: int = 0
        self.write_calls: int = 0
        self.last_read_ops: list[Any] = []
        self.last_write_ops: list[Any] = []
        self._memory: dict[int, int] = {}

    def read(self, address: int, width: int) -> int:  # noqa: ARG002
        return self._memory.get(address, 0)

    def write(self, address: int, value: int, width: int) -> None:  # noqa: ARG002
        self._memory[address] = int(value)

    def read_many(self, ops: list[Any]) -> list[int]:
        self.read_calls += 1
        self.last_read_ops = list(ops)
        return [self._memory.get(op.address, 0) for op in ops]

    def write_many(self, ops: list[Any]) -> None:
        self.write_calls += 1
        self.last_write_ops = list(ops)
        for op in ops:
            self._memory[op.address] = int(op.value)


def _make_register_array(n: int = 8, *, master: Any | None = None) -> ArrayView:
    elements = [_MockRegister(address=0x4000 + 4 * i, master=master) for i in range(n)]
    return ArrayView(elements, (n,))


def _make_2d_register_array(rows: int = 4, cols: int = 16) -> ArrayView:
    n = rows * cols
    elements = [_MockRegister(address=0x4000 + 4 * i) for i in range(n)]
    return ArrayView(elements, (rows, cols))


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


class TestIndexing:
    def test_int_index_returns_element(self) -> None:
        arr = _make_register_array(8)
        third = arr[3]
        assert isinstance(third, _MockRegister)
        assert third.address == 0x4000 + 4 * 3

    def test_negative_index_returns_last(self) -> None:
        arr = _make_register_array(8)
        last = arr[-1]
        assert isinstance(last, _MockRegister)
        assert last.address == 0x4000 + 4 * 7

    def test_len_matches_first_dim(self) -> None:
        arr = _make_register_array(8)
        assert len(arr) == 8

    def test_iter_yields_each_element(self) -> None:
        arr = _make_register_array(4)
        elements = list(arr)
        assert len(elements) == 4
        assert all(isinstance(e, _MockRegister) for e in elements)

    def test_full_slice_returns_array_view(self) -> None:
        arr = _make_register_array(8)
        view = arr[:]
        assert isinstance(view, ArrayView)
        assert view.shape == (8,)
        assert len(view) == 8

    def test_partial_slice(self) -> None:
        arr = _make_register_array(8)
        view = arr[2:6]
        assert isinstance(view, ArrayView)
        assert view.shape == (4,)
        assert view[0] is arr[2]
        assert view[-1] is arr[5]

    def test_step_slice(self) -> None:
        arr = _make_register_array(8)
        view = arr[::2]
        assert view.shape == (4,)
        assert view[0] is arr[0]
        assert view[1] is arr[2]
        assert view[3] is arr[6]

    def test_out_of_bounds_raises(self) -> None:
        arr = _make_register_array(8)
        with pytest.raises(IndexError):
            arr[8]
        with pytest.raises(IndexError):
            arr[-9]

    def test_invalid_key_raises(self) -> None:
        arr = _make_register_array(8)
        with pytest.raises(TypeError):
            arr["foo"]  # type: ignore[index]

    def test_contains_int(self) -> None:
        arr = _make_register_array(8)
        assert 3 in arr
        assert 8 not in arr
        assert -1 not in arr  # contains is positional, not python-list-like


class TestMultiDim:
    def test_shape_property(self) -> None:
        arr = _make_2d_register_array(4, 16)
        assert arr.shape == (4, 16)

    def test_tuple_indexing(self) -> None:
        arr = _make_2d_register_array(4, 16)
        el = arr[2, 5]
        assert isinstance(el, _MockRegister)
        assert el.address == 0x4000 + 4 * (2 * 16 + 5)

    def test_chained_indexing(self) -> None:
        arr = _make_2d_register_array(4, 16)
        row = arr[2]
        assert isinstance(row, ArrayView)
        assert row.shape == (16,)
        el = row[5]
        assert el is arr[2, 5]

    def test_partial_tuple_returns_view(self) -> None:
        arr = _make_2d_register_array(4, 16)
        row_slice = arr[2, :]
        assert isinstance(row_slice, ArrayView)
        assert row_slice.shape == (16,)
        assert row_slice[0] is arr[2, 0]

    def test_too_many_indices_raises(self) -> None:
        arr = _make_2d_register_array(4, 16)
        with pytest.raises(IndexError):
            arr[2, 5, 1]

    def test_two_slices_returns_2d_view(self) -> None:
        arr = _make_2d_register_array(4, 16)
        sub = arr[1:3, 4:8]
        assert isinstance(sub, ArrayView)
        assert sub.shape == (2, 4)
        assert sub[0, 0] is arr[1, 4]


# ---------------------------------------------------------------------------
# Bulk read
# ---------------------------------------------------------------------------


class TestBulkRead:
    def test_read_returns_ndarray_of_length_n(self) -> None:
        arr = _make_register_array(8)
        for i, el in enumerate(arr):
            el.write(i * 10)
        result = arr[:].read()
        assert isinstance(result, np.ndarray)
        assert result.shape == (8,)
        assert list(result) == [i * 10 for i in range(8)]

    def test_read_preserves_2d_shape(self) -> None:
        arr = _make_2d_register_array(4, 16)
        for i, el in enumerate(arr._iter_elements()):
            el.write(i)
        result = arr[:].read()
        assert result.shape == (4, 16)

    def test_read_uses_master_read_many(self) -> None:
        master = _CountingMaster()
        arr = _make_register_array(8, master=master)
        # Pre-load memory through the master.
        for i, el in enumerate(arr):
            master._memory[el.address] = i + 1
            el.write(i + 1)  # Keep the descriptor in sync (read() returns this).
        result = arr[:].read()
        assert master.read_calls == 1, "expected exactly one read_many call"
        assert isinstance(result, np.ndarray)
        assert list(result) == [i + 1 for i in range(8)]

    def test_read_falls_back_without_master(self) -> None:
        arr = _make_register_array(4)  # no master attached
        for i, el in enumerate(arr):
            el.write(i + 100)
        result = arr[:].read()
        assert list(result) == [100, 101, 102, 103]


# ---------------------------------------------------------------------------
# Bulk write
# ---------------------------------------------------------------------------


class TestBulkWrite:
    def test_setitem_broadcast_int(self) -> None:
        arr = _make_register_array(8)
        for el in arr:
            el.write(0xFF)
        arr[:] = 0
        for el in arr:
            assert el.read() == 0

    def test_setitem_arange(self) -> None:
        arr = _make_register_array(8)
        arr[:] = np.arange(8)
        for i, el in enumerate(arr):
            assert el.read() == i

    def test_setitem_list(self) -> None:
        arr = _make_register_array(4)
        arr[:] = [10, 20, 30, 40]
        assert [el.read() for el in arr] == [10, 20, 30, 40]

    def test_setitem_uses_write_many(self) -> None:
        master = _CountingMaster()
        arr = _make_register_array(8, master=master)
        arr[:] = np.arange(8) * 2
        assert master.write_calls == 1, "expected exactly one write_many call"
        assert len(master.last_write_ops) == 8
        assert [op.value for op in master.last_write_ops] == list(range(0, 16, 2))

    def test_setitem_size_mismatch_raises(self) -> None:
        arr = _make_register_array(4)
        with pytest.raises(ValueError):
            arr[:] = np.arange(7)

    def test_setitem_partial_slice(self) -> None:
        arr = _make_register_array(8)
        for el in arr:
            el.write(0)
        arr[2:6] = 0xAA
        values = [el.read() for el in arr]
        assert values == [0, 0, 0xAA, 0xAA, 0xAA, 0xAA, 0, 0]


# ---------------------------------------------------------------------------
# Field projection
# ---------------------------------------------------------------------------


class TestFieldProjection:
    def test_projected_read_returns_ndarray_bool(self) -> None:
        arr = _make_register_array(8)
        # Set every other element's enable bit.
        for i, el in enumerate(arr):
            el.config.enable.write(i % 2)
        values = arr[:].config.enable.read()
        assert isinstance(values, np.ndarray)
        assert values.dtype == np.bool_
        assert list(values.tolist()) == [False, True, False, True, False, True, False, True]

    def test_projected_read_returns_uint_for_multibit(self) -> None:
        arr = _make_register_array(8)
        for i, el in enumerate(arr):
            el.config.baudrate.write(i * 3)
        values = arr[:].config.baudrate.read()
        assert isinstance(values, np.ndarray)
        assert values.dtype.kind in "biu"
        assert list(values.tolist()) == [i * 3 for i in range(8)]

    def test_projected_read_preserves_2d_shape(self) -> None:
        arr = _make_2d_register_array(4, 16)
        for i, el in enumerate(arr._iter_elements()):
            el.config.enable.write(i % 2)
        values = arr[:].config.enable.read()
        assert values.shape == (4, 16)

    def test_projected_write_broadcast(self) -> None:
        arr = _make_register_array(4)
        arr[:].config.enable.write(1)
        for el in arr:
            assert el.config.enable.read() is True

    def test_projected_write_elementwise(self) -> None:
        arr = _make_register_array(4)
        arr[:].config.baudrate.write([10, 20, 30, 40])
        assert [el.config.baudrate.read() for el in arr] == [10, 20, 30, 40]


# ---------------------------------------------------------------------------
# modify
# ---------------------------------------------------------------------------


class TestModify:
    def test_modify_runs_per_element(self) -> None:
        arr = _make_register_array(8)
        # Pre-state: every element disabled.
        for el in arr:
            el.config.enable.write(0)

        # Track modify calls per element by wrapping the method.
        seen: list[int] = []
        for el in arr:
            real_modify = el.modify

            def traced(self_addr: int = el.address, _real: Any = real_modify, **kw: Any) -> None:
                seen.append(self_addr)
                _real(**kw)

            el.modify = traced  # type: ignore[method-assign]

        arr[:].modify(enable=1)
        assert len(seen) == 8, "expected one RMW per element (N RMWs)"
        for el in arr:
            assert el.config.enable.read() is True


# ---------------------------------------------------------------------------
# FieldArray
# ---------------------------------------------------------------------------


class TestFieldArray:
    def test_field_array_bool(self) -> None:
        bits = [_MockBoolField(f"bit{i}", i % 2) for i in range(16)]
        # FieldArray needs a width hint to pick bool dtype; the mock sets width=1.
        for b in bits:
            b.width = 1  # type: ignore[attr-defined]
        arr = FieldArray(bits, (16,))
        result = arr.read()
        assert result.dtype == np.bool_
        assert result.shape == (16,)
        assert list(result.tolist()) == [bool(i % 2) for i in range(16)]

    def test_field_array_multibit(self) -> None:
        fields = [_MockField(f"f{i}", i * 7) for i in range(8)]
        for f in fields:
            f.width = 4  # type: ignore[attr-defined]
        arr = FieldArray(fields, (8,))
        result = arr.read()
        assert result.dtype == np.uint64
        assert list(result.tolist()) == [i * 7 for i in range(8)]


# ---------------------------------------------------------------------------
# Wiring hook
# ---------------------------------------------------------------------------


class TestWiringHook:
    def setup_method(self) -> None:
        reset_enhancements()

    def teardown_method(self) -> None:
        reset_enhancements()

    def test_default_hook_wraps_with_array_view(self) -> None:
        register_register_enhancement()  # default hook
        elements = [_MockRegister(address=0x100 + 4 * i) for i in range(4)]
        view = wrap_array(_MockRegister, "channel", elements, (4,))
        assert isinstance(view, ArrayView)
        assert view.shape == (4,)
        assert view[0] is elements[0]

    def test_custom_hook_takes_precedence(self) -> None:
        sentinel = object()

        def custom(
            parent_cls: type, name: str, elements: Any, shape: tuple[int, ...]
        ) -> object:
            return sentinel

        register_register_enhancement(custom)
        result = wrap_array(_MockRegister, "channel", [_MockRegister()], (1,))
        assert result is sentinel

    def test_custom_class_via_array_view_cls(self) -> None:
        class _Subclass(ArrayView):
            tagged: bool = True

        register_register_enhancement(array_view_cls=_Subclass)
        view = wrap_array(_MockRegister, "channel", [_MockRegister()], (1,))
        assert isinstance(view, _Subclass)

    def test_no_hook_falls_back_to_array_view(self) -> None:
        # No hook registered.
        view = wrap_array(_MockRegister, "channel", [_MockRegister()], (1,))
        assert isinstance(view, ArrayView)

    def test_register_is_idempotent(self) -> None:
        from peakrdl_pybind11.runtime.arrays import _REGISTERED_HOOKS

        def hook(*args: Any, **kw: Any) -> None:
            return None

        register_register_enhancement(hook)
        register_register_enhancement(hook)
        assert _REGISTERED_HOOKS.count(hook) == 1

    def test_default_hook_is_idempotent_per_class(self) -> None:
        from peakrdl_pybind11.runtime.arrays import _REGISTERED_HOOKS

        register_register_enhancement()
        register_register_enhancement()
        register_register_enhancement(array_view_cls=FieldArray)
        register_register_enhancement(array_view_cls=FieldArray)
        # Two distinct default hooks (one per class), each registered once.
        assert len(_REGISTERED_HOOKS) == 2


# ---------------------------------------------------------------------------
# ArrayView constructor edge cases
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_default_shape_uses_length(self) -> None:
        elements = [_MockRegister() for _ in range(5)]
        arr = ArrayView(elements)
        assert arr.shape == (5,)

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            ArrayView([_MockRegister() for _ in range(4)], (3, 2))

    def test_shape_with_correct_product(self) -> None:
        elements = [_MockRegister() for _ in range(12)]
        arr = ArrayView(elements, (3, 4))
        assert arr.shape == (3, 4)
        assert arr[2, 3] is elements[2 * 4 + 3]

    def test_repr(self) -> None:
        arr = _make_register_array(8)
        rep = repr(arr)
        assert "ArrayView" in rep
        assert "shape" in rep
