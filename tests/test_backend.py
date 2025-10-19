from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from peakrdl_pybind.backend import PyBindBackend


class MockField:
    def __init__(self, name: str, lsb: int, msb: int, access: str = "rw", reset: int | None = None):
        self.inst_name = name
        self.type_name = "field"
        self.lsb = lsb
        self.msb = msb
        self._properties = {
            "sw": access,
            "reset": reset,
        }

    def get_property(self, name: str):  # pragma: no cover - fallback path
        return self._properties.get(name)


class MockRegister:
    def __init__(
        self,
        name: str,
        address: int,
        width: int,
        reset: int | None,
        access: str = "rw",
        fields: list[MockField] | None = None,
    ) -> None:
        self.inst_name = name
        self.type_name = "reg"
        self.absolute_address = address
        self.width = width
        self._properties = {
            "sw": access,
            "reset": reset,
            "desc": f"Register {name}",
        }
        self._fields = fields or []

    def get_property(self, name: str):
        return self._properties.get(name)


class MockBlock:
    def __init__(
        self,
        name: str,
        address: int,
        children: list[MockBlock | MockRegister] | None = None,
        registers: list[MockRegister] | None = None,
        is_array: bool = False,
        array_dims: list[int] | None = None,
        array_stride: int = 0,
    ) -> None:
        self.inst_name = name
        self.type_name = "addrmap"
        self.absolute_address = address
        self._properties = {"desc": f"Block {name}"}
        self._children = (children or []) + (registers or [])
        self.is_array = is_array
        self.array_dimensions = array_dims or []
        self.array_stride = array_stride

    def get_property(self, name: str):
        return self._properties.get(name)


class MockRegfile(MockBlock):
    def __init__(
        self,
        name: str,
        address: int,
        registers: list[MockRegister],
        is_array: bool = False,
        array_dims: list[int] | None = None,
        array_stride: int = 0,
    ) -> None:
        super().__init__(
            name,
            address,
            children=[],
            registers=registers,
            is_array=is_array,
            array_dims=array_dims,
            array_stride=array_stride,
        )
        self.type_name = "regfile"


def test_backend_generates_cpp_tree(tmp_path: Path) -> None:
    lcr = MockRegister(
        "LCR",
        address=0x1000,
        width=8,
        reset=0x83,
        fields=[MockField("DLAB", 7, 7), MockField("WLS", 0, 1)],
    )
    ctrl = MockRegister(
        "CTRL",
        address=0x2000,
        width=32,
        reset=0,
        fields=[MockField("enable", 0, 0)],
    )
    uart = MockRegfile(
        "uart",
        address=0x2000,
        registers=[ctrl],
        is_array=True,
        array_dims=[4],
        array_stride=0x100,
    )
    top = MockBlock("top", address=0, children=[uart], registers=[lcr])

    backend = PyBindBackend()
    backend.export(
        top,
        tmp_path,
        soc_name="aurora",
        namespace="aurora",
        word_bytes=4,
        little_endian=True,
        with_examples=True,
        gen_pyi=True,
        emit_reset_writes=True,
    )

    cpp_dir = tmp_path / "cpp"
    assert (cpp_dir / "soc_module.cpp").exists()
    reg_model_cpp = (cpp_dir / "reg_model.cpp").read_text(encoding="utf-8")
    assert "LCR" in reg_model_cpp
    assert "uart" in reg_model_cpp

    soc_manifest = json.loads((tmp_path / "soc.json").read_text(encoding="utf-8"))
    assert soc_manifest["module_name"] == "aurora"
    assert soc_manifest["top"]["registers"][0]["name"] == "LCR"

    typing_dir = tmp_path / "typing"
    assert any(path.name.endswith(".pyi") for path in typing_dir.iterdir())

    if (tmp_path / "masters").exists():
        assert any(path.name.endswith("_master.cpp") for path in (tmp_path / "masters").iterdir())
