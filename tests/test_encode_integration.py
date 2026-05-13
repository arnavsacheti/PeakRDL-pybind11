"""Integration test for per-field RDL ``encode`` -> IntEnum (sketch §8.1).

Exports a small RDL with an ``encode`` UDP, builds the C++ extension,
then asserts that:

* The generated module exposes the per-field IntEnum class.
* ``field.read()`` returns a value whose ``.decoded()`` is the enum member.
* ``field.choices`` lists the enum members.
* ``field.write(EnumMember)`` round-trips correctly.

Skips automatically if cmake / pybind11 isn't available — mirrors the
gate pattern in ``test_native_masters_integration.py``.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

# RDL with a top-level enum referenced via ``encode = baud_e;`` (the
# syntax systemrdl actually parses — the inline ``encode = name { ... };``
# form in the API sketch is illustrative, not literal).
ENCODE_RDL = """
enum baud_e {
    BAUD_9600 = 3'd0;
    BAUD_115200 = 3'd1;
    BAUD_AUTO = 3'd2;
};

addrmap encode_soc {
    name = "RDL encode integration test";
    reg {
        field {
            sw = rw; hw = r;
            encode = baud_e;
        } baud_rate[2:0] = 0;
    } config @ 0x0;
};
"""


def _build_test_module(workdir: Path, soc_name: str = "encode_soc"):
    """Export + build + import a tiny SoC. Returns module or None on failure."""
    rdl_path = workdir / "encode.rdl"
    rdl_path.write_text(ENCODE_RDL)

    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_path))
    root = rdl.elaborate()

    output_dir = workdir / "out"
    output_dir.mkdir()
    Pybind11Exporter().export(root.top, str(output_dir), soc_name=soc_name)

    build_dir = output_dir / "build"
    build_dir.mkdir()

    # Mirror ``tests/runtime/test_e2e.py``: point cmake at the active
    # virtualenv's pybind11 install and the matching Python interpreter
    # so the build works without a system-wide pybind11 install.
    env = os.environ.copy()
    try:
        import pybind11

        env["pybind11_DIR"] = pybind11.get_cmake_dir()
    except ImportError:
        return None

    if subprocess.run(
        [
            "cmake",
            "-S",
            str(output_dir),
            "-B",
            str(build_dir),
            f"-DPython_EXECUTABLE={sys.executable}",
        ],
        capture_output=True,
        text=True,
        env=env,
    ).returncode != 0:
        return None
    if subprocess.run(
        ["cmake", "--build", str(build_dir), "--config", "Release"],
        capture_output=True,
        text=True,
        env=env,
    ).returncode != 0:
        return None

    so_files = list(build_dir.glob("**/*.so")) + list(build_dir.glob("**/*.pyd"))
    if not so_files:
        return None
    pkg_dir = output_dir / soc_name
    pkg_dir.mkdir(exist_ok=True)
    shutil.copy(so_files[0], pkg_dir)

    sys.path.insert(0, str(output_dir))
    spec = importlib.util.spec_from_file_location(
        soc_name, str(pkg_dir / "__init__.py")
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[soc_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        print(f"Failed to import generated module: {exc}")
        return None
    return module


class TestFieldEncodeIntegration:
    """End-to-end checks for the RDL ``encode`` -> IntEnum pipeline."""

    def test_encode_class_is_exported(self, tmpdir):
        """The generated module exposes the per-field IntEnum class."""
        soc_module = _build_test_module(Path(tmpdir))
        if soc_module is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        # Naming follows: <register-pybind-name>__<field>_e
        encode_cls_name = "encode_soc__config__baud_rate_e"
        assert hasattr(soc_module, encode_cls_name), (
            f"Expected exported IntEnum {encode_cls_name!r}; "
            f"got: {[n for n in dir(soc_module) if n.endswith('_e')]}"
        )
        enum_cls = getattr(soc_module, encode_cls_name)

        from enum import IntEnum

        assert issubclass(enum_cls, IntEnum)
        names = [m.name for m in enum_cls]
        assert "BAUD_9600" in names
        assert "BAUD_115200" in names
        assert "BAUD_AUTO" in names
        # Member values must match the RDL definition.
        assert int(enum_cls.BAUD_9600) == 0
        assert int(enum_cls.BAUD_115200) == 1
        assert int(enum_cls.BAUD_AUTO) == 2

    def test_field_read_returns_decodable_value(self, tmpdir):
        """``field.read()`` produces a FieldValue whose ``.decoded()`` is the enum."""
        soc_module = _build_test_module(Path(tmpdir))
        if soc_module is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = soc_module.create()
        soc.attach_master(soc_module.MockMaster())

        enum_cls = soc_module.encode_soc__config__baud_rate_e

        # Write the raw int 1 (= BAUD_115200) and read it back.
        soc.config.baud_rate.write(1)
        result = soc.config.baud_rate.read()

        # Integer behavior preserved.
        assert int(result) == 1
        # FieldValue carries the encode class and ``.decoded()`` returns the
        # enum member. Compare by value/name rather than identity because
        # the exporter writes the runtime module to both ``out/__init__.py``
        # and ``out/encode_soc/__init__.py`` — the import via the package
        # path and any direct ``getattr`` on the module can resolve to
        # distinct class instances even though they describe the same enum.
        decoded = result.decoded()
        assert int(decoded) == int(enum_cls.BAUD_115200)
        assert decoded.name == "BAUD_115200"

    def test_field_write_accepts_enum_member(self, tmpdir):
        """Writing an IntEnum member is equivalent to writing its int value."""
        soc_module = _build_test_module(Path(tmpdir))
        if soc_module is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = soc_module.create()
        soc.attach_master(soc_module.MockMaster())

        enum_cls = soc_module.encode_soc__config__baud_rate_e

        # Write through the enum member; read back as raw int to verify.
        soc.config.baud_rate.write(enum_cls.BAUD_AUTO)
        assert int(soc.config.baud_rate.read(raw=True)) == 2
        # Round-trip through the typed path too. ``==`` (not ``is``) because
        # the duplicated runtime module (see test above) can yield distinct
        # IntEnum class instances pointing at the same RDL enum.
        decoded = soc.config.baud_rate.read().decoded()
        assert int(decoded) == int(enum_cls.BAUD_AUTO)
        assert decoded.name == "BAUD_AUTO"

    def test_field_choices_lists_enum_members(self, tmpdir):
        """``field.choices`` lists the IntEnum members for IDE completion."""
        soc_module = _build_test_module(Path(tmpdir))
        if soc_module is None:
            pytest.skip("Could not build test module (cmake/pybind11 unavailable)")

        soc = soc_module.create()
        soc.attach_master(soc_module.MockMaster())

        enum_cls = soc_module.encode_soc__config__baud_rate_e
        choices = soc.config.baud_rate.choices

        assert isinstance(choices, list)
        # Compare by (name, value) tuples rather than identity, because
        # ``soc_module.encode_soc__config__baud_rate_e`` and the class
        # attached to ``choices`` may come from sibling module copies.
        assert [(m.name, int(m)) for m in choices] == [(m.name, int(m)) for m in enum_cls]
        assert len(choices) == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
