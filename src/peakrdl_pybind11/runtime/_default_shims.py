"""
Default register/field enhancements.

The bulk of what the generated ``runtime.py`` module used to do inline
lives here. Lifting it into a module that auto-registers means sibling
units of the API overhaul can simply *also* register more enhancements
and they will compose: defaults run first (they wrap the bare C++
``read``/``write`` into Python shims), then sibling-unit enhancements
layer on additional behaviour.

The semantics of the default shims are intentionally identical to the
pre-overhaul template, so the existing ``tests/test_*`` integration suite
continues to pass.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..int_types import FieldInt, RegisterInt
from . import _registry

logger = logging.getLogger("peakrdl_pybind11.runtime.default_shims")

# Sentinel attribute placed on enhanced read/write callables so we never
# wrap twice. Mirrors the ``__peakrdl_enhanced__`` marker used in the
# original template.
_ENHANCED = "__peakrdl_enhanced__"


def _enhanced_register_read(
    original_read: Callable[..., int],
    flag_type: type | None,
    enum_type: type | None,
    fields_spec: dict[str, tuple[int, int]],
) -> Callable[[Any], Any]:
    def read(self: Any) -> Any:
        value = original_read(self)
        if flag_type is not None:
            return flag_type(value)
        if enum_type is not None:
            return enum_type(value)
        return RegisterInt(value, self.offset, self.width, fields_spec)

    setattr(read, _ENHANCED, True)
    return read


def _enhanced_register_write(
    original_write: Callable[[Any, int], None],
    original_modify: Callable[[Any, int, int], None],
) -> Callable[[Any, Any], None]:
    def write(self: Any, value: Any) -> None:
        if isinstance(value, FieldInt):
            shifted = (int(value) << value.lsb) & value.mask
            original_modify(self, shifted, value.mask)
        else:
            original_write(self, int(value))

    setattr(write, _ENHANCED, True)
    return write


def _enhanced_field_read(original_read: Callable[..., int]) -> Callable[[Any], FieldInt]:
    def read(self: Any) -> FieldInt:
        return FieldInt(original_read(self), self.lsb, self.width, self.offset)

    setattr(read, _ENHANCED, True)
    return read


def _enhanced_field_write(original_write: Callable[[Any, int], None]) -> Callable[[Any, Any], None]:
    def write(self: Any, value: Any) -> None:
        original_write(self, int(value))

    setattr(write, _ENHANCED, True)
    return write


def _make_write_fields(
    fields_spec: dict[str, tuple[int, int]],
    writable_spec: dict[str, bool],
) -> Callable[..., None]:
    """Build a ``write_fields(**kwargs)`` shim for a generated register class.

    Collapses N per-field writes into a single C++ ``write_fields(mask,
    value)`` call (1 read + 1 write on the master, regardless of N).
    Validates field names and writability at the Python boundary so the
    C++ side stays minimal.
    """

    def write_fields(self: Any, **kwargs: Any) -> None:
        combined_mask = 0
        combined_value = 0
        for name, raw_value in kwargs.items():
            spec = fields_spec.get(name)
            if spec is None:
                raise KeyError(
                    f"Unknown field '{name}' on register '{self.name}'. "
                    f"Known fields: {sorted(fields_spec)}"
                )
            if not writable_spec.get(name, False):
                raise PermissionError(f"Field '{name}' on register '{self.name}' is not writable")
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
    * ``"flag_type"``   — optional flag class for this register
    * ``"enum_type"``   — optional enum class for this register

    Sibling units may add more keys; we ignore them silently.

    If ``cls`` doesn't expose ``read``/``write`` (e.g. a unit test passes
    a stub class) we bail cleanly — the seam is generic, but the default
    shim only knows how to handle generated register classes.
    """
    raw_read = getattr(cls, "read", None)
    if raw_read is None:
        return
    if getattr(raw_read, _ENHANCED, False):
        return  # already enhanced (e.g. importing the module twice)

    fields_spec: dict[str, tuple[int, int]] = metadata.get("fields", {})
    writable_spec: dict[str, bool] = metadata.get("writable", {})
    flag_type: type | None = metadata.get("flag_type")
    enum_type: type | None = metadata.get("enum_type")

    raw_write = getattr(cls, "write", None)
    if raw_write is None:
        return
    cls.read = _enhanced_register_read(raw_read, flag_type, enum_type, fields_spec)  # type: ignore[method-assign]
    cls.write = _enhanced_register_write(raw_write, cls.modify)  # type: ignore[method-assign]
    # Fast-path scalar accessors: bypass RegisterInt wrapping / FieldInt
    # handling. ``read_raw`` returns a plain ``int``; ``write_raw`` accepts
    # a plain ``int`` and writes it directly via the underlying C++ method.
    cls.read_raw = raw_read  # type: ignore[attr-defined]
    cls.write_raw = raw_write  # type: ignore[attr-defined]
    # Preserve the native binding under a private name; expose the Python
    # shim with validation under the public name.
    if hasattr(cls, "write_fields"):
        cls._native_write_fields = cls.write_fields  # type: ignore[attr-defined]
        cls.write_fields = _make_write_fields(fields_spec, writable_spec)  # type: ignore[method-assign]


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
    # Fast-path scalar accessors: ``read_raw`` returns a plain ``int`` (the
    # field-space value, identical to what the C++ getter returns -- no
    # ``FieldInt`` allocation). ``write_raw`` takes a plain ``int`` in
    # field-space and forwards directly to the C++ setter (which performs
    # the shift+mask+modify on the underlying register).
    cls.read_raw = raw_read  # type: ignore[attr-defined]
    cls.write_raw = raw_write  # type: ignore[attr-defined]
