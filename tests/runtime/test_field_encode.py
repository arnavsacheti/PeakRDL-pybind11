"""Tests for RDL ``encode`` -> IntEnum plumbing (sketch §8.1).

These tests drive ``_default_field_shim`` and ``_default_register_shim``
directly with mock metadata so the encode path can be exercised without
compiling any generated C++. The seam contract is:

* Generated runtime emits per-field ``IntEnum`` classes and threads the
  encode type through ``apply_field_enhancements(cls, {"encode": <cls>})``.
* The shim wraps ``field.read()`` so it returns a :class:`FieldValue`
  carrying the encode class (``.decoded()`` returns enum members).
* The shim stamps ``field.choices`` = ``list(encode_class)``.
* Existing register-level ``is_enum`` / ``is_flag`` flow keeps working
  because that path runs through ``apply_register_enhancements`` on a
  different metadata key (``"enum_type"`` / ``"flag_type"``).
"""

from __future__ import annotations

from enum import IntEnum, IntFlag
from typing import Any

import pytest

from peakrdl_pybind11.runtime import _registry
from peakrdl_pybind11.runtime._default_shims import (
    _default_field_shim,
    _default_register_shim,
)
from peakrdl_pybind11.runtime.values import FieldValue


# ---------------------------------------------------------------------------
# Fixtures: mock field/register classes shaped like the generated bindings.
# ---------------------------------------------------------------------------


class _BaudRate(IntEnum):
    """Stand-in for an RDL ``encode = baud_e { ... };`` enum."""

    BAUD_9600 = 0
    BAUD_115200 = 1
    BAUD_AUTO = 2


def _make_field_class(*, value: int = 0, width: int = 3, lsb: int = 0) -> type:
    """Build a fresh field class that records its read/write traffic.

    Fresh per test because ``_default_field_shim`` mutates the class
    in-place; sharing would cross-contaminate tests.
    """
    calls: dict[str, list[Any]] = {"reads": [], "writes": []}

    class _FieldCls:
        # Generated bindings expose these as instance attributes; we put
        # them on the class for simplicity (they're constants per-field).
        is_readable = True
        is_writable = True
        lsb = 0
        width = 1
        name = "baud_rate"

        def read(self) -> int:
            calls["reads"].append(None)
            return self._value

        def write(self, v: int) -> None:
            calls["writes"].append(int(v))
            self._value = int(v)

    _FieldCls.lsb = lsb
    _FieldCls.width = width
    _FieldCls._test_calls = calls  # type: ignore[attr-defined]

    instance = _FieldCls()
    instance._value = value  # type: ignore[attr-defined]
    _FieldCls._test_instance = instance  # type: ignore[attr-defined]
    return _FieldCls


# ---------------------------------------------------------------------------
# Tests: field-level encode behaviour.
# ---------------------------------------------------------------------------


def test_field_with_encode_attaches_class_to_fieldvalue() -> None:
    """``field.read()`` returns a FieldValue whose ``.encode`` is the IntEnum."""
    cls = _make_field_class(value=1)
    _default_field_shim(cls, {"encode": _BaudRate})

    instance = cls._test_instance  # type: ignore[attr-defined]
    result = instance.read()

    assert isinstance(result, FieldValue)
    assert result.encode is _BaudRate
    assert int(result) == 1
    # ``decoded()`` returns the enum member, not the raw int.
    decoded = result.decoded()
    assert decoded is _BaudRate.BAUD_115200


def test_field_without_encode_gets_plain_fieldvalue() -> None:
    """Backwards compatibility: no encode -> FieldValue with ``encode=None``."""
    cls = _make_field_class(value=2)
    # No metadata at all — matches the legacy single-arg path.
    _default_field_shim(cls)

    instance = cls._test_instance  # type: ignore[attr-defined]
    result = instance.read()

    assert isinstance(result, FieldValue)
    assert result.encode is None
    assert int(result) == 2


def test_field_with_empty_metadata_gets_plain_fieldvalue() -> None:
    """Empty metadata dict behaves the same as no metadata at all."""
    cls = _make_field_class(value=2)
    _default_field_shim(cls, {})

    instance = cls._test_instance  # type: ignore[attr-defined]
    result = instance.read()

    assert isinstance(result, FieldValue)
    assert result.encode is None


def test_field_write_accepts_enum_member() -> None:
    """IntEnum members are int subclasses, so write must accept them directly."""
    cls = _make_field_class(value=0)
    _default_field_shim(cls, {"encode": _BaudRate})

    instance = cls._test_instance  # type: ignore[attr-defined]
    instance.write(_BaudRate.BAUD_115200)

    assert cls._test_calls["writes"] == [int(_BaudRate.BAUD_115200)]  # type: ignore[attr-defined]
    # And the underlying value reads back as the same int.
    assert int(instance.read()) == int(_BaudRate.BAUD_115200)


def test_field_choices_is_enum_member_list() -> None:
    """``field.choices`` exposes the list of IntEnum members for IDE completion."""
    cls = _make_field_class()
    _default_field_shim(cls, {"encode": _BaudRate})

    assert cls.choices == list(_BaudRate)  # type: ignore[attr-defined]
    # Sanity: it's a list, not the type itself.
    assert isinstance(cls.choices, list)  # type: ignore[attr-defined]


def test_field_without_encode_has_no_choices_attribute() -> None:
    """Fields without ``encode`` do not get ``choices`` stamped on the class."""
    cls = _make_field_class()
    _default_field_shim(cls)

    assert not hasattr(cls, "choices")


def test_field_raw_read_skips_encode_wrap() -> None:
    """``raw=True`` returns the plain int regardless of encode metadata."""
    cls = _make_field_class(value=2)
    _default_field_shim(cls, {"encode": _BaudRate})

    instance = cls._test_instance  # type: ignore[attr-defined]
    result = instance.read(raw=True)

    assert isinstance(result, int) and not isinstance(result, FieldValue)
    assert result == 2


def test_field_repr_shows_enum_member() -> None:
    """FieldValue ``__repr__`` decodes when an encode is attached (smoke)."""
    cls = _make_field_class(value=0)
    _default_field_shim(cls, {"encode": _BaudRate})

    instance = cls._test_instance  # type: ignore[attr-defined]
    result = instance.read()

    rendered = repr(result)
    # Format defined in ``runtime/values.py::FieldValue.__repr__``.
    assert "BAUD_9600" in rendered


# ---------------------------------------------------------------------------
# Backward-compat: existing register-level is_enum / is_flag still works.
# ---------------------------------------------------------------------------


class _CtrlRegEnum(IntEnum):
    """Register-level enum (RDL ``is_enum`` UDP)."""

    IDLE = 0
    RUNNING = 1
    HALT = 2


class _CtrlRegFlags(IntFlag):
    """Register-level flags (RDL ``is_flag`` UDP)."""

    READY = 1
    BUSY = 2
    ERROR = 4


def _make_register_class(*, value: int = 0) -> type:
    """A mock register class shaped like the generated pybind11 binding."""

    class _RegCls:
        offset = 0x100
        width = 4  # bytes (32 bits)
        name = "control"

        def read(self) -> int:
            return self._value

        def write(self, v: int) -> None:
            self._value = int(v)

        def modify(self, val: int, mask: int) -> None:
            self._value = (self._value & ~mask) | (int(val) & mask)

        def write_fields(self, mask: int, val: int) -> None:
            self._value = (self._value & ~mask) | (int(val) & mask)

    instance = _RegCls()
    instance._value = value  # type: ignore[attr-defined]
    _RegCls._test_instance = instance  # type: ignore[attr-defined]
    return _RegCls


def test_register_level_is_enum_still_works() -> None:
    """Existing ``enum_type`` metadata produces enum-typed reads.

    Regression check: the encode work added a sibling key (``encode_types``)
    but must not perturb the existing ``enum_type`` / ``flag_type`` paths.
    """
    cls = _make_register_class(value=2)
    _default_register_shim(
        cls,
        {
            "fields": {"state": (0, 2)},
            "writable": {"state": True},
            "enum_type": _CtrlRegEnum,
        },
    )

    instance = cls._test_instance  # type: ignore[attr-defined]
    result = instance.read()

    assert isinstance(result, _CtrlRegEnum)
    assert result is _CtrlRegEnum.HALT


def test_register_level_is_flag_still_works() -> None:
    """Existing ``flag_type`` metadata produces IntFlag-typed reads."""
    cls = _make_register_class(value=3)
    _default_register_shim(
        cls,
        {
            "fields": {"ready": (0, 1), "busy": (1, 1), "error": (2, 1)},
            "writable": {"ready": True, "busy": True, "error": True},
            "flag_type": _CtrlRegFlags,
        },
    )

    instance = cls._test_instance  # type: ignore[attr-defined]
    result = instance.read()

    assert isinstance(result, _CtrlRegFlags)
    assert result == (_CtrlRegFlags.READY | _CtrlRegFlags.BUSY)


def test_register_metadata_propagates_encode_types() -> None:
    """``encode_types`` is stashed on the class for sibling introspection.

    The default shim stores the metadata bundle on ``cls.__peakrdl_meta__``;
    sibling units use it to look up per-field encode classes without
    re-walking the field classes themselves.
    """
    cls = _make_register_class()
    _default_register_shim(
        cls,
        {
            "fields": {"baud_rate": (0, 3)},
            "writable": {"baud_rate": True},
            "encode_types": {"baud_rate": _BaudRate},
        },
    )

    meta = cls.__peakrdl_meta__  # type: ignore[attr-defined]
    assert meta["encode_types"] == {"baud_rate": _BaudRate}


# ---------------------------------------------------------------------------
# Registry seam: backward-compat for sibling single-arg field enhancements.
# ---------------------------------------------------------------------------


def test_apply_field_enhancements_dispatches_on_arity() -> None:
    """The registry must invoke 1-arg and 2-arg field hooks correctly.

    ``bits.py`` and ``side_effects.py`` register single-arg ``(cls)``
    callables; the encode path adds a 2-arg ``(cls, metadata)`` shape.
    Both must be invoked with the right argument count when
    ``apply_field_enhancements`` fires.
    """
    seen_one: list[type] = []
    seen_two: list[tuple[type, dict]] = []

    @_registry.register_field_enhancement
    def legacy_hook(c: type) -> None:
        seen_one.append(c)

    @_registry.register_field_enhancement
    def new_hook(c: type, m: dict) -> None:
        seen_two.append((c, m))

    class FakeField:
        pass

    try:
        _registry.apply_field_enhancements(FakeField, {"encode": _BaudRate})
        assert seen_one == [FakeField]
        assert seen_two == [(FakeField, {"encode": _BaudRate})]
    finally:
        # Clean up: the registry is module-global.
        _registry.get_field_enhancers().remove(legacy_hook)
        _registry.get_field_enhancers().remove(new_hook)


def test_apply_field_enhancements_omits_metadata_for_legacy_callers() -> None:
    """Calling ``apply_field_enhancements(cls)`` (no metadata) still works.

    ``tests/runtime/test_registry.py`` and ``_default_field_shim`` callers
    that pass only ``cls`` must continue to function — the new metadata
    parameter is optional.
    """
    seen: list[type] = []

    @_registry.register_field_enhancement
    def hook(c: type) -> None:
        seen.append(c)

    class FakeField:
        pass

    try:
        _registry.apply_field_enhancements(FakeField)
        assert seen == [FakeField]
    finally:
        _registry.get_field_enhancers().remove(hook)


# ---------------------------------------------------------------------------
# Out-of-range decode behaviour.
# ---------------------------------------------------------------------------


def test_field_with_encode_decoded_for_out_of_range_value() -> None:
    """A FieldValue carrying a value not in the enum decodes as the raw int.

    Matches the contract in ``FieldValue.decoded()`` — falls back to int
    when the value is outside the enum's defined members.
    """
    # _BaudRate has only 0/1/2; width=3 allows up to 7.
    cls = _make_field_class(value=5)
    _default_field_shim(cls, {"encode": _BaudRate})

    instance = cls._test_instance  # type: ignore[attr-defined]
    result = instance.read()

    assert int(result) == 5
    # Out of range -> decoded() returns the int.
    decoded = result.decoded()
    assert decoded == 5
    assert not isinstance(decoded, _BaudRate)


# ---------------------------------------------------------------------------
# Encode in metadata that isn't a proper IntEnum should be ignored safely.
# ---------------------------------------------------------------------------


def test_non_intenum_encode_is_ignored() -> None:
    """A non-IntEnum ``encode`` value must not crash the shim.

    Defensive: the metadata bundle is open to sibling-unit extension,
    so a bogus value should fall through to the plain FieldValue path
    rather than raise during class-attach.
    """
    cls = _make_field_class(value=1)
    _default_field_shim(cls, {"encode": "not-a-type"})

    instance = cls._test_instance  # type: ignore[attr-defined]
    result = instance.read()

    assert isinstance(result, FieldValue)
    assert result.encode is None
    assert not hasattr(cls, "choices")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
