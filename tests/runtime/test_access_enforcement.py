"""Tests for runtime access-mode enforcement on fields.

The default field shim wraps the generated ``read`` / ``write`` methods
with a gate that consults each field instance's ``is_readable`` and
``is_writable`` attributes (exposed by the C++ ``FieldBase`` binding).

* ``sw=r`` (``is_writable == False``) fields raise :class:`AccessError`
  on any ``write()``, including ``write(v, raw=True)``.
* ``sw=w`` (``is_readable == False``) fields raise :class:`AccessError`
  on any ``read()``, including ``read(raw=True)``.
* ``sw=rw`` fields keep their existing behaviour.
* Fields with neither attribute set default to read-allowed and
  write-allowed (back-compat with stubs that pre-date this enforcement).

The tests use hand-rolled mock classes — no C++ compilation needed — and
drive the shim directly so the test stays focused on the seam under
test rather than the full register/master plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from peakrdl_pybind11.runtime._default_shims import (
    _default_field_shim,
    _default_register_shim,
)
from peakrdl_pybind11.runtime.errors import AccessError


# ---------------------------------------------------------------------------
# Mock field — minimal stand-in for the generated field class shape.
#
# The default field shim only needs:
# * a ``read(self) -> int`` method
# * a ``write(self, value: int) -> None`` method
# * ``lsb`` / ``width`` (consumed by the shim when wrapping the read value
#   in a ``FieldValue``)
# * ``name`` (used in :class:`AccessError` messages)
# * ``is_readable`` / ``is_writable`` (the enforcement signal; optional —
#   missing attrs default to ``True`` for back-compat)
# ---------------------------------------------------------------------------


def _make_field_class(
    *,
    readable: bool | None,
    writable: bool | None,
    name: str = "ctrl",
    lsb: int = 0,
    width: int = 8,
) -> type:
    """Build a fresh field class wired through ``_default_field_shim``.

    Each call returns a distinct class so the shim's idempotency guard
    (``__peakrdl_enhanced__``) doesn't cross-pollute between tests.

    ``readable``/``writable`` of ``None`` omits the attribute entirely so
    the missing-attribute default path is exercised; ``True``/``False``
    sets the attribute as a class-level constant (mirroring how the C++
    binding exposes ``is_readable``/``is_writable`` as instance attrs of
    the per-field generated class).
    """

    namespace: dict[str, object] = {
        "lsb": lsb,
        "width": width,
        "name": name,
        # Per-instance store used by ``read``/``write`` so writes round-trip.
        "_storage": 0,
    }
    if readable is not None:
        namespace["is_readable"] = readable
    if writable is not None:
        namespace["is_writable"] = writable

    def read(self: "MockField") -> int:
        return type(self)._storage  # type: ignore[attr-defined]

    def write(self: "MockField", value: int) -> None:
        type(self)._storage = int(value) & ((1 << self.width) - 1)  # type: ignore[attr-defined]

    namespace["read"] = read
    namespace["write"] = write

    cls = type("MockField", (), namespace)
    _default_field_shim(cls)
    return cls


class MockField:
    """Type alias declared only to give ``read``/``write`` a self type."""


# ---------------------------------------------------------------------------
# Read-only field (sw=r): is_readable=True, is_writable=False.
# ---------------------------------------------------------------------------


def test_read_only_write_raises_access_error() -> None:
    cls = _make_field_class(readable=True, writable=False, name="status")
    instance = cls()

    with pytest.raises(AccessError) as exc_info:
        instance.write(0xFF)

    err = exc_info.value
    assert err.access_mode == "r"
    assert err.node_path == "status"
    assert "status" in str(err)


def test_read_only_write_raw_also_raises() -> None:
    """The ``raw=True`` fast path must also be gated — otherwise the bus
    write happens before the check, defeating the enforcement."""
    cls = _make_field_class(readable=True, writable=False, name="status")
    instance = cls()

    with pytest.raises(AccessError) as exc_info:
        instance.write(0xFF, raw=True)

    assert "status" in str(exc_info.value)


def test_read_only_read_succeeds() -> None:
    """Reading a read-only field is the whole point — must not raise."""
    cls = _make_field_class(readable=True, writable=False, name="status")
    instance = cls()
    type(instance)._storage = 0x42  # type: ignore[attr-defined]

    # Returns a FieldValue (int subclass) on the typed path.
    result = instance.read()
    assert int(result) == 0x42

    # And a plain int on the raw path.
    raw_result = instance.read(raw=True)
    assert raw_result == 0x42
    assert isinstance(raw_result, int)


# ---------------------------------------------------------------------------
# Write-only field (sw=w): is_readable=False, is_writable=True.
# ---------------------------------------------------------------------------


def test_write_only_read_raises_access_error() -> None:
    cls = _make_field_class(readable=False, writable=True, name="cmd")
    instance = cls()

    with pytest.raises(AccessError) as exc_info:
        instance.read()

    err = exc_info.value
    assert err.access_mode == "w"
    assert err.node_path == "cmd"
    assert "cmd" in str(err)


def test_write_only_read_raw_also_raises() -> None:
    """``read(raw=True)`` must raise before doing the bus read so the
    enforcement holds even on the fast path."""
    cls = _make_field_class(readable=False, writable=True, name="cmd")
    instance = cls()

    with pytest.raises(AccessError) as exc_info:
        instance.read(raw=True)

    assert "cmd" in str(exc_info.value)


def test_write_only_write_succeeds() -> None:
    """Writing a write-only field is the whole point — must not raise."""
    cls = _make_field_class(readable=False, writable=True, name="cmd")
    instance = cls()

    instance.write(0x37)
    assert type(instance)._storage == 0x37  # type: ignore[attr-defined]

    instance.write(0xAB, raw=True)
    assert type(instance)._storage == 0xAB  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Read-write field (sw=rw): both paths work normally.
# ---------------------------------------------------------------------------


def test_read_write_field_read_and_write_succeed() -> None:
    cls = _make_field_class(readable=True, writable=True, name="data")
    instance = cls()

    instance.write(0x55)
    assert int(instance.read()) == 0x55

    instance.write(0xAA, raw=True)
    assert instance.read(raw=True) == 0xAA


# ---------------------------------------------------------------------------
# Back-compat: a field class with neither ``is_readable`` nor
# ``is_writable`` attribute must default to read-allowed AND
# write-allowed. This matches the task spec ("treat missing as True
# (read-allowed) — safest default") and keeps unannotated mocks and
# pre-enforcement bindings working unchanged.
# ---------------------------------------------------------------------------


def test_missing_is_readable_defaults_to_read_allowed() -> None:
    cls = _make_field_class(readable=None, writable=None, name="legacy")
    instance = cls()

    # Both reads and writes succeed: missing attrs → defaults of True.
    instance.write(0x12)
    assert int(instance.read()) == 0x12
    assert instance.read(raw=True) == 0x12


def test_missing_is_writable_defaults_to_write_allowed() -> None:
    """Symmetric back-compat for the write path."""
    cls = _make_field_class(readable=None, writable=None, name="legacy")
    instance = cls()

    # No ``AccessError`` even with ``raw=True``.
    instance.write(0x99, raw=True)
    assert type(instance)._storage == 0x99  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AccessError message shape: includes the field's node_path AND the
# access mode token, so CI logs can triage failures without digging.
# ---------------------------------------------------------------------------


def test_access_error_message_includes_field_name_and_mode() -> None:
    cls = _make_field_class(readable=True, writable=False, name="my_field")
    instance = cls()

    with pytest.raises(AccessError) as exc_info:
        instance.write(1)

    msg = str(exc_info.value)
    # The canonical AccessError form is ``"<path> is sw=<mode>"``.
    assert "my_field" in msg
    assert "sw=r" in msg


def test_access_error_for_write_only_includes_w_mode() -> None:
    cls = _make_field_class(readable=False, writable=True, name="my_field")
    instance = cls()

    with pytest.raises(AccessError) as exc_info:
        instance.read()

    msg = str(exc_info.value)
    assert "my_field" in msg
    assert "sw=w" in msg


# ---------------------------------------------------------------------------
# Register-level back-compat: ``metadata["readable"]`` missing must
# default the register-shim path to read-allowed for every field. We
# drive ``_default_register_shim`` directly with a metadata dict that
# omits ``readable`` and verify nothing blows up — the enforcement is
# delegated to per-field instance attrs (covered above).
# ---------------------------------------------------------------------------


@dataclass
class _MockRegisterNative:
    """Minimal register stub for ``_default_register_shim``.

    Provides the four C++-binding entry points the shim wraps:
    ``read``, ``write``, ``modify``, ``write_fields``. The body of each
    is irrelevant for this test — we only care that the shim accepts a
    metadata dict missing the ``readable`` key.
    """

    name: str = "reg"
    offset: int = 0
    width: int = 4

    def read(self: "_MockRegisterNative") -> int:  # pragma: no cover - not exercised
        return 0

    def write(self: "_MockRegisterNative", value: int) -> None:  # pragma: no cover
        pass

    def modify(self: "_MockRegisterNative", value: int, mask: int) -> None:  # pragma: no cover
        pass

    def write_fields(self: "_MockRegisterNative", mask: int, value: int) -> None:  # pragma: no cover
        pass


def test_register_shim_accepts_metadata_without_readable_key() -> None:
    """Back-compat: templates that don't emit ``readable`` must still
    wire up cleanly through ``_default_register_shim``."""

    # Build a fresh class so the enhanced-marker idempotency guard
    # doesn't trip between tests.
    cls = type(
        "MockRegister",
        (_MockRegisterNative,),
        {},
    )

    metadata = {
        "fields": {"data": (0, 8)},
        "writable": {"data": True},
        # Deliberately no "readable" key — exercises the back-compat path.
        "name": "reg",
        "path": "test.reg",
    }

    # Must not raise.
    _default_register_shim(cls, metadata)

    # Sanity: the shim attached the typed-read marker, proving it ran.
    assert getattr(cls.read, "__peakrdl_enhanced__", False) is True
    # And ``write_fields`` was replaced with the validating Python shim.
    assert cls._native_write_fields is _MockRegisterNative.write_fields


def test_register_shim_write_fields_uses_access_error_for_non_writable() -> None:
    """The ``write_fields`` shim raises :class:`AccessError` (not
    ``PermissionError``) so the error type is consistent with the
    per-field ``write()`` gate."""

    captured: dict[str, tuple[int, int]] = {}

    class MockRegister(_MockRegisterNative):
        def write_fields(self, mask: int, value: int) -> None:  # type: ignore[override]
            captured["call"] = (mask, value)

    metadata = {
        "fields": {"locked": (0, 8), "data": (8, 8)},
        "writable": {"locked": False, "data": True},
        # ``readable`` omitted on purpose: confirms the default-True
        # fallback inside ``_make_write_fields`` produces an ``sw=r``
        # access mode for the read-only field.
        "name": "reg",
    }
    _default_register_shim(MockRegister, metadata)

    instance = MockRegister()

    # Writing the writable field is fine.
    instance.write_fields(data=0xAB)
    assert "call" in captured

    # Writing the non-writable field raises ``AccessError`` (and not
    # the legacy ``PermissionError``).
    with pytest.raises(AccessError) as exc_info:
        instance.write_fields(locked=0x12)
    msg = str(exc_info.value)
    assert "locked" in msg
    assert "sw=r" in msg  # readable defaults to True, writable=False → "r"
