"""Unit tests for register- and regfile-array codegen (issue #138).

These exercise the exporter and Jinja templates without building any
C++. The companion ``test_array_integration.py`` builds the generated
module and validates the runtime surface; that one is gated on cmake +
pybind11 availability and may skip in CI.

Stop-gap coverage (addrmap / multi-dim / field arrays) is included
here because the ``NotSupportedError`` raises directly from the
exporter — no build needed. Phase 2 (issue #138) flips the regfile-
array stop-gap from ``NotSupportedError`` to working codegen; the
unit assertions for that live in :class:`TestRegfileArrayDescriptorEmission`
and :class:`TestRegfileArrayBindingEmission` below.
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

# Phase 2 (#138) — arrayed regfile at the SoC root. The regfile carries
# two children registers; tests assert each entry's relative offset
# is preserved per-instance and the array stride spans the regfile
# size.
REGFILE_ARRAY_RDL = """
addrmap rf_array_soc {
    regfile {
        reg {
            field { sw=rw; hw=r; } enable[0:0] = 0;
        } config @ 0x0;
        reg {
            field { sw=r; hw=w; } status[0:0];
        } stat @ 0x4;
    } channel[4] @ 0x100;
};
"""

# Phase 2 (#138) — combined register- and regfile-array RDL to catch
# unified-list ordering regressions. Both shapes should work side by
# side after the ``nodes["reg_arrays"]`` → ``nodes["arrays"]``
# consolidation.
MIXED_ARRAY_RDL = """
addrmap mixed_array_soc {
    reg {
        field { sw=rw; hw=r; } data[31:0] = 0;
    } lut[8] @ 0x0;
    regfile {
        reg {
            field { sw=rw; hw=r; } enable[0:0] = 0;
        } config @ 0x0;
    } channel[2] @ 0x100;
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
    """Tier 0 stop-gap: unsupported shapes raise ``NotSupportedError``.

    Phase 2 (#138) removes the regfile-array stop-gap — that's now
    working codegen. Remaining stop-gaps:

    * addrmap arrays — Phase 3+
    * multi-dim register / regfile arrays — Phase 3
    * field arrays — Phase 4
    """

    def _attempt(self, rdl_text: str, soc_name: str, tmpdir: Path) -> None:
        _export(rdl_text, soc_name=soc_name, tmpdir=tmpdir)

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

    def test_multi_dim_regfile_array_not_supported(self, tmp_path: Path) -> None:
        """Multi-dim regfile arrays still raise (Phase 3 territory)."""
        rdl_text = """
        addrmap multi_dim_rf_soc {
            regfile {
                reg { field { sw=rw; hw=r; } data[31:0] = 0; } ctrl @ 0x0;
            } grid[2][3] @ 0x100;
        };
        """
        with pytest.raises(NotSupportedError) as exc:
            self._attempt(rdl_text, "multi_dim_rf_soc", tmp_path)
        assert "multi-dim" in str(exc.value).lower()
        assert "regfile" in str(exc.value).lower()
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

    def test_unified_arrays_collected(self, tmp_path: Path) -> None:
        """Phase 2 (#138): ``nodes["arrays"]`` exists with ``kind`` discriminator.

        For ``SIMPLE_ARRAY_RDL`` (1 register array), the unified list
        has one entry with ``kind == "reg"`` and ``reg_arrays`` holds
        the same payload as a back-compat alias.
        """
        rdl_path = tmp_path / "x.rdl"
        rdl_path.write_text(SIMPLE_ARRAY_RDL)
        rdl = RDLCompiler()
        rdl.compile_file(str(rdl_path))
        root = rdl.elaborate()

        ex = Pybind11Exporter()
        ex.soc_name = "simple_array_soc"
        nodes = ex._collect_nodes(root.top)

        assert "arrays" in nodes, "unified nodes['arrays'] list expected"
        assert len(nodes["arrays"]) == 1
        assert nodes["arrays"][0]["kind"] == "reg"
        # Phase 1 back-compat alias has the same register-array subset.
        assert len(nodes["reg_arrays"]) == 1


class TestRegfileArrayCollection:
    """Phase 2 (#138) — exporter collects arrayed regfiles into ``nodes['arrays']``."""

    def _collect(self, rdl_text: str, soc_name: str, tmp_path: Path):
        rdl_path = tmp_path / "x.rdl"
        rdl_path.write_text(rdl_text)
        rdl = RDLCompiler()
        rdl.compile_file(str(rdl_path))
        root = rdl.elaborate()

        ex = Pybind11Exporter()
        ex.soc_name = soc_name
        return ex._collect_nodes(root.top)

    def test_regfile_array_collected(self, tmp_path: Path) -> None:
        """An arrayed regfile lands in ``arrays`` with ``kind == "regfile"``."""
        nodes = self._collect(REGFILE_ARRAY_RDL, "rf_array_soc", tmp_path)
        regfile_arrays = [a for a in nodes["arrays"] if a["kind"] == "regfile"]
        assert len(regfile_arrays) == 1
        meta = regfile_arrays[0]
        assert meta["dimensions"] == [4]
        # Stride: regfile size = sum of two 4-byte regs = 8.
        assert meta["stride"] == 8
        assert meta["relative_offset"] == 0x100

    def test_regfile_array_still_in_regfiles_list(self, tmp_path: Path) -> None:
        """Arrayed regfile entry-type is still emitted as a ``regfile_t`` class."""
        nodes = self._collect(REGFILE_ARRAY_RDL, "rf_array_soc", tmp_path)
        # The arrayed regfile's entry type appears in ``regfiles`` so
        # ``regfiles.hpp.jinja`` emits its class. ``arrays`` carries the
        # array-shape metadata separately.
        assert len(nodes["regfiles"]) == 1
        # And the inner registers' classes get emitted too (decsend works).
        reg_paths = {r.get_path() for r in nodes["regs"]}
        # Plenty of detail here — just check the inner regs were walked.
        assert any("config" in p for p in reg_paths)
        assert any("stat" in p for p in reg_paths)

    def test_regfile_array_not_in_reg_arrays_alias(self, tmp_path: Path) -> None:
        """Phase 1 alias only covers register arrays, not regfile arrays."""
        nodes = self._collect(REGFILE_ARRAY_RDL, "rf_array_soc", tmp_path)
        assert nodes["reg_arrays"] == []
        # But the unified list does hold it.
        assert len(nodes["arrays"]) == 1


class TestRegfileArrayDescriptorEmission:
    """The C++ descriptor header carries the regfile-array typedef + class."""

    def test_regfile_array_class_emitted(self, tmp_path: Path) -> None:
        """A ``<rf>_array_t`` C++ class is emitted for the arrayed regfile."""
        out = _export(REGFILE_ARRAY_RDL, soc_name="rf_array_soc", tmpdir=tmp_path)
        desc = (out / "rf_array_soc_descriptors.hpp").read_text()
        # Forward decl and full class body.
        assert "class rf_array_soc__channel_array_t;" in desc
        assert "class rf_array_soc__channel_array_t :" in desc
        # The regfile entry-type class itself is emitted exactly once.
        assert desc.count("class rf_array_soc__channel_t :") == 1
        # ArrayBase<rf_array_soc__channel_t> is the inheritance shape.
        assert "ArrayBase<rf_array_soc__channel_t>" in desc

    def test_regfile_array_constructor_uses_stride_and_size(self, tmp_path: Path) -> None:
        """The ctor passes ``(num_entries=4, stride=8)`` to ``ArrayBase``."""
        out = _export(REGFILE_ARRAY_RDL, soc_name="rf_array_soc", tmpdir=tmp_path)
        desc = (out / "rf_array_soc_descriptors.hpp").read_text()
        assert '"channel", base_offset,' in desc
        assert "0x100," in desc
        # 4 entries, 8-byte stride.
        assert "4, 8" in desc

    def test_regfile_array_definition_after_entry(self, tmp_path: Path) -> None:
        """``<rf>_array_t`` class body lands *after* its ``<rf>_t`` entry.

        Required so ``std::vector<<rf>_t>`` in ``ArrayBase`` sees the
        complete entry type. Forward declarations don't suffice for
        ``vector`` instantiation.
        """
        out = _export(REGFILE_ARRAY_RDL, soc_name="rf_array_soc", tmpdir=tmp_path)
        desc = (out / "rf_array_soc_descriptors.hpp").read_text()
        # Class *definitions* — not just the forward declarations.
        entry_pos = desc.index("class rf_array_soc__channel_t :")
        array_pos = desc.index("class rf_array_soc__channel_array_t :")
        assert entry_pos < array_pos, (
            "channel_t must be fully defined before channel_array_t's "
            "std::vector<channel_t> instantiation"
        )

    def test_parent_instantiates_regfile_array_type(self, tmp_path: Path) -> None:
        """The SoC class member is ``<rf>_array_t``, not ``<rf>_t``."""
        out = _export(REGFILE_ARRAY_RDL, soc_name="rf_array_soc", tmpdir=tmp_path)
        desc = (out / "rf_array_soc_descriptors.hpp").read_text()
        assert "rf_array_soc__channel_array_t channel{offset_};" in desc

    def test_regfile_entry_uses_zero_relative_offset(self, tmp_path: Path) -> None:
        """Arrayed regfile entry class uses 0 relative offset.

        ``ArrayBase`` adds ``i*stride`` per entry at construction; the
        regfile class itself must not carry the static array base.
        """
        out = _export(REGFILE_ARRAY_RDL, soc_name="rf_array_soc", tmpdir=tmp_path)
        desc = (out / "rf_array_soc_descriptors.hpp").read_text()
        # The arrayed regfile_t ctor passes 0x0 to NodeBase.
        assert 'NodeBase("channel", base_offset, 0x0)' in desc


class TestRegfileArrayBindingEmission:
    """The pybind11 binding wires sequence-protocol + shape/stride for regfile arrays."""

    def test_regfile_array_binding_present(self, tmp_path: Path) -> None:
        out = _export(REGFILE_ARRAY_RDL, soc_name="rf_array_soc", tmpdir=tmp_path)
        bindings = (out / "rf_array_soc_bindings.cpp").read_text()
        assert "py::class_<rf_array_soc__channel_array_t, NodeBase>" in bindings
        assert '"rf_array_soc__channel_array_t"' in bindings

    def test_regfile_array_binding_exposes_indexing(self, tmp_path: Path) -> None:
        out = _export(REGFILE_ARRAY_RDL, soc_name="rf_array_soc", tmpdir=tmp_path)
        bindings = (out / "rf_array_soc_bindings.cpp").read_text()
        # ``channel_array_t`` should appear in both int and slice getitem
        # lambdas — easiest to assert by checking the array class name
        # appears multiple times below ``py::class_<channel_array_t>``.
        # We just check the binding emits shape/stride/iter for it.
        assert "rf_array_soc__channel_array_t::size" in bindings
        assert "rf_array_soc__channel_array_t::stride" in bindings


class TestRegfileArrayRuntimeWiring:
    """The generated runtime carries unified array path metadata."""

    def test_array_paths_metadata_includes_regfile(self, tmp_path: Path) -> None:
        out = _export(REGFILE_ARRAY_RDL, soc_name="rf_array_soc", tmpdir=tmp_path)
        runtime = (out / "rf_array_soc" / "__init__.py").read_text()
        # New unified name.
        assert "_ARRAY_PATHS" in runtime
        # Phase 1 back-compat alias still exists; it just doesn't list
        # the regfile entry.
        assert "_REG_ARRAY_PATHS" in runtime
        # The regfile path is in the unified list.
        assert '("rf_array_soc.channel[]"' in runtime
        assert "(4," in runtime

    def test_install_array_properties_handles_regfile(self, tmp_path: Path) -> None:
        """The runtime's ``_install_array_properties`` walks the unified list."""
        out = _export(REGFILE_ARRAY_RDL, soc_name="rf_array_soc", tmpdir=tmp_path)
        runtime = (out / "rf_array_soc" / "__init__.py").read_text()
        # The loop now consumes ``_ARRAY_PATHS`` (Phase 2 rename).
        assert "for path_with_root, _shape in _ARRAY_PATHS:" in runtime


class TestRegfileArrayStubGeneration:
    """The .pyi stub exposes the regfile-array class + parent attribute."""

    def test_regfile_array_class_stub_emitted(self, tmp_path: Path) -> None:
        out = _export(REGFILE_ARRAY_RDL, soc_name="rf_array_soc", tmpdir=tmp_path)
        pyi = (out / "__init__.pyi").read_text()
        assert "class rf_array_soc__channel_array_t" in pyi
        assert "def __len__(self) -> int" in pyi

    def test_parent_attribute_uses_regfile_array_type(self, tmp_path: Path) -> None:
        out = _export(REGFILE_ARRAY_RDL, soc_name="rf_array_soc", tmpdir=tmp_path)
        pyi = (out / "__init__.pyi").read_text()
        assert "channel: rf_array_soc__channel_array_t" in pyi


class TestNestedParentArrayDocumented:
    """Phase 2 (#138): arrays inside a non-arrayed regfile/addrmap are *not*
    yet supported. The runtime template raises a pointed
    :class:`NotImplementedError` at module-import time so users don't
    get silent misbehavior. Phase 3+ will handle nested parents.
    """

    def test_runtime_module_has_explicit_nested_failure(self, tmp_path: Path) -> None:
        """The generated runtime template surfaces the nested-parent error."""
        out = _export(REGFILE_ARRAY_RDL, soc_name="rf_array_soc", tmpdir=tmp_path)
        runtime = (out / "rf_array_soc" / "__init__.py").read_text()
        # The runtime guards the nested-parent case with a clear message.
        assert "nested-parent path" in runtime.lower()
        # And points at the issue.
        assert "#138" in runtime or "issues/138" in runtime


class TestMixedArrays:
    """Phase 2 (#138) — register + regfile arrays coexist in the same SoC."""

    def test_both_kinds_emit(self, tmp_path: Path) -> None:
        """The unified ``arrays`` list carries one of each kind."""
        rdl_path = tmp_path / "x.rdl"
        rdl_path.write_text(MIXED_ARRAY_RDL)
        rdl = RDLCompiler()
        rdl.compile_file(str(rdl_path))
        root = rdl.elaborate()

        ex = Pybind11Exporter()
        ex.soc_name = "mixed_array_soc"
        nodes = ex._collect_nodes(root.top)

        kinds = sorted(a["kind"] for a in nodes["arrays"])
        assert kinds == ["reg", "regfile"]
        # ``reg_arrays`` only carries the register-array half.
        assert len(nodes["reg_arrays"]) == 1

    def test_both_array_types_emitted_in_descriptors(self, tmp_path: Path) -> None:
        """Both ``lut_array_t`` and ``channel_array_t`` exist in the header."""
        out = _export(MIXED_ARRAY_RDL, soc_name="mixed_array_soc", tmpdir=tmp_path)
        desc = (out / "mixed_array_soc_descriptors.hpp").read_text()
        assert "class mixed_array_soc__lut_array_t :" in desc
        assert "class mixed_array_soc__channel_array_t :" in desc
