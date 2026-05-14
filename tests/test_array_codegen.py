"""Unit tests for Phase 1 register-array codegen (issue #138).

These exercise the exporter and Jinja templates without building any
C++. The companion ``test_array_integration.py`` builds the generated
module and validates the runtime surface; that one is gated on cmake +
pybind11 availability and may skip in CI.

Stop-gap coverage (regfile / addrmap / multi-dim arrays) is included
here because the ``NotSupportedError`` raises directly from the
exporter — no build needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter
from peakrdl_pybind11.runtime.errors import NotSupportedError


SIMPLE_ARRAY_RDL = """
addrmap simple_array_soc {
    reg {
        field { sw=rw; hw=r; } data[31:0] = 0;
    } lut[8] @ 0x100;
};
"""


def _export(rdl_text: str, *, soc_name: str = "simple_array_soc", tmpdir: Path):
    """Compile + export the given RDL; return the output directory path."""
    rdl_path = tmpdir / f"{soc_name}.rdl"
    rdl_path.write_text(rdl_text)
    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_path))
    root = rdl.elaborate()
    out = tmpdir / "out"
    out.mkdir()
    Pybind11Exporter().export(root.top, str(out), soc_name=soc_name)
    return out


class TestArrayDescriptorEmission:
    """The C++ descriptor header carries the array typedef + class."""

    def test_array_class_emitted_once(self, tmp_path: Path) -> None:
        """Exactly one ``<reg>_array_t`` class is emitted for the array."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        desc = (out / "simple_array_soc_descriptors.hpp").read_text()
        # One forward-decl ``class X;`` and one class body ``class X :``.
        assert desc.count("class simple_array_soc__lut_array_t;") == 1
        assert desc.count("class simple_array_soc__lut_array_t :") == 1
        # Entry type emitted once: ``class simple_array_soc__lut_t :``.
        # (No forward declaration is needed for the entry; only addrmap /
        # regfile / mem / array have forward declarations today.)
        assert desc.count("class simple_array_soc__lut_t :") == 1
        # Inherits from ``ArrayBase<entry>``.
        assert "ArrayBase<simple_array_soc__lut_t>" in desc

    def test_array_constructor_uses_stride_and_size(self, tmp_path: Path) -> None:
        """The ctor passes (size=8, stride=4) to ``ArrayBase``."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        desc = (out / "simple_array_soc_descriptors.hpp").read_text()
        # The single-arg constructor pre-fills (name, base, rel, size, stride).
        assert '"lut", base_offset,' in desc
        assert "0x100," in desc  # relative offset of the array base.
        assert "8, 4" in desc  # num_entries, stride bytes

    def test_entry_class_has_zero_relative_offset(self, tmp_path: Path) -> None:
        """Arrayed entry class uses 0 relative offset (ArrayBase adds idx*stride)."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        desc = (out / "simple_array_soc_descriptors.hpp").read_text()
        # The entry's RegisterBase ctor passes ``base_offset, 0x0, 4`` —
        # the per-entry offset comes from ArrayBase at construction.
        assert 'RegisterBase("lut", base_offset, 0x0, 4)' in desc

    def test_parent_instantiates_array_type(self, tmp_path: Path) -> None:
        """The SoC class member is typed ``<entry>_array_t`` not ``<entry>_t``."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        desc = (out / "simple_array_soc_descriptors.hpp").read_text()
        assert "simple_array_soc__lut_array_t lut{offset_};" in desc

    def test_forward_declaration_emitted(self, tmp_path: Path) -> None:
        """The array class is forward-declared at the top of the header."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        desc = (out / "simple_array_soc_descriptors.hpp").read_text()
        assert "class simple_array_soc__lut_array_t;" in desc


class TestArrayBindingEmission:
    """The pybind11 binding wires sequence-protocol + shape/stride."""

    def test_array_binding_present(self, tmp_path: Path) -> None:
        """``py::class_<lut_array_t, NodeBase>`` is emitted with name."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        bindings = (out / "simple_array_soc_bindings.cpp").read_text()
        assert "py::class_<simple_array_soc__lut_array_t, NodeBase>" in bindings
        assert '"simple_array_soc__lut_array_t"' in bindings

    def test_array_binding_exposes_indexing(self, tmp_path: Path) -> None:
        """Both int and slice __getitem__ overloads are bound."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        bindings = (out / "simple_array_soc_bindings.cpp").read_text()
        # int overload via lambda; slice overload via py::slice lambda.
        assert ".def(\"__getitem__\"," in bindings
        assert "size_t i" in bindings
        assert "py::slice slice" in bindings
        assert "reference_internal" in bindings
        assert ".def(\"__iter__\"" in bindings
        assert ".def(\"__len__\"" in bindings

    def test_array_binding_exposes_shape_and_stride(self, tmp_path: Path) -> None:
        """``shape`` returns a tuple and ``stride`` returns the C++ stride."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        bindings = (out / "simple_array_soc_bindings.cpp").read_text()
        assert ".def_property_readonly(\"shape\"" in bindings
        assert "py::make_tuple(self.size())" in bindings
        assert ".def_property_readonly(\"stride\"" in bindings


class TestRuntimeWiring:
    """The generated Python runtime carries the array path metadata + hook."""

    def test_array_paths_metadata(self, tmp_path: Path) -> None:
        """The ``_REG_ARRAY_PATHS`` list mentions every arrayed reg."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        runtime = (out / "simple_array_soc" / "__init__.py").read_text()
        assert "_REG_ARRAY_PATHS" in runtime
        # The full RDL path goes in; the post-create hook strips the root.
        assert '("simple_array_soc.lut[]"' in runtime
        # Shape is a 1-tuple for Phase 1.
        assert "(8," in runtime

    def test_wrap_arrays_hook_present(self, tmp_path: Path) -> None:
        """``_wrap_arrays`` is defined and called from ``create()``."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        runtime = (out / "simple_array_soc" / "__init__.py").read_text()
        assert "def _wrap_arrays(soc)" in runtime
        # ``create()`` invokes the hook before firing post-create hooks.
        assert "_wrap_arrays(soc)" in runtime
        # Imports the shared ArrayView wrapper module.
        assert "from peakrdl_pybind11.runtime import arrays as _peakrdl_arrays" in runtime


class TestStubGeneration:
    """The .pyi stub exposes the array class + correct parent attribute type."""

    def test_array_class_stub_emitted(self, tmp_path: Path) -> None:
        """The pyi has a ``<entry>_array_t`` class with the expected surface."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        pyi = (out / "__init__.pyi").read_text()
        assert "class simple_array_soc__lut_array_t" in pyi
        assert "def __len__(self) -> int" in pyi
        assert "def shape" in pyi
        assert "def stride" in pyi

    def test_parent_attribute_uses_array_type(self, tmp_path: Path) -> None:
        """The SoC class stub references ``lut: <entry>_array_t``."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        pyi = (out / "__init__.pyi").read_text()
        assert "lut: simple_array_soc__lut_array_t" in pyi


class TestStopGapErrors:
    """Tier 0 stop-gap: unsupported shapes raise ``NotSupportedError``."""

    def _attempt(self, rdl_text: str, soc_name: str, tmpdir: Path) -> None:
        _export(rdl_text, soc_name=soc_name, tmpdir=tmpdir)

    def test_regfile_array_not_supported(self, tmp_path: Path) -> None:
        rdl_text = """
        addrmap rf_array_soc {
            regfile {
                reg { field { sw=rw; hw=r; } data[31:0] = 0; } ctrl @ 0x0;
            } chan[4] @ 0x0;
        };
        """
        with pytest.raises(NotSupportedError) as exc:
            self._attempt(rdl_text, "rf_array_soc", tmp_path)
        assert "regfile arrays" in str(exc.value).lower()
        assert "#138" in str(exc.value)

    def test_addrmap_array_not_supported(self, tmp_path: Path) -> None:
        rdl_text = """
        addrmap inner {
            reg { field { sw=rw; hw=r; } data[31:0] = 0; } ctrl @ 0x0;
        };
        addrmap am_array_soc {
            inner blocks[2] @ 0x0;
        };
        """
        with pytest.raises(NotSupportedError) as exc:
            self._attempt(rdl_text, "am_array_soc", tmp_path)
        assert "addrmap arrays" in str(exc.value).lower()
        assert "#138" in str(exc.value)

    def test_multi_dim_register_array_not_supported(self, tmp_path: Path) -> None:
        rdl_text = """
        addrmap multi_dim_soc {
            reg {
                field { sw=rw; hw=r; } data[31:0] = 0;
            } grid[2][3] @ 0x100;
        };
        """
        with pytest.raises(NotSupportedError) as exc:
            self._attempt(rdl_text, "multi_dim_soc", tmp_path)
        assert "multi-dim" in str(exc.value).lower()
        assert "#138" in str(exc.value)


class TestCollectNodes:
    """Sanity-check that ``_collect_nodes`` records the array metadata."""

    def test_reg_arrays_collected(self, tmp_path: Path) -> None:
        rdl_path = tmp_path / "x.rdl"
        rdl_path.write_text(SIMPLE_ARRAY_RDL)
        rdl = RDLCompiler()
        rdl.compile_file(str(rdl_path))
        root = rdl.elaborate()

        ex = Pybind11Exporter()
        ex.soc_name = "simple_array_soc"
        nodes = ex._collect_nodes(root.top)

        # The array node appears in ``reg_arrays`` as well as ``regs``
        # (so the entry's C++ class is emitted exactly once).
        assert len(nodes["reg_arrays"]) == 1
        assert len(nodes["regs"]) == 1
        meta = nodes["reg_arrays"][0]
        assert meta["dimensions"] == [8]
        assert meta["stride"] == 4
        assert meta["relative_offset"] == 0x100

    def test_non_arrayed_reg_not_in_reg_arrays(self, tmp_path: Path) -> None:
        """A plain (non-arrayed) register stays out of ``reg_arrays``."""
        rdl_path = tmp_path / "x.rdl"
        rdl_path.write_text("""
        addrmap plain_soc {
            reg { field { sw=rw; hw=r; } data[31:0] = 0; } config @ 0x0;
        };
        """)
        rdl = RDLCompiler()
        rdl.compile_file(str(rdl_path))
        root = rdl.elaborate()

        ex = Pybind11Exporter()
        ex.soc_name = "plain_soc"
        nodes = ex._collect_nodes(root.top)

        assert nodes["reg_arrays"] == []
        assert len(nodes["regs"]) == 1
