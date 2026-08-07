"""Regression coverage for shared native register/field method bodies."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter
from peakrdl_pybind11.errors import AccessError

RDL_TEMPLATE = """
addrmap {soc_name} {{
    reg {{
        field {{ sw = rw; hw = r; }} low[3:0] = 0;
        field {{ sw = rw; hw = r; }} high[7:4] = 0;
        field {{ sw = rw; hw = r; }} mode[11:8] = 0;
        field {{ sw = r; hw = r; }} status[15:12] = 0;
        field {{ sw = rw; hw = r; }} tag[19:16] = 0;
    }} control @ 0x0;

    reg {{
        field {{ sw = rw; hw = r; }} data[31:0] = 0;
    }} payload @ 0x4;
}};
"""


def _export(tmp_path: Path, soc_name: str, split_bindings: int | None) -> Path:
    source = tmp_path / f"{soc_name}.rdl"
    source.write_text(RDL_TEMPLATE.format(soc_name=soc_name))

    compiler = RDLCompiler()
    compiler.compile_file(str(source))
    root = compiler.elaborate()

    output = tmp_path / "output"
    options = {} if split_bindings is None else {"split_bindings": split_bindings}
    Pybind11Exporter().export(root.top, str(output), soc_name=soc_name, **options)
    return output


def _run_checked(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    if result.returncode:
        pytest.fail(
            f"command failed ({' '.join(command)}):\n{result.stdout}\n{result.stderr}"
        )


def _build_and_import(tmp_path: Path, soc_name: str, split_bindings: int | None) -> ModuleType:
    if shutil.which("cmake") is None:
        pytest.skip("cmake is unavailable")
    pybind11 = pytest.importorskip("pybind11")

    output = _export(tmp_path, soc_name, split_bindings)
    build = output / "build"
    env = os.environ.copy()
    cmake_prefix = str(pybind11.get_cmake_dir())
    if env.get("CMAKE_PREFIX_PATH"):
        cmake_prefix = os.pathsep.join((cmake_prefix, env["CMAKE_PREFIX_PATH"]))
    env["CMAKE_PREFIX_PATH"] = cmake_prefix

    _run_checked(["cmake", "-S", str(output), "-B", str(build)], cwd=output, env=env)
    _run_checked(
        ["cmake", "--build", str(build), "--config", "Release"],
        cwd=output,
        env=env,
    )

    extensions = [path for path in build.rglob(f"_{soc_name}_native*") if path.is_file()]
    assert extensions, "native extension was not produced"
    package = output / soc_name
    shutil.copy2(extensions[0], package / extensions[0].name)

    sys.path.insert(0, str(output))
    try:
        return importlib.import_module(soc_name)
    finally:
        sys.path.remove(str(output))


@pytest.mark.parametrize("split_bindings", [None, 1], ids=["default-single-file", "split"])
def test_shared_methods_are_emitted_and_bound_once(
    tmp_path: Path,
    split_bindings: int | None,
) -> None:
    suffix = "default" if split_bindings is None else str(split_bindings)
    soc_name = f"compact_shape_{suffix}"
    output = _export(tmp_path, soc_name, split_bindings)
    descriptor = (output / f"{soc_name}_descriptors.hpp").read_text()
    binding_files = sorted(output.glob(f"{soc_name}_bindings*.cpp"))
    bindings = "\n".join(path.read_text() for path in binding_files)

    assert descriptor.count('throw std::runtime_error("Field " + name_ + " is not readable")') == 1
    assert descriptor.count('throw std::runtime_error("Field " + name_ + " is not writable")') == 1
    assert descriptor.count("void write_fields(uint64_t mask, uint64_t value)") == 1
    assert "parent_(parent)" not in descriptor

    assert bindings.count("&FieldBase::read") == 1
    assert bindings.count("&FieldBase::write") == 1
    assert bindings.count("&RegisterBase::write_fields") == 1
    assert "_field::read" not in bindings
    assert "_field::write" not in bindings

    # Concrete generated types and their named attributes remain present.
    assert f"class {soc_name}__control_t : public RegisterBase" in descriptor
    assert "class low_field : public FieldBase" in descriptor
    assert f'py::class_<{soc_name}__control_t, RegisterBase>' in bindings
    assert f'py::class_<{soc_name}__control_t::low_field, FieldBase>' in bindings
    assert '.def_readonly("low"' in bindings


@pytest.mark.integration
@pytest.mark.parametrize("split_bindings", [None, 1], ids=["default-single-file", "split"])
def test_shared_methods_preserve_runtime_behavior(
    tmp_path: Path,
    split_bindings: int | None,
) -> None:
    suffix = "default" if split_bindings is None else str(split_bindings)
    soc_name = f"compact_runtime_{suffix}"
    module = _build_and_import(tmp_path, soc_name, split_bindings)
    soc = module.create()

    assert isinstance(soc.control, module.RegisterBase)
    assert isinstance(soc.control.low, module.FieldBase)
    assert type(soc.control.low) is not type(soc.control.high)
    assert hasattr(module, f"{soc_name}__control_t")
    assert hasattr(module, f"{soc_name}__control_low_field")
    assert (soc.control.low.name, soc.control.low.lsb, soc.control.low.width) == ("low", 0, 4)

    store: dict[int, int] = {}
    calls = {"read": 0, "write": 0}

    def read(address: int, width: int) -> int:
        calls["read"] += 1
        return store.get(address, 0)

    def write(address: int, value: int, width: int) -> None:
        calls["write"] += 1
        store[address] = value

    master = module.CallbackMaster(read, write)
    soc.attach_master(master)

    soc.control.write(0xA5CD0)
    calls.update(read=0, write=0)
    assert int(soc.control.low.read()) == 0
    assert calls == {"read": 1, "write": 0}

    calls.update(read=0, write=0)
    soc.control.low.write(0xB)
    assert calls == {"read": 1, "write": 1}
    assert store[soc.control.offset] == 0xA5CDB

    calls.update(read=0, write=0)
    soc.control.write_fields(low=0x2, high=0x3, mode=0x4, tag=0x6)
    assert calls == {"read": 1, "write": 1}
    assert store[soc.control.offset] == 0x65432

    calls.update(read=0, write=0)
    with pytest.raises(AccessError, match=r"control\.status is sw=r"):
        soc.control.status.write(1)
    assert calls == {"read": 0, "write": 0}

    with pytest.raises(AttributeError, match="Unknown field"):
        soc.control.write_fields(typo=1)
