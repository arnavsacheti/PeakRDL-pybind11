"""Typed array views for register / regfile / field arrays.

Implements the ``ArrayView`` / ``FieldArray`` surface from
``docs/IDEAL_API_SKETCH.md`` §7. The view is a *bus-bound* sequence: it
holds references to the underlying generated register/field descriptor
instances and dispatches reads/writes through them. Bulk reads coalesce
into ``master.read_many`` when a master is reachable; bulk writes
coalesce into ``master.write_many``. Field projection
(``arr.config.enable.read()``) walks the same attribute path on each
element and returns a NumPy array of the homogeneous values.

The surface is intentionally NumPy-first: ``read()`` always returns an
``ndarray`` (1-D for scalar values; structured ``dtype`` when projecting
across multiple fields). ``arr[:] = value`` accepts a Python int (which
is broadcast across all elements) or any array-like with matching length
(elementwise write). ``arr[:].modify(**fields)`` issues one
read-modify-write per element -- the caller asked for a coalesced bulk
form by name only when the underlying field write semantics permit it.

``Any`` is used pervasively in this module: the API takes user-typed
register/field descriptor classes that have ``.read``/``.write``/etc.
but no enforced base type. Tightening the annotation to a Protocol
would over-constrain callers; the duck-typed surface is the contract.
"""

# ruff: noqa: ANN401  (see module docstring)

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any

import numpy as np

__all__ = [
    "ArrayView",
    "FieldArray",
    "register_register_enhancement",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_major_strides(shape: tuple[int, ...]) -> tuple[int, ...]:
    """Compute row-major strides (in *elements*) for ``shape``.

    For a 2-D shape ``(R, C)`` the result is ``(C, 1)``; for 3-D
    ``(D, R, C)`` it is ``(R*C, C, 1)``. Always non-empty when ``shape``
    is non-empty.
    """
    if not shape:
        return ()
    strides: list[int] = [1]
    for dim in reversed(shape[1:]):
        strides.append(strides[-1] * dim)
    return tuple(reversed(strides))


def _normalize_int_index(idx: int, dim_size: int, axis: int) -> int:
    """Normalize a (possibly negative) integer index against ``dim_size``."""
    if not isinstance(idx, (int, np.integer)):
        raise TypeError(
            f"array index must be int, slice, or tuple thereof; got {type(idx).__name__}"
        )
    real = int(idx)
    if real < 0:
        real += dim_size
    if real < 0 or real >= dim_size:
        raise IndexError(
            f"index {idx} is out of bounds for axis {axis} with size {dim_size}"
        )
    return real


def _element_dtype_for_value(sample: Any) -> np.dtype:
    """Pick a NumPy dtype for an array-of-elements read.

    For pure boolean values use ``bool``; for everything that quacks like
    an integer use ``uint64`` (the generated register width is at most 64
    bits today; widening is safe). Strings / enums fall back to ``object``
    so the caller still gets a useful ndarray.
    """
    if isinstance(sample, (bool, np.bool_)):
        return np.dtype(bool)
    if isinstance(sample, (int, np.integer)):
        return np.dtype(np.uint64)
    if isinstance(sample, float):
        return np.dtype(np.float64)
    return np.dtype(object)


def _resolve_attr_path(obj: Any, path: tuple[str, ...]) -> Any:
    """Walk a dotted attribute path on ``obj``."""
    cur = obj
    for name in path:
        cur = getattr(cur, name)
    return cur


def _master_for(elements: Sequence[Any]) -> Any | None:
    """Find a master shared by all elements, if any.

    The C++ descriptor classes expose the master via either ``_master``
    or ``master``. We prefer ``_master`` first to match the existing
    code-gen convention. Returns ``None`` when masters are mismatched or
    absent -- callers fall back to per-element ``read``/``write``.
    """
    if not elements:
        return None
    candidates = ("_master", "master")
    master: Any | None = None
    for el in elements:
        found = None
        for name in candidates:
            if hasattr(el, name):
                found = getattr(el, name)
                break
        if found is None:
            return None
        if master is None:
            master = found
        elif master is not found:
            return None
    return master


def _make_access_op(element: Any, value: int = 0) -> Any:
    """Build an ``AccessOp`` for ``read_many`` / ``write_many`` if we can.

    Returns ``None`` if the element doesn't expose the metadata we need.
    The fallback path uses element-level ``read()``/``write()`` and is
    always correct, just slower.
    """
    try:
        from peakrdl_pybind11.masters.base import AccessOp
    except ImportError:  # pragma: no cover - defensive
        return None

    address = getattr(element, "address", None) or getattr(element, "offset", None)
    if address is None:
        return None
    width = getattr(element, "width", None) or 4

    return AccessOp(address=int(address), value=int(value), width=int(width))


def _values_to_ndarray(
    values: Sequence[Any], shape: tuple[int, ...]
) -> np.ndarray:
    """Promote a flat sequence of read values to an ``ndarray`` of ``shape``.

    Picks dtype from the first element; falls back to ``object`` when
    coercion fails. Empty inputs yield ``np.empty(0)``.
    """
    if not values:
        return np.empty(0)
    dtype = _element_dtype_for_value(values[0])
    try:
        if dtype.kind in "biu":
            arr = np.array([int(v) for v in values], dtype=dtype)
        else:
            arr = np.array(list(values), dtype=dtype)
    except (TypeError, ValueError):
        arr = np.array(list(values), dtype=object)
    return arr.reshape(shape) if shape else arr


# ---------------------------------------------------------------------------
# Field projection
# ---------------------------------------------------------------------------


class _FieldProjection:
    """Lazy attribute walk across an :class:`ArrayView`.

    Created when the user does ``arr[:].config`` or ``arr[:].config.enable``
    -- each ``__getattr__`` extends the dotted path. Materializing happens
    at ``.read()`` / ``.write()`` / ``.modify()`` time so the user only
    pays for the bus traffic at the leaf call.
    """

    __slots__ = ("_path", "_view")

    def __init__(self, view: ArrayView, path: tuple[str, ...]) -> None:
        self._view = view
        self._path = path

    def __getattr__(self, name: str) -> _FieldProjection:
        if name.startswith("_"):
            raise AttributeError(name)
        return _FieldProjection(self._view, (*self._path, name))

    def __repr__(self) -> str:
        return f"<FieldProjection {'.'.join(self._path)} over shape {self._view.shape}>"

    # ---- read / write ---------------------------------------------------

    def read(self) -> np.ndarray:
        """Read the field across every element, return ``ndarray``."""
        elements = list(self._view._iter_elements())
        if not elements:
            return np.empty(0)

        targets = [_resolve_attr_path(e, self._path) for e in elements]
        if hasattr(targets[0], "read"):
            values = [t.read() for t in targets]
        else:
            values = targets
        return _values_to_ndarray(values, self._view.shape)

    def write(self, value: Any) -> None:
        """Write ``value`` to the projected field on every element.

        ``value`` may be a scalar (broadcast) or an array-like whose
        length matches the flat element count.
        """
        elements = list(self._view._iter_elements())
        if not elements:
            return

        values = self._view._broadcast_values(value, len(elements))
        for el, v in zip(elements, values, strict=True):
            target = _resolve_attr_path(el, self._path)
            target.write(v)

    def modify(self, **fields: Any) -> None:
        """Issue ``N`` read-modify-writes -- one per element."""
        elements = list(self._view._iter_elements())
        for el in elements:
            target = _resolve_attr_path(el, self._path)
            target.modify(**fields)


# ---------------------------------------------------------------------------
# ArrayView
# ---------------------------------------------------------------------------


class ArrayView:
    """A typed, bus-bound sequence of register / regfile instances.

    Constructed with a flat (row-major) list of ``elements`` and a
    ``shape`` tuple whose product equals ``len(elements)``. Sub-views
    keep the same flat element list -- slicing produces a new view with
    a new ``shape`` and an index map onto the parent storage. Bulk
    reads/writes coalesce into ``read_many`` / ``write_many`` when a
    shared master is reachable.
    """

    __slots__ = ("_elements", "_indices", "_shape")

    def __init__(
        self,
        elements: Sequence[Any],
        shape: tuple[int, ...] | None = None,
        *,
        _indices: Sequence[int] | None = None,
    ) -> None:
        elements_list = list(elements)
        # Number of *visible* slots in this view: equals
        # ``len(_indices)`` for a sub-view, or ``len(elements_list)`` for
        # a top-level construction.
        visible = len(_indices) if _indices is not None else len(elements_list)
        if shape is None:
            shape = (visible,)
        else:
            shape = tuple(int(d) for d in shape)
            expected = 1
            for dim in shape:
                expected *= dim
            if expected != visible:
                raise ValueError(
                    f"shape {shape} (product={expected}) does not match "
                    f"visible element count {visible}"
                )
        self._elements: list[Any] = elements_list
        self._shape: tuple[int, ...] = shape
        # ``_indices`` lets a *slice* keep referencing the original element
        # storage without copying. None means "all elements, in order".
        if _indices is None:
            self._indices: list[int] = list(range(len(elements_list)))
        else:
            self._indices = list(_indices)

    # ---- basic dunder/property surface ----------------------------------

    @property
    def shape(self) -> tuple[int, ...]:
        """The (multi-dim) shape of the view."""
        return self._shape

    def __len__(self) -> int:
        return self._shape[0] if self._shape else 0

    def __iter__(self) -> Iterator[Any]:
        for i in range(len(self)):
            yield self[i]

    def __contains__(self, item: object) -> bool:
        if isinstance(item, (int, np.integer)):
            return 0 <= int(item) < len(self)
        for el in self._iter_elements():
            if el is item or el == item:
                return True
        return False

    def __repr__(self) -> str:
        kind = type(self).__name__
        return f"<{kind} shape={self._shape} len={len(self)}>"

    # ---- internal helpers -----------------------------------------------

    def _iter_elements(self) -> Iterator[Any]:
        """Iterate the *underlying* elements in row-major order."""
        for idx in self._indices:
            yield self._elements[idx]

    def _broadcast_values(self, value: Any, n: int) -> list[Any]:
        """Coerce ``value`` to a length-``n`` list for elementwise write."""
        if isinstance(value, (int, np.integer, float, bool, np.bool_)):
            return [value] * n
        if isinstance(value, np.ndarray):
            arr = value.ravel()
            if arr.size != n:
                raise ValueError(
                    f"cannot broadcast array of size {arr.size} to {n} elements"
                )
            return arr.tolist()
        if isinstance(value, str):
            # Treat as scalar (broadcast). Mirrors enum-by-name in §8.
            return [value] * n
        if isinstance(value, Iterable):
            seq = list(value)
            if len(seq) != n:
                raise ValueError(
                    f"cannot broadcast iterable of length {len(seq)} to {n} elements"
                )
            return seq
        # Fallback: scalar broadcast for anything else (e.g. enum members).
        return [value] * n

    # ---- indexing -------------------------------------------------------

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, tuple):
            return self._getitem_tuple(key)
        if isinstance(key, slice):
            return self._getitem_slice(key)
        if isinstance(key, (int, np.integer)):
            return self._getitem_int(int(key))
        raise TypeError(
            f"array indices must be int, slice, or tuple; got {type(key).__name__}"
        )

    def _getitem_int(self, idx: int) -> Any:
        if not self._shape:
            raise IndexError("0-d array is not indexable")
        normalized = _normalize_int_index(idx, self._shape[0], 0)
        if len(self._shape) == 1:
            return self._elements[self._indices[normalized]]
        # Drop the first dim, keep the rest of the shape.
        sub_shape = self._shape[1:]
        stride = 1
        for dim in sub_shape:
            stride *= dim
        start = normalized * stride
        sub_indices = self._indices[start : start + stride]
        return type(self)(self._elements, sub_shape, _indices=sub_indices)

    def _getitem_slice(self, sl: slice) -> ArrayView:
        if not self._shape:
            raise IndexError("0-d array is not indexable")
        start, stop, step = sl.indices(self._shape[0])
        rows = list(range(start, stop, step))
        sub_shape_tail = self._shape[1:]
        stride = 1
        for dim in sub_shape_tail:
            stride *= dim
        new_indices: list[int] = []
        for r in rows:
            base = r * stride
            new_indices.extend(self._indices[base : base + stride])
        new_shape = (len(rows), *sub_shape_tail)
        return type(self)(self._elements, new_shape, _indices=new_indices)

    def _getitem_tuple(self, key: tuple[Any, ...]) -> Any:
        # NumPy-like int/slice tuple indexing. Ellipsis and newaxis are
        # intentionally unsupported -- callers can chain ``arr[i][j]``.
        if len(key) > len(self._shape):
            raise IndexError(
                f"too many indices for array: {len(key)} for shape {self._shape}"
            )
        full_key: list[Any] = list(key) + [slice(None)] * (len(self._shape) - len(key))

        per_axis: list[list[int]] = []
        result_shape: list[int] = []
        for axis, item in enumerate(full_key):
            dim = self._shape[axis]
            if isinstance(item, (int, np.integer)):
                per_axis.append([_normalize_int_index(int(item), dim, axis)])
            elif isinstance(item, slice):
                start, stop, step = item.indices(dim)
                coords = list(range(start, stop, step))
                per_axis.append(coords)
                result_shape.append(len(coords))
            else:
                raise TypeError(
                    f"tuple element {axis} must be int or slice, got {type(item).__name__}"
                )

        strides = _row_major_strides(self._shape)
        flat_index_set = [0]
        for axis, coords in enumerate(per_axis):
            stride = strides[axis]
            flat_index_set = [base + c * stride for base in flat_index_set for c in coords]

        if not result_shape:
            return self._elements[self._indices[flat_index_set[0]]]

        new_indices = [self._indices[i] for i in flat_index_set]
        return type(self)(self._elements, tuple(result_shape), _indices=new_indices)

    # ---- attribute lookup: field projection -----------------------------

    def __getattr__(self, name: str) -> _FieldProjection:
        # ``__getattr__`` only fires when ordinary lookup misses, so
        # ``__slots__`` reads are unaffected. The leading-underscore
        # guard catches typos on private internals.
        if name.startswith("_"):
            raise AttributeError(name)
        return _FieldProjection(self, (name,))

    # ---- bulk read / write ---------------------------------------------

    def read(self) -> np.ndarray:
        """Bulk read of every element. Returns ``ndarray``.

        For homogeneous register/field arrays (the common case) the
        result is a 1-D / N-D ``ndarray[uint64]`` (or ``bool`` for 1-bit
        fields). For multi-field projection across a register array, use
        :meth:`structured_read` (or simply project a single field via
        ``arr.foo.read()``).
        """
        elements = list(self._iter_elements())
        if not elements:
            return np.empty(0)

        # Coalesce into ``master.read_many`` for the bus round-trip. We
        # still call per-element ``read()`` afterward to get any typed
        # decode (``RegisterInt`` etc.) -- the bus traffic already
        # happened, and the decode is local-only.
        master = _master_for(elements)
        if master is not None and hasattr(master, "read_many"):
            ops = [_make_access_op(el) for el in elements]
            if all(op is not None for op in ops):
                master.read_many(ops)
        values = [el.read() for el in elements]
        return _values_to_ndarray(values, self._shape)

    def structured_read(self) -> np.ndarray:
        """Read every field of every element into a *structured* ndarray.

        Field metadata is taken from the first element via the standard
        ``_REGISTER_FIELDS`` map populated by the runtime template
        (``runtime.py.jinja``). When that metadata is unavailable the
        method returns the same shape as :meth:`read`.
        """
        elements = list(self._iter_elements())
        if not elements:
            return np.empty(0)
        first = elements[0]

        field_spec = self._discover_field_spec(first)
        if not field_spec:
            return self.read()

        # 1-bit fields are stored as ``bool``; everything else as ``uint64``.
        dtype = np.dtype(
            [(name, np.bool_ if w == 1 else np.uint64) for name, (_lsb, w) in field_spec.items()]
        )

        result = np.empty(self._shape, dtype=dtype)
        flat = result.reshape(-1)
        for i, el in enumerate(elements):
            value = el.read() if hasattr(el, "read") else el
            iv = int(value)
            row = tuple(
                (iv >> lsb) & ((1 << width) - 1) for lsb, width in field_spec.values()
            )
            flat[i] = row
        return result

    def _discover_field_spec(self, element: Any) -> dict[str, tuple[int, int]]:
        """Best-effort lookup of ``{name: (lsb, width)}`` for each field.

        The generated runtime template registers a module-level
        ``_REGISTER_FIELDS`` map keyed by class. We try that first; if
        the element exposes a ``_fields`` mapping (a ``RegisterInt``
        returned by ``read``), we synthesize the spec from that.
        """
        cls = type(element)
        module_name = getattr(cls, "__module__", None)
        mod = sys.modules.get(module_name) if module_name else None
        spec_map = getattr(mod, "_REGISTER_FIELDS", None) if mod else None
        if spec_map and cls in spec_map:
            return dict(spec_map[cls])
        fields_attr = getattr(element, "_fields", None)
        if isinstance(fields_attr, dict) and fields_attr:
            return {k: (v.lsb, v.width) for k, v in fields_attr.items()}
        return {}

    def __setitem__(self, key: Any, value: Any) -> None:
        target = self if key == slice(None) else self[key]
        if not isinstance(target, ArrayView):
            target.write(int(value))
            return

        elements = list(target._iter_elements())
        if not elements:
            return
        values = self._broadcast_values(value, len(elements))

        master = _master_for(elements)
        if master is not None and hasattr(master, "write_many"):
            ops = [
                _make_access_op(el, value=int(v))
                for el, v in zip(elements, values, strict=True)
            ]
            if all(op is not None for op in ops):
                master.write_many(ops)
                return

        for el, v in zip(elements, values, strict=True):
            el.write(int(v))

    # ---- modify ---------------------------------------------------------

    def modify(self, **fields: Any) -> None:
        """Issue ``N`` element-level RMWs (one per element).

        The sketch's note about a "burst-RMW" remains a backend
        capability negotiation; until masters expose it, we explicitly
        do ``N`` RMWs and document the cost.
        """
        for el in self._iter_elements():
            el.modify(**fields)


# ---------------------------------------------------------------------------
# FieldArray
# ---------------------------------------------------------------------------


class FieldArray(ArrayView):
    """Specialized view for arrays of *fields* (e.g. ``mode[16]``).

    Inherits all slicing / iteration semantics from :class:`ArrayView`.
    The override here only differs in the default dtype (``bool`` for
    1-bit fields). Multi-bit field arrays still come back as
    ``uint64``.
    """

    def read(self) -> np.ndarray:
        elements = list(self._iter_elements())
        if not elements:
            return np.empty(0, dtype=bool)
        # Trust per-element ``read()`` -- a FieldInt of width 1 is bool;
        # of width >1 is unsigned int. Mirror in the dtype.
        sample = elements[0].read() if hasattr(elements[0], "read") else elements[0]
        width = getattr(elements[0], "width", None)
        if width == 1 or isinstance(sample, (bool, np.bool_)):
            arr = np.array([bool(int(el.read())) for el in elements], dtype=bool)
        else:
            arr = np.array([int(el.read()) for el in elements], dtype=np.uint64)
        return arr.reshape(self._shape)


# ---------------------------------------------------------------------------
# Wiring hook
# ---------------------------------------------------------------------------

# Sibling unit `_registry` (Unit 1) calls back into this module to wrap
# array-typed members on register / regfile classes. We keep the hook
# self-contained so the module is usable without the registry.

_REGISTERED_HOOKS: list[Callable[[type, str, Sequence[Any], tuple[int, ...]], Any]] = []


def register_register_enhancement(
    hook: Callable[[type, str, Sequence[Any], tuple[int, ...]], Any] | None = None,
    *,
    array_view_cls: type[ArrayView] | None = None,
) -> Callable[[type, str, Sequence[Any], tuple[int, ...]], Any]:
    """Register an enhancement hook for register-class array members.

    Two usage modes:

    1. ``register_register_enhancement(my_hook)`` adds ``my_hook`` to a
       module-level chain. Each hook is called as
       ``hook(parent_cls, member_name, elements, shape)`` and may
       return an :class:`ArrayView`-compatible wrapper (or anything;
       the chain stops at the first non-``None`` result).

    2. ``register_register_enhancement(array_view_cls=ArrayView)`` (the
       default) returns the canonical wrapper -- a hook that wraps
       any sequence of generated descriptors with the given view
       class.

    The function is intentionally idempotent: registering the same
    hook twice is a no-op. This lets the generated runtime emit
    ``register_register_enhancement(_array_hook)`` at import time
    without worrying about double-registration on hot reload.
    """
    if hook is None:
        # Cache one default-hook closure per ``array_view_cls`` so repeat
        # ``register_register_enhancement()`` calls really are idempotent
        # (the closure carries the class identity but a fresh ``def``
        # would be a different object on each call).
        cls = array_view_cls or ArrayView
        hook = _default_hook_for(cls)

    if hook not in _REGISTERED_HOOKS:
        _REGISTERED_HOOKS.append(hook)
    return hook


_DEFAULT_HOOKS: dict[type, Callable[[type, str, Sequence[Any], tuple[int, ...]], Any]] = {}


def _default_hook_for(
    cls: type[ArrayView],
) -> Callable[[type, str, Sequence[Any], tuple[int, ...]], Any]:
    cached = _DEFAULT_HOOKS.get(cls)
    if cached is not None:
        return cached

    def default_hook(
        parent_cls: type,
        member_name: str,
        elements: Sequence[Any],
        shape: tuple[int, ...],
    ) -> ArrayView:
        return cls(elements, shape)

    _DEFAULT_HOOKS[cls] = default_hook
    return default_hook


def wrap_array(
    parent_cls: type,
    member_name: str,
    elements: Sequence[Any],
    shape: tuple[int, ...] | None = None,
) -> ArrayView:
    """Apply registered hooks to wrap a sequence of descriptors.

    The first hook to return a non-``None`` value wins. If no hook is
    registered we fall back to a vanilla :class:`ArrayView`.
    """
    elements_list = list(elements)
    eff_shape: tuple[int, ...] = tuple(shape) if shape is not None else (len(elements_list),)
    for hook in _REGISTERED_HOOKS:
        result = hook(parent_cls, member_name, elements_list, eff_shape)
        if result is not None:
            return result
    return ArrayView(elements_list, eff_shape)


def reset_enhancements() -> None:
    """Clear all registered enhancement hooks. Intended for tests."""
    _REGISTERED_HOOKS.clear()
