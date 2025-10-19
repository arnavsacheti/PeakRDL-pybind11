from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
pytest.importorskip("jinja2")

from peakrdl_pybind.backend import PyBindBackend, PyBindOptions
from peakrdl_pybind.ir import IRBuilder


@dataclass
class MockField:
    inst_name: str
    lsb: int
    msb: int
    reset: int
    access: str = "rw"

    def get_property(self, name: str):
        if name == "reset":
            return self.reset
        if name == "sw":
            return self.access
        if name == "desc":
            return f"Field {self.inst_name}"
        return None


class MockRegister:
    def __init__(self, inst_name: str, address: int, width: int, fields, *, array_dimensions=None, stride=None):
        self.inst_name = inst_name
        self.absolute_address = address
        self.width = width
        self._fields = list(fields)
        self.array_dimensions = array_dimensions
        self.array_stride = stride

    def get_property(self, name: str):
        if name == "reset":
            return 0
        if name == "sw":
            return "rw"
        if name == "desc":
            return f"Register {self.inst_name}"
        if name == "volatile":
            return False
        return None

    def fields(self):
        return list(self._fields)


class MockBlock:
    def __init__(self, inst_name: str, address: int, *, registers=None, blocks=None, array_dimensions=None, stride=None):
        self.inst_name = inst_name
        self.absolute_address = address
        self._registers = list(registers or [])
        self._blocks = list(blocks or [])
        self.array_dimensions = array_dimensions
        self.array_stride = stride

    def children(self):
        return [*self._registers, *self._blocks]

    def get_property(self, name: str):
        if name == "desc":
            return f"Block {self.inst_name}"
        return None


@pytest.fixture
def mock_design():
    lcr = MockRegister(
        "LCR",
        address=0x1000,
        width=8,
        fields=[MockField("DLAB", 7, 7, 0), MockField("WLS", 0, 1, 3)],
    )
    uart_reg = MockRegister(
        "CTRL",
        address=0x2000,
        width=32,
        fields=[MockField("enable", 0, 0, 0)],
    )
    timer_array = MockRegister(
        "TIMER",
        address=0x3000,
        width=16,
        fields=[MockField("value", 0, 7, 0)],
        array_dimensions=(2,),
        stride=4,
    )
    uart_block = MockBlock(
        "uart",
        address=0x2000,
        registers=[uart_reg],
        array_dimensions=(4,),
        stride=0x100,
    )
    top = MockBlock("top", address=0, registers=[lcr, timer_array], blocks=[uart_block])
    return top


def test_ir_builder_collects_registers(mock_design):
    builder = IRBuilder(word_bytes=4, little_endian=True)
    soc = builder.build(mock_design, soc_name="aurora", namespace="soc_aurora")

    assert soc.top.name == "top"
    singles = [reg for reg in soc.top.registers if not reg.is_array]
    arrays = [reg for reg in soc.top.registers if reg.is_array]
    assert len(singles) == 1
    lcr = singles[0]
    assert lcr.name == "LCR"
    assert lcr.fields[0].name == "DLAB"
    assert len(arrays) == 1
    assert arrays[0].name == "TIMER"
    assert arrays[0].array_dimensions == (2,)
    assert soc.top.blocks[0].name == "uart"
    assert soc.top.blocks[0].array_dimensions == (4,)


def test_backend_renders_expected_files(tmp_path: Path, mock_design):
    out_dir = tmp_path / "aurora"
    opts = PyBindOptions(
        soc_name="aurora",
        namespace="aurora",
        out_dir=out_dir,
        word_bytes=4,
        little_endian=True,
        gen_pyi=True,
        with_examples=True,
        access_checks=True,
        emit_reset_writes=False,
    )

    backend = PyBindBackend()
    backend.run(mock_design, opts)

    expected = [
        out_dir / "cpp" / "master.hpp",
        out_dir / "cpp" / "reg_model.cpp",
        out_dir / "cpp" / "soc_module.cpp",
        out_dir / "pyproject.toml",
        out_dir / "CMakeLists.txt",
        out_dir / "typing" / "aurora.pyi",
        out_dir / "masters" / "openocd_master.cpp",
    ]
    for path in expected:
        assert path.exists(), f"Expected generated file {path}"

    content = (out_dir / "cpp" / "soc_module.cpp").read_text()
    assert "PYBIND11_MODULE" in content
