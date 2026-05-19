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
from enum import IntEnum
from typing import Any

from . import _registry
from .errors import AccessError, did_you_mean
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


def _enhanced_field_read(
    original_read: Callable[..., int],
    encode_type: type[IntEnum] | None = None,
) -> Callable[..., Any]:
    """Wrap the C++ field read with the typed/raw kwarg dispatch.

    ``field.read()``         → ``FieldValue`` (decodes to ``encode_type`` when set)
    ``field.read(raw=True)`` → plain ``int`` from the C++ binding

    Reads on write-only fields (``is_readable == False``) raise
    :class:`AccessError` before touching the bus, including on the
    ``raw=True`` fast path. Missing ``is_readable`` defaults to ``True``
    so unannotated mocks remain back-compatible.

    ``encode_type`` is the per-field RDL ``encode`` IntEnum class (sketch
    §8.1). When set, the returned :class:`FieldValue` carries the class so
    ``.decoded()`` and ``__repr__`` surface enum-member names; otherwise
    the FieldValue is plain.
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
            encode=encode_type,
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
                # Per sketch §19 matrix: an unknown field name passed to
                # ``modify(**kwargs)`` (which routes through this shim)
                # surfaces as ``AttributeError`` with a did-you-mean
                # suggestion — mirrors attribute-style access elsewhere
                # in the API and is the canonical "you typo'd a name"
                # exception. The legacy ``KeyError`` path lives only on
                # ``RegisterValue.replace`` (subscript-style mutation).
                suggestion = did_you_mean(name, fields_spec.keys())
                hint = f"; did you mean {suggestion!r}?" if suggestion else ""
                raise AttributeError(
                    f"Unknown field {name!r} on register {self.name!r}. "
                    f"Known fields: {sorted(fields_spec)}{hint}"
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

    # Install the ``strict_fields=False`` attribute-assignment shim.
    # Issue #140 Gap 3: when the generated module is built with
    # ``--strict-fields=false`` the bare ``reg.field = value`` idiom must
    # fire a per-assignment :class:`DeprecationWarning` and dispatch
    # through ``modify(field=value)`` (sketch §22.8). Under the default
    # strict mode (the flag is True or absent) the shim is a thin
    # passthrough that delegates straight to ``object.__setattr__`` so we
    # don't pay any extra cost on the hot path.
    _install_setattr_shim(cls, fields_spec)


def _resolve_strict_fields(cls: type) -> bool:
    """Return the effective ``_PEAKRDL_STRICT_FIELDS`` flag for ``cls``.

    The generated runtime emits the flag on the Python wrapper module
    (e.g. ``loose_check``), but ``cls.__module__`` resolves to the *native*
    pybind11 module (e.g. ``loose_check._loose_check_native``). Walk
    upward through dotted parent modules and return the first
    ``_PEAKRDL_STRICT_FIELDS`` we find, defaulting to ``True`` (strict)
    when no ancestor exposes the flag. This keeps non-generated stub
    classes from accidentally entering the loose path.
    """
    import sys

    module_path = getattr(cls, "__module__", "") or ""
    if not module_path:
        return True
    parts = module_path.split(".")
    while parts:
        candidate = ".".join(parts)
        mod = sys.modules.get(candidate)
        if mod is not None:
            flag = getattr(mod, "_PEAKRDL_STRICT_FIELDS", None)
            if isinstance(flag, bool):
                return flag
        parts.pop()
    return True


def _install_setattr_shim(cls: type, fields_spec: dict[str, tuple[int, int]]) -> None:
    """Install a ``__setattr__`` that honours ``--strict-fields=false``.

    The shim does nothing different in strict mode — but in loose mode it
    intercepts assignments whose name matches a known field, fires a
    :class:`DeprecationWarning`, and routes through ``modify(**{name:
    value})`` so the legacy "attribute-assign = RMW" idiom keeps working
    on a porting build (issue #140 Gap 3).

    The strict-fields flag lives on the generated module
    (``_PEAKRDL_STRICT_FIELDS``); ``_resolve_strict_fields`` walks the
    dotted module path so the lookup happens at call time and picks up
    the value the generated Python wrapper loaded, even though pybind11
    reports the *native* module on the class.
    """
    import warnings

    field_names = frozenset(fields_spec)
    # Bail when the class can't accept method assignment (e.g. a slotted
    # stub used in unit tests). The shim isn't useful there anyway.
    try:
        original_setattr = cls.__setattr__  # type: ignore[attr-defined]
    except AttributeError:
        return

    def _peakrdl_setattr(self: Any, name: str, value: Any) -> None:
        # Fast-path: anything starting with ``_``, a known method, or
        # an attribute that's not in the field set — fall through.
        if name in field_names:
            strict = _resolve_strict_fields(type(self))
            if not strict:
                # Resolve the path for the warning so the user can locate
                # the offending line without spelunking the SoC.
                reg_path = getattr(self, "name", None) or type(self).__name__
                warnings.warn(
                    f"Bare field assignment {reg_path!r}.{name} = {value!r} is "
                    "deprecated and falls back to modify(...); use "
                    f"{reg_path!r}.modify({name}=...) explicitly. See "
                    "IDEAL_API_SKETCH.md §22.8.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                modify = getattr(self, "modify", None)
                if callable(modify):
                    modify(**{name: value})
                    return
        original_setattr(self, name, value)  # pyrefly: ignore[bad-argument-count]

    try:
        cls.__setattr__ = _peakrdl_setattr  # type: ignore[assignment,method-assign]
    except (AttributeError, TypeError):
        # Slotted / built-in classes that don't accept attribute
        # injection: leave the original ``__setattr__`` in place. The
        # ``strict_fields=False`` shim is a best-effort feature; the
        # rest of the API still works.
        return


@_registry.register_field_enhancement
def _default_field_shim(cls: type, metadata: dict | None = None) -> None:
    """Wrap the generated field class with typed read/write.

    ``metadata`` is the optional per-field metadata dict the registry
    threads through ``apply_field_enhancements`` (sketch §8.1). Today the
    only key we honor is ``"encode"`` — an :class:`enum.IntEnum` subclass
    produced by the RDL ``encode`` property. When present, the encode
    class is attached to every :class:`FieldValue` returned by
    ``field.read()`` and surfaced as ``cls.choices``.

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

    encode_type: type[IntEnum] | None = None
    if metadata:
        candidate = metadata.get("encode")
        if isinstance(candidate, type) and issubclass(candidate, IntEnum):
            encode_type = candidate
        # Stash field-level metadata on the class so the default ``.info``
        # factory can build an :class:`Info` carrying the RDL side-effect
        # tokens (``on_read`` / ``on_write`` / ``singlepulse``) plus
        # ``lsb`` / ``field_width`` / ``path`` / ``name``. Issue #140 Gap 1.
        # Use ``setdefault`` semantics so a re-apply doesn't clobber a
        # richer meta dict left by a sibling unit.
        existing_meta = getattr(cls, "__peakrdl_meta__", None)
        if not isinstance(existing_meta, dict):
            cls.__peakrdl_meta__ = dict(metadata)  # type: ignore[attr-defined]

    cls.read = _enhanced_field_read(raw_read, encode_type)  # type: ignore[method-assign]
    cls.write = _enhanced_field_write(raw_write)  # type: ignore[method-assign]
    if encode_type is not None:
        # ``field.choices`` surfaces the list of enum members so users can
        # iterate / display valid encodings. Per task spec we expose the
        # member *list* (not the type) — diverges from API sketch §8.1
        # which describes the type itself; documented in the unit's report.
        cls.choices = list(encode_type)  # type: ignore[attr-defined]
