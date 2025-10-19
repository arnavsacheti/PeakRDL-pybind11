from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("jinja2")

from peakrdl_pybind.backend import BackendOptions, IRBuilder, TemplateManager


class FakeField:
    def __init__(self, name, lsb, msb, access="rw", reset=None):
        self.inst_name = name
        self.lsb = lsb
        self.msb = msb
        self.sw_access = access
        self.reset = reset
        self.description = f"Field {name}"
        self.enum_dict = {"zero": 0}


class FakeRegister:
    def __init__(self, name, addr, fields, width=32, reset=0x0):
        self.inst_name = name
        self.absolute_address = addr
        self.total_width = width
        self.reset = reset
        self.volatile = False
        self.sw_access = "rw"
        self.description = f"Register {name}"
        self._fields = fields

    def field_children(self):
        return list(self._fields)


class FakeBlock:
    def __init__(self, name, addr, children):
        self.inst_name = name
        self.absolute_address = addr
        self.description = f"Block {name}"
        self._children = children

    def children(self):
        return list(self._children)


def test_ir_builder_collects_registers(tmp_path):
    fields = [FakeField("ENABLE", 0, 0), FakeField("MODE", 1, 2)]
    reg = FakeRegister("CTRL", 0x1000, fields)
    top = FakeBlock("top", 0, [reg])
    options = BackendOptions(
        soc_name="aurora",
        output=tmp_path,
        top="top",
    )
    ir = IRBuilder(options).build(top)

    assert ir.name == "top"
    assert len(ir.registers) == 1
    ctrl = ir.registers[0]
    assert ctrl.address == 0x1000
    assert ctrl.fields[0].name == "ENABLE"
    assert ctrl.fields[1].mask == 0x6


def test_templates_render(tmp_path):
    fields = [FakeField("ENABLE", 0, 0)]
    reg = FakeRegister("CTRL", 0x1000, fields)
    top = FakeBlock("top", 0, [reg])
    options = BackendOptions(
        soc_name="aurora",
        output=tmp_path,
        top="top",
        generate_pyi=True,
    )
    builder = IRBuilder(options)
    ir = builder.build(top)
    tm = TemplateManager()
    context = {
        "options": options,
        "ir": ir,
        "module_name": "soc_aurora",
        "registers": list(ir.all_registers()),
    }
    tm.render_to_file("reg_model.hpp.jinja", tmp_path / "reg_model.hpp", **context)
    assert (tmp_path / "reg_model.hpp").exists()
