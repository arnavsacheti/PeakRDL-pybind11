"""Tests for the ``raw=True`` fast-path on ``read``/``write``.

The aspirational API surface from sketch §3.2 is now a single typed
method with a keyword-only ``raw`` flag:

* ``reg.read()`` → ``RegisterInt`` (default)
* ``reg.read(raw=True)`` → plain ``int`` (skip wrap)
* ``reg.write(value)`` / ``reg.write(value, raw=True)``

Same shape on ``field``. The standalone ``read_raw`` / ``write_raw``
helpers have been removed.
"""

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

    val = soc.reg_a.data.read(raw=True)
    assert type(val) is int  # exact type, not FieldInt subclass
    assert not isinstance(val, FieldInt)
    assert val == int(soc.reg_a.data.read())


def test_field_write_raw_round_trip(soc_module):
    soc = soc_module.create()
    soc.attach_master(soc_module.MockMaster())

    soc.reg_a.data.write(0x12345678, raw=True)
    assert soc.reg_a.data.read(raw=True) == 0x12345678
    assert int(soc.reg_a.data.read()) == 0x12345678


def test_register_read_raw_returns_plain_int(soc_module):
    soc = soc_module.create()
    soc.attach_master(soc_module.MockMaster())

    soc.reg_b.write(0xCAFEBABE)

    val = soc.reg_b.read(raw=True)
    assert type(val) is int
    assert not isinstance(val, RegisterInt)
    assert val == int(soc.reg_b.read())


def test_register_write_raw_round_trip(soc_module):
    soc = soc_module.create()
    soc.attach_master(soc_module.MockMaster())

    soc.reg_b.write(0xA5A5A5A5, raw=True)
    assert soc.reg_b.read(raw=True) == 0xA5A5A5A5
    assert int(soc.reg_b.read()) == 0xA5A5A5A5


def test_read_raw_is_keyword_only(soc_module):
    """``raw`` is keyword-only; passing positionally must raise."""
    soc = soc_module.create()
    soc.attach_master(soc_module.MockMaster())

    with pytest.raises(TypeError):
        soc.reg_b.read(True)  # type: ignore[misc]


def test_read_raw_false_returns_typed(soc_module):
    """Explicit ``raw=False`` returns the typed wrapper."""
    soc = soc_module.create()
    soc.attach_master(soc_module.MockMaster())

    soc.reg_b.write(0x55AA55AA)
    val = soc.reg_b.read(raw=False)
    assert isinstance(val, RegisterInt)
    assert int(val) == 0x55AA55AA
