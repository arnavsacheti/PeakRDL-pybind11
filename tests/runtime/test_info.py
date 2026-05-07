"""Tests for :mod:`peakrdl_pybind11.runtime.info`."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass

import pytest

from peakrdl_pybind11.runtime.info import (
    Info,
    TagsNamespace,
    attach_info,
    from_rdl_node,
)


# ---------------------------------------------------------------------------
# Construction & basic attribute access
# ---------------------------------------------------------------------------


def test_info_basic_construction() -> None:
    """Spec: ``Info(name='x', address=0x100, offset=0, ...).name == 'x'``."""
    info = Info(name="x", address=0x100, offset=0, path="x")
    assert info.name == "x"
    assert info.address == 0x100
    assert info.offset == 0
    assert info.path == "x"


def test_info_supports_bare_default_construction() -> None:
    """All fields are defaulted, so ``Info()`` must work for stubs."""
    info = Info()
    assert info.name == ""
    assert info.desc is None
    assert info.address == 0
    assert info.offset == 0
    assert info.regwidth is None
    assert info.access is None
    assert info.reset is None
    assert info.fields == {}
    assert info.path == ""
    assert info.rdl_node is None
    assert info.source is None
    assert info.precedence is None
    assert info.paritycheck is False
    assert info.is_volatile is False
    assert info.is_interrupt_source is False
    assert info.on_read is None
    assert info.on_write is None
    assert info.alias_kind is None


def test_info_is_a_dataclass_with_slots() -> None:
    assert is_dataclass(Info)
    # frozen+slots dataclasses have __slots__ and no __dict__ on instances.
    assert hasattr(Info, "__slots__")
    info = Info()
    assert not hasattr(info, "__dict__")


def test_info_exposes_all_documented_fields() -> None:
    """Guard the public schema so consumers can rely on attribute names."""
    expected = {
        # common
        "name",
        "desc",
        "address",
        "offset",
        "regwidth",
        "access",
        "reset",
        "fields",
        "path",
        "rdl_node",
        "source",
        "tags",
        # field-only
        "precedence",
        "paritycheck",
        "is_volatile",
        "is_interrupt_source",
        "on_read",
        "on_write",
        "alias_kind",
    }
    actual = {f.name for f in fields(Info)}
    assert actual == expected


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_info_is_frozen_name_assignment_raises() -> None:
    info = Info(name="x", address=0x100, offset=0, path="x")
    with pytest.raises((FrozenInstanceError, AttributeError)):
        info.name = "y"  # type: ignore[misc]


def test_info_frozen_disallows_other_field_mutation() -> None:
    info = Info()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        info.address = 0xDEAD  # type: ignore[misc]


def test_info_slots_disallows_arbitrary_new_attributes() -> None:
    info = Info()
    # frozen+slots may raise FrozenInstanceError (frozen check first),
    # AttributeError (slots layout), or TypeError on some Python versions.
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        info.totally_made_up = 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# from_rdl_node graceful degradation
# ---------------------------------------------------------------------------


def test_from_rdl_node_none_returns_defaults() -> None:
    info = from_rdl_node(None)
    assert info == Info()
    assert info.name == ""
    assert info.address == 0
    assert info.fields == {}


def test_from_rdl_node_handles_stub_object() -> None:
    """A stripped-down stub (no get_property, no children) shouldn't blow up."""

    class Stub:
        inst_name = "stub_reg"

    info = from_rdl_node(Stub())
    assert info.name == "stub_reg"
    assert info.path == "stub_reg"
    # Everything else should fall back to defaults.
    assert info.address == 0
    assert info.fields == {}
    assert info.rdl_node is not None


# ---------------------------------------------------------------------------
# tags namespace
# ---------------------------------------------------------------------------


def test_tags_namespace_allows_arbitrary_name_access() -> None:
    info = Info()
    # Reading any unknown attribute on tags must not raise.
    assert info.tags.does_not_exist is None
    assert info.tags.also_missing is None


def test_tags_namespace_round_trips_set_attributes() -> None:
    ns = TagsNamespace()
    ns.foo = 1
    ns.bar = "value"
    assert ns.foo == 1
    assert ns.bar == "value"
    # Unknown still returns None.
    assert ns.qux is None


def test_each_info_gets_its_own_tags_namespace() -> None:
    """Mutable defaults shared across instances would be a real bug."""
    a = Info()
    b = Info()
    a.tags.foo = 1
    assert b.tags.foo is None
    assert a.tags is not b.tags


def test_each_info_gets_its_own_fields_dict() -> None:
    a = Info()
    b = Info()
    assert a.fields is not b.fields


# ---------------------------------------------------------------------------
# attach_info
# ---------------------------------------------------------------------------


def test_attach_info_sets_class_attribute() -> None:
    class FakeReg:
        pass

    info = Info(name="ctl", address=0x100, offset=0, path="ctl")
    attach_info(FakeReg, info)
    assert FakeReg.info is info  # type: ignore[attr-defined]
    # Instances inherit the class attribute.
    assert FakeReg().info is info  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# repr
# ---------------------------------------------------------------------------


def test_info_repr_includes_path_and_address() -> None:
    info = Info(name="control", address=0x4000_1000, offset=0, path="uart.control", access="rw")
    text = repr(info)
    assert "uart.control" in text
    assert "0x40001000" in text
    assert "rw" in text


def test_info_repr_handles_anonymous() -> None:
    """An empty Info still produces a readable repr."""
    text = repr(Info())
    assert "Info(" in text
    assert "anon" in text or "''" in text
