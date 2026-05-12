"""
Default register/field enhancements.

The bulk of what the generated ``runtime.py`` module used to do inline
lives here. Lifting it into a module that auto-registers means sibling
units of the API overhaul can simply *also* register more enhancements
and they will compose: defaults run first (they wrap the bare C++
``read``/``write`` into Python shims), then sibling-unit enhancements
layer on additional behaviour.

This module is the **seam where the new API takes effect**: register
reads return :class:`peakrdl_pybind11.runtime.values.RegisterValue` and
field reads return :class:`peakrdl_pybind11.runtime.values.FieldValue`
(both immutable, hashable ``int`` subclasses with ``.hex()``, ``.bin()``,
``.replace(**fields)``, ``.table()`` etc.). The legacy
``RegisterInt`` / ``FieldInt`` types in :mod:`peakrdl_pybind11.int_types`
remain importable for code that constructs them directly, but the shim
no longer emits them. See ``docs/IDEAL_API_SKETCH.md`` §3.2.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from . import _registry
from .errors import AccessError
from .values import FieldValue, RegisterValue, _normalize_field_meta

logger = logging.getLogger("peakrdl_pybind11.runtime.default_shims")

# Sentinel attribute placed on enhanced read/write callables so we never
# wrap twice. Mirrors the ``__peakrdl_enhanced__`` marker used in the
# original template.
_ENHANCED = "__peakrdl_enhanced__"


def _field_access_mode(readable: bool, writable: bool) -> str:
    """Map (readable, writable) booleans onto the canonical sw= token.

    Used to build the ``access_mode`` argument for :class:`AccessError` so
    the default error message — ``"<path> is sw=<mode>"`` — reflects the
    actual access mode of the field that rejected the operation.
    """
    if readable and writable:
        return "rw"
    if readable:
        return "r"
    if writable:
        return "w"
    return "na"


def _field_node_path(field: Any) -> str:
    """Best-effort path for a field instance, used in error messages.

    Falls back to the bare ``name`` attribute (always present on generated
    fields) when no richer path is available.
    """
    info = getattr(field, "info", None)
    if info is not None:
        path = getattr(info, "path", None)
        if isinstance(path, str) and path:
            return path
    name = getattr(field, "name", None)
    if isinstance(name, str) and name:
        return name
    return "<field>"


def _enhanced_register_read(
    original_read: Callable[..., int],
    flag_type: type | None,
    enum_type: type | None,
    fields_spec: dict[str, tuple[int, int]],
) -> Callable[..., Any]:
    """Wrap the C++ register read with the typed/raw kwarg dispatch.

    ``reg.read()``         → ``RegisterValue`` (or flag/enum subclass)
    ``reg.read(raw=True)`` → plain ``int`` from the C++ binding

    ``raw`` is keyword-only so the call site reads as ``read(raw=True)`` and
    can never collide with a positional argument in the future.

    ``fields_spec`` is normalized **once** at class-attach time and the
    pre-normalized dict is reused on every read. That moves the hot
    ``reg.read()`` path from ~2 us to ~0.9 us on multi-field registers
    because ``_normalize_field_meta`` was 70% of the construction cost.
    """

    fields_normalized = _normalize_field_meta(fields_spec)

    def read(self: Any, *, raw: bool = False) -> Any:
        value = original_read(self)
        if raw:
            return value
        if flag_type is not None:
            return flag_type(value)
        if enum_type is not None:
            return enum_type(value)
        # ``self.width`` from the C++ ``RegisterBase`` is in bytes; the
        # ``RegisterValue`` constructor masks against ``(1 << width) - 1``
        # which expects bits. Multiply once at the seam.
        return RegisterValue(
            value,
            address=self.offset,
            width=self.width * 8,
            fields_normalized=fields_normalized,
            name=getattr(self, "name", None),
        )

    setattr(read, _ENHANCED, True)
    return read


def _enhanced_register_write(
    original_write: Callable[[Any, int], None],
    original_modify: Callable[[Any, int, int], None],
) -> Callable[..., None]:
    """Wrap the C++ register write with the FieldValue/raw kwarg dispatch.

    ``reg.write(int)``                       → C++ write (plain value)
    ``reg.write(FieldValue)``                → C++ modify (shifted + masked)
    ``reg.write(int, raw=True)``             → C++ write, no FieldValue check
    """

    def write(self: Any, value: Any, *, raw: bool = False) -> None:
        if raw:
            original_write(self, int(value))
            return
        if isinstance(value, FieldValue):
            shifted = (int(value) << value.lsb) & value.mask
            original_modify(self, shifted, value.mask)
        else:
            original_write(self, int(value))

    setattr(write, _ENHANCED, True)
    return write


def _enhanced_field_read(original_read: Callable[..., int]) -> Callable[..., Any]:
    """Wrap the C++ field read with the typed/raw kwarg dispatch.

    ``field.read()``         → ``FieldValue``
    ``field.read(raw=True)`` → plain ``int`` from the C++ binding

    Reads on write-only fields (``is_readable == False``) raise
    :class:`AccessError` before touching the bus, including on the
    ``raw=True`` fast path. Missing ``is_readable`` defaults to ``True``
    so unannotated mocks remain back-compatible.
    """

    def read(self: Any, *, raw: bool = False) -> Any:
        # Gate BEFORE calling ``original_read`` so the raw fast path is
        # also blocked — otherwise the side effect of the bus read would
        # leak past the access check.
        readable = getattr(self, "is_readable", True)
        if not readable:
            writable = getattr(self, "is_writable", True)
            raise AccessError(
                _field_node_path(self),
                _field_access_mode(readable, writable),
            )
        value = original_read(self)
        if raw:
            return value
        # Field ``width`` from C++ ``FieldBase`` is in bits — feed it to
        # ``FieldValue`` directly. ``offset`` is the parent register's
        # address; we surface it as ``register_path`` for diagnostics.
        return FieldValue(
            value,
            lsb=self.lsb,
            width=self.width,
            name=getattr(self, "name", None),
        )

    setattr(read, _ENHANCED, True)
    return read


def _enhanced_field_write(original_write: Callable[[Any, int], None]) -> Callable[..., None]:
    """Field write only has one shape; ``raw`` is accepted for parity.

    Writes on read-only fields (``is_writable == False``) raise
    :class:`AccessError` before touching the bus, including on the
    ``raw=True`` fast path. Missing ``is_writable`` defaults to ``True``
    so unannotated mocks remain back-compatible.
    """

    def write(self: Any, value: Any, *, raw: bool = False) -> None:
        # Gate BEFORE calling ``original_write`` so ``raw=True`` does not
        # leak past the access check.
        writable = getattr(self, "is_writable", True)
        if not writable:
            readable = getattr(self, "is_readable", True)
            raise AccessError(
                _field_node_path(self),
                _field_access_mode(readable, writable),
            )
        # ``raw`` is signature parity with the register write — for fields
        # the path is identical either way (no FieldValue dispatch).
        original_write(self, int(value))

    setattr(write, _ENHANCED, True)
    return write


def _make_write_fields(
    fields_spec: dict[str, tuple[int, int]],
    writable_spec: dict[str, bool],
    readable_spec: dict[str, bool] | None = None,
) -> Callable[..., None]:
    """Build a ``write_fields(**kwargs)`` shim for a generated register class.

    Collapses N per-field writes into a single C++ ``write_fields(mask,
    value)`` call (1 read + 1 write on the master, regardless of N).
    Validates field names and writability at the Python boundary so the
    C++ side stays minimal. Writability failures surface as
    :class:`AccessError` for consistency with the per-field
    ``field.write()`` access gate.

    ``readable_spec`` is accepted (and unused on this path) so callers can
    pass through the full readable/writable metadata pair without
    surprises; per-field read gating happens in :func:`_enhanced_field_read`.
    """

    _ = readable_spec  # unused on the write path; kept in the signature for symmetry.

    def write_fields(self: Any, **kwargs: Any) -> None:
        combined_mask = 0
        combined_value = 0
        for name, raw_value in kwargs.items():
            spec = fields_spec.get(name)
            if spec is None:
                raise KeyError(
                    f"Unknown field '{name}' on register '{self.name}'. Known fields: {sorted(fields_spec)}"
                )
            if not writable_spec.get(name, False):
                # Build a node_path consistent with per-field errors:
                # ``<register_name>.<field_name>``. ``access_mode`` is
                # ``"r"`` when the field is at least readable, ``"na"``
                # otherwise — the readable_spec is the only signal we
                # have at this layer.
                read_ok = True if readable_spec is None else readable_spec.get(name, True)
                raise AccessError(
                    f"{self.name}.{name}",
                    _field_access_mode(read_ok, False),
                )
            lsb, width = spec
            field_mask = ((1 << width) - 1) << lsb
            combined_mask |= field_mask
            combined_value |= (int(raw_value) << lsb) & field_mask
        # Single C++ entry: native RMW under the hood.
        self._native_write_fields(combined_mask, combined_value)

    return write_fields


@_registry.register_register_enhancement
def _default_register_shim(cls: type, metadata: dict) -> None:
    """Wrap the generated register class with typed read/write/write_fields.

    ``metadata`` is the dict the generated runtime passes in. The keys we
    care about:

    * ``"fields"``      — ``{field_name: (lsb, width)}``
    * ``"writable"``    — ``{field_name: bool}``
    * ``"readable"``    — ``{field_name: bool}``  (optional; missing
      entries default to ``True`` for back-compat with templates that
      don't yet emit this key)
    * ``"flag_type"``   — optional flag class for this register
    * ``"enum_type"``   — optional enum class for this register

    Sibling units may add more keys; we ignore them silently.

    If ``cls`` doesn't expose ``read``/``write`` (e.g. a unit test passes
    a stub class) we bail cleanly — the seam is generic, but the default
    shim only knows how to handle generated register classes.

    Per-field access-mode enforcement (sw=r read-only, sw=w write-only)
    is implemented in :func:`_enhanced_field_read` and
    :func:`_enhanced_field_write`, which gate on the C++-exposed
    ``is_readable``/``is_writable`` instance attributes of each field.
    Missing attributes default to allowing the access — keeps Python
    stubs and pre-enforcement bindings working unchanged.
    """
    raw_read = getattr(cls, "read", None)
    if raw_read is None:
        return
    if getattr(raw_read, _ENHANCED, False):
        return  # already enhanced (e.g. importing the module twice)

    fields_spec: dict[str, tuple[int, int]] = metadata.get("fields", {})
    writable_spec: dict[str, bool] = metadata.get("writable", {})
    # ``readable`` is optional metadata. When the template doesn't emit
    # it, ``readable_spec`` is empty and ``write_fields`` treats every
    # field as readable; per-field read gating still works because it
    # consults the field instance's ``is_readable`` attribute directly.
    readable_spec: dict[str, bool] = metadata.get("readable", {})
    flag_type: type | None = metadata.get("flag_type")
    enum_type: type | None = metadata.get("enum_type")

    raw_write = getattr(cls, "write", None)
    if raw_write is None:
        return
    # Stash the full metadata on the class so the ``.info`` factory and
    # other sibling units can access fields like ``address`` / ``path`` /
    # ``regwidth`` without re-parsing the RDL.
    cls.__peakrdl_meta__ = dict(metadata)  # type: ignore[attr-defined]
    cls.read = _enhanced_register_read(raw_read, flag_type, enum_type, fields_spec)  # type: ignore[method-assign]
    cls.write = _enhanced_register_write(raw_write, cls.modify)  # type: ignore[method-assign]
    # ``poke(v)`` is the explicit "I know what I'm doing" alias for write —
    # documented in sketch §3.1. Symmetric to write but signals user intent.
    cls.poke = cls.write  # type: ignore[attr-defined]
    # Preserve the native binding under a private name; expose the Python
    # shim with validation under the public name.
    if hasattr(cls, "write_fields"):
        cls._native_write_fields = cls.write_fields  # type: ignore[attr-defined]
        write_fields_shim = _make_write_fields(fields_spec, writable_spec, readable_spec)
        cls.write_fields = write_fields_shim  # type: ignore[method-assign]
        # ``reg.modify(**fields)`` is the canonical aspirational API
        # (sketch §3.3). The C++ ``modify(value, mask)`` is preserved
        # under ``_native_modify`` for the RMW machinery; the Python
        # ``modify`` accepts EITHER ``(value, mask)`` positional args
        # (legacy) OR ``**fields`` kwargs (the canonical surface).
        native_modify = cls.modify  # type: ignore[attr-defined]
        cls._native_modify = native_modify  # type: ignore[attr-defined]

        def modify(self: Any, *args: Any, **kwargs: Any) -> None:
            if kwargs and not args:
                write_fields_shim(self, **kwargs)
                return
            if args and not kwargs:
                native_modify(self, *args)
                return
            raise TypeError(
                "modify() takes either (value, mask) positional args or **fields kwargs, "
                f"not both. Got args={args!r}, kwargs={kwargs!r}"
            )

        cls.modify = modify  # type: ignore[method-assign]


@_registry.register_field_enhancement
def _default_field_shim(cls: type) -> None:
    """Wrap the generated field class with typed read/write.

    Bails cleanly on classes that don't expose ``read``/``write`` so the
    seam stays generic enough to test in isolation.
    """
    raw_read = getattr(cls, "read", None)
    if raw_read is None:
        return
    if getattr(raw_read, _ENHANCED, False):
        return

    raw_write = getattr(cls, "write", None)
    if raw_write is None:
        return
    cls.read = _enhanced_field_read(raw_read)  # type: ignore[method-assign]
    cls.write = _enhanced_field_write(raw_write)  # type: ignore[method-assign]
