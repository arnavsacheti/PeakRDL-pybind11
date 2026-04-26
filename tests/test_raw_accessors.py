"""Tests for read_raw / write_raw fast-path accessors."""

import pytest

from peakrdl_pybind11.int_types import FieldInt, RegisterInt
from tests.test_native_masters_integration import _build_test_module


@pytest.fixture(scope="module")
def soc_module(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("raw_acc")
    mod = _build_test_module(workdir)
    if mod is None:
        pytest.skip("Could not build test module (cmake/pybind11 unavailable)")
    return mod


def test_field_read_raw_returns_plain_int(soc_module):
    soc = soc_module.create()
    soc.attach_master(soc_module.MockMaster())

    soc.reg_a.write(0xDEADBEEF)

    val = soc.reg_a.data.read_raw()
    assert type(val) is int  # exact type, not FieldInt subclass
    assert not isinstance(val, FieldInt)
    assert val == int(soc.reg_a.data.read())


def test_field_write_raw_round_trip(soc_module):
    soc = soc_module.create()
    soc.attach_master(soc_module.MockMaster())

    soc.reg_a.data.write_raw(0x12345678)
    assert soc.reg_a.data.read_raw() == 0x12345678
    assert int(soc.reg_a.data.read()) == 0x12345678


def test_register_read_raw_returns_plain_int(soc_module):
    soc = soc_module.create()
    soc.attach_master(soc_module.MockMaster())

    soc.reg_b.write(0xCAFEBABE)

    val = soc.reg_b.read_raw()
    assert type(val) is int
    assert not isinstance(val, RegisterInt)
    assert val == int(soc.reg_b.read())


def test_register_write_raw_round_trip(soc_module):
    soc = soc_module.create()
    soc.attach_master(soc_module.MockMaster())

    soc.reg_b.write_raw(0xA5A5A5A5)
    assert soc.reg_b.read_raw() == 0xA5A5A5A5
    assert int(soc.reg_b.read()) == 0xA5A5A5A5
