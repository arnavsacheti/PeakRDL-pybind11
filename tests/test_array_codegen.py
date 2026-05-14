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

# Phase 3 (#138) — 2-D register array. The exporter now emits one
# ``ArrayBase`` subclass per axis (innermost first); 2-D produces
# ``inner_0_array_t`` + ``array_t``. Strides are 4 (innermost: entry
# size) and 32 (outermost: inner_axis_size * inner_stride = 8 * 4).
MULTIDIM_REG_RDL = """
addrmap multidim_reg_soc {
    reg {
        field { sw=rw; hw=r; } data[31:0] = 0;
    } matrix[4][8] @ 0x100;
};
"""

# Phase 3 (#138) — 2-D regfile array. Inner stride = regfile size
# (4 bytes, one register); outer stride = 3 * 4 = 12 bytes.
MULTIDIM_REGFILE_RDL = """
addrmap multidim_rf_soc {
    regfile {
        reg {
            field { sw=rw; hw=r; } enable[0:0] = 0;
        } config @ 0x0;
    } channel[2][3] @ 0x200;
};
"""

# Phase 3 (#138) — 3-D register array. Strides chain:
# innermost = 4 (entry size), middle = 4 * 4 = 16, outermost = 3 * 16 = 48.
CUBE_REG_RDL = """
addrmap cube_soc {
    reg {
        field { sw=rw; hw=r; } data[31:0] = 0;
    } cube[2][3][4] @ 0x100;
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

    Phase 3 (#138) removes the multi-dim register and multi-dim
    regfile array stop-gaps — those are now working codegen.
    Remaining stop-gaps:

    * addrmap arrays — out of scope (issue #138)
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
        # Phase 5 (#138) widened ``_ARRAY_PATHS`` to a 4-tuple
        # ``(path, shape, strides, entry_type_name)``; the loop now
        # iterates each entry and unpacks defensively. The earlier
        # ``for path_with_root, _shape in _ARRAY_PATHS:`` shape is gone.
        assert "for entry in _ARRAY_PATHS:" in runtime
        # Defensive unpack present so old-shape tuples still work.
        assert 'entry[2] if len(entry) > 2 else ()' in runtime


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


class TestMultiDimRegisterArrayCollection:
    """Phase 3 (#138) — exporter collects multi-dim register arrays
    into ``nodes['arrays']`` with full ``dimensions`` + per-axis
    ``strides`` lists.
    """

    def _collect(self, rdl_text: str, soc_name: str, tmp_path: Path):
        rdl_path = tmp_path / "x.rdl"
        rdl_path.write_text(rdl_text)
        rdl = RDLCompiler()
        rdl.compile_file(str(rdl_path))
        root = rdl.elaborate()

        ex = Pybind11Exporter()
        ex.soc_name = soc_name
        return ex._collect_nodes(root.top)

    def test_2d_reg_dimensions_and_strides(self, tmp_path: Path) -> None:
        """``matrix[4][8]`` lands with ``dimensions=[4, 8]`` and
        ``strides=[32, 4]`` (outer stride = inner_size * inner_stride).
        """
        nodes = self._collect(MULTIDIM_REG_RDL, "multidim_reg_soc", tmp_path)
        arrays = [a for a in nodes["arrays"] if a["kind"] == "reg"]
        assert len(arrays) == 1
        meta = arrays[0]
        assert meta["dimensions"] == [4, 8]
        assert meta["strides"] == [32, 4]
        # Back-compat: singular ``stride`` = innermost stride.
        assert meta["stride"] == 4
        assert meta["relative_offset"] == 0x100

    def test_3d_reg_dimensions_and_strides(self, tmp_path: Path) -> None:
        """``cube[2][3][4]`` lands with three dims + chained strides."""
        nodes = self._collect(CUBE_REG_RDL, "cube_soc", tmp_path)
        arrays = [a for a in nodes["arrays"] if a["kind"] == "reg"]
        assert len(arrays) == 1
        meta = arrays[0]
        assert meta["dimensions"] == [2, 3, 4]
        # Innermost = 4 (entry size); next = 4*4 = 16; outermost = 3*16 = 48.
        assert meta["strides"] == [48, 16, 4]

    def test_2d_regfile_dimensions_and_strides(self, tmp_path: Path) -> None:
        """``channel[2][3]`` regfile: inner stride = regfile size, outer = 3*size."""
        nodes = self._collect(MULTIDIM_REGFILE_RDL, "multidim_rf_soc", tmp_path)
        arrays = [a for a in nodes["arrays"] if a["kind"] == "regfile"]
        assert len(arrays) == 1
        meta = arrays[0]
        assert meta["dimensions"] == [2, 3]
        # Regfile holds one 4-byte register; inner stride = 4, outer = 12.
        assert meta["strides"] == [12, 4]


class TestMultiDimRegisterArrayDescriptorEmission:
    """The descriptor header carries one ``ArrayBase`` subclass per
    axis (Phase 3 of Tier 3 array support, issue #138).
    """

    def test_2d_emits_two_array_typedefs(self, tmp_path: Path) -> None:
        """For a 2-D array we get ``inner_0_array_t`` + ``array_t``."""
        out = _export(MULTIDIM_REG_RDL, soc_name="multidim_reg_soc", tmpdir=tmp_path)
        desc = (out / "multidim_reg_soc_descriptors.hpp").read_text()
        # Innermost level: wraps the entry type.
        assert "class multidim_reg_soc__matrix_inner_0_array_t :" in desc
        assert "ArrayBase<multidim_reg_soc__matrix_t>" in desc
        # Outermost level: named ``<entry>_array_t``, wraps the inner.
        assert "class multidim_reg_soc__matrix_array_t :" in desc
        assert "ArrayBase<multidim_reg_soc__matrix_inner_0_array_t>" in desc

    def test_2d_inner_carries_innermost_stride_and_size(self, tmp_path: Path) -> None:
        """Inner ctor: (count=8, stride=4); outer ctor: (count=4, stride=32)."""
        out = _export(MULTIDIM_REG_RDL, soc_name="multidim_reg_soc", tmpdir=tmp_path)
        desc = (out / "multidim_reg_soc_descriptors.hpp").read_text()
        # Inner: 8 entries, 4-byte stride. The inner ctor's name string is
        # empty so the outermost provides the user-facing identifier.
        assert "8, 4" in desc
        # Outer: 4 entries, 32-byte stride (8 * 4).
        assert "4, 32" in desc

    def test_2d_outer_carries_inst_name_and_relative_offset(self, tmp_path: Path) -> None:
        """Outermost ctor pre-fills inst_name + RDL relative offset; inner is anonymous."""
        out = _export(MULTIDIM_REG_RDL, soc_name="multidim_reg_soc", tmpdir=tmp_path)
        desc = (out / "multidim_reg_soc_descriptors.hpp").read_text()
        # Outermost: ``"matrix", base_offset, 0x100, ...``.
        assert '"matrix", base_offset,' in desc
        assert "0x100" in desc
        # Inner: ``"", base_offset, 0x0, ...`` — empty name, zero offset.
        assert '"", base_offset, 0x0' in desc

    def test_3d_emits_three_array_typedefs(self, tmp_path: Path) -> None:
        """For a 3-D array we get ``inner_0``, ``inner_1``, ``array_t``."""
        out = _export(CUBE_REG_RDL, soc_name="cube_soc", tmpdir=tmp_path)
        desc = (out / "cube_soc_descriptors.hpp").read_text()
        assert "class cube_soc__cube_inner_0_array_t :" in desc
        assert "class cube_soc__cube_inner_1_array_t :" in desc
        assert "class cube_soc__cube_array_t :" in desc
        # Each level wraps the next-inner: 0 wraps entry, 1 wraps 0, outer wraps 1.
        assert "ArrayBase<cube_soc__cube_t>" in desc
        assert "ArrayBase<cube_soc__cube_inner_0_array_t>" in desc
        assert "ArrayBase<cube_soc__cube_inner_1_array_t>" in desc

    def test_2d_regfile_emits_two_array_typedefs(self, tmp_path: Path) -> None:
        out = _export(
            MULTIDIM_REGFILE_RDL, soc_name="multidim_rf_soc", tmpdir=tmp_path
        )
        desc = (out / "multidim_rf_soc_descriptors.hpp").read_text()
        assert "class multidim_rf_soc__channel_inner_0_array_t :" in desc
        assert "class multidim_rf_soc__channel_array_t :" in desc
        # Outer wraps inner; inner wraps the regfile entry type.
        assert "ArrayBase<multidim_rf_soc__channel_t>" in desc
        assert "ArrayBase<multidim_rf_soc__channel_inner_0_array_t>" in desc

    def test_parent_instantiates_outermost_only(self, tmp_path: Path) -> None:
        """The SoC class instantiates only ``<entry>_array_t`` — the inner
        levels live inside the outer's ``std::vector`` and don't need to
        be referenced by the parent.
        """
        out = _export(MULTIDIM_REG_RDL, soc_name="multidim_reg_soc", tmpdir=tmp_path)
        desc = (out / "multidim_reg_soc_descriptors.hpp").read_text()
        assert "multidim_reg_soc__matrix_array_t matrix{offset_};" in desc
        # No ``inner_0_array_t`` direct member reference in the SoC class.
        soc_class_pos = desc.index("class multidim_reg_soc_t :")
        assert "matrix_inner_0_array_t matrix" not in desc[soc_class_pos:]


class TestMultiDimRegisterArrayBindingEmission:
    """The pybind11 binding emits one ``py::class_`` per axis."""

    def test_2d_emits_two_bindings(self, tmp_path: Path) -> None:
        """Both the inner and outer array classes get registered with pybind."""
        out = _export(MULTIDIM_REG_RDL, soc_name="multidim_reg_soc", tmpdir=tmp_path)
        bindings = (out / "multidim_reg_soc_bindings.cpp").read_text()
        # Both py::class_ blocks emit. The inner getitem returns ``matrix_t``;
        # the outer getitem returns ``matrix_inner_0_array_t``.
        assert "py::class_<multidim_reg_soc__matrix_inner_0_array_t, NodeBase>" in bindings
        assert "py::class_<multidim_reg_soc__matrix_array_t, NodeBase>" in bindings
        # Outer's __getitem__ returns inner; inner's __getitem__ returns entry.
        assert "-> multidim_reg_soc__matrix_inner_0_array_t&" in bindings
        assert "-> multidim_reg_soc__matrix_t&" in bindings

    def test_3d_emits_three_bindings(self, tmp_path: Path) -> None:
        out = _export(CUBE_REG_RDL, soc_name="cube_soc", tmpdir=tmp_path)
        bindings = (out / "cube_soc_bindings.cpp").read_text()
        assert "py::class_<cube_soc__cube_inner_0_array_t, NodeBase>" in bindings
        assert "py::class_<cube_soc__cube_inner_1_array_t, NodeBase>" in bindings
        assert "py::class_<cube_soc__cube_array_t, NodeBase>" in bindings

    def test_2d_regfile_emits_two_bindings(self, tmp_path: Path) -> None:
        out = _export(
            MULTIDIM_REGFILE_RDL, soc_name="multidim_rf_soc", tmpdir=tmp_path
        )
        bindings = (out / "multidim_rf_soc_bindings.cpp").read_text()
        assert "py::class_<multidim_rf_soc__channel_inner_0_array_t, NodeBase>" in bindings
        assert "py::class_<multidim_rf_soc__channel_array_t, NodeBase>" in bindings


class TestMultiDimRuntimeWiring:
    """The generated runtime carries multi-dim ``_ARRAY_PATHS`` shapes."""

    def test_2d_array_paths_shape(self, tmp_path: Path) -> None:
        """``_ARRAY_PATHS`` carries the full multi-dim shape tuple."""
        out = _export(MULTIDIM_REG_RDL, soc_name="multidim_reg_soc", tmpdir=tmp_path)
        runtime = (out / "multidim_reg_soc" / "__init__.py").read_text()
        # Path has two ``[]`` suffixes for 2-D; the runtime strips both.
        assert '("multidim_reg_soc.matrix[][]"' in runtime
        # Full multi-dim shape: (4, 8). Note Jinja emits ``(4, 8, )``.
        assert "(4, 8," in runtime

    def test_flatten_helper_emitted(self, tmp_path: Path) -> None:
        """``_flatten_cxx_array`` lives in the generated runtime so
        ``_wrap_arrays`` can walk nested ``ArrayBase`` chains.
        """
        out = _export(MULTIDIM_REG_RDL, soc_name="multidim_reg_soc", tmpdir=tmp_path)
        runtime = (out / "multidim_reg_soc" / "__init__.py").read_text()
        assert "def _flatten_cxx_array" in runtime

    def test_strip_root_handles_multidim_brackets(self, tmp_path: Path) -> None:
        """``_strip_soc_root`` strips all trailing ``[]`` not just one."""
        out = _export(MULTIDIM_REG_RDL, soc_name="multidim_reg_soc", tmpdir=tmp_path)
        runtime = (out / "multidim_reg_soc" / "__init__.py").read_text()
        # The runtime uses a ``while`` loop now, not a single ``if``.
        assert "while path.endswith" in runtime


class TestMultiDimStubGeneration:
    """The .pyi stub exposes one class per axis."""

    def test_2d_emits_inner_and_outer_stub(self, tmp_path: Path) -> None:
        out = _export(MULTIDIM_REG_RDL, soc_name="multidim_reg_soc", tmpdir=tmp_path)
        pyi = (out / "__init__.pyi").read_text()
        assert "class multidim_reg_soc__matrix_inner_0_array_t" in pyi
        assert "class multidim_reg_soc__matrix_array_t" in pyi

    def test_2d_outer_tuple_overload_present(self, tmp_path: Path) -> None:
        """Outermost stub has a tuple ``__getitem__`` overload for type-checkers.
        Generic over N dimensions: ``tuple[int, ...]``.
        """
        out = _export(MULTIDIM_REG_RDL, soc_name="multidim_reg_soc", tmpdir=tmp_path)
        pyi = (out / "__init__.pyi").read_text()
        # Look at the multidim outer class only; tuple overload must be present.
        outer_start = pyi.index("class multidim_reg_soc__matrix_array_t")
        outer_body = pyi[outer_start : outer_start + 1500]
        assert "tuple[int, ...]" in outer_body

    def test_3d_emits_three_stubs(self, tmp_path: Path) -> None:
        out = _export(CUBE_REG_RDL, soc_name="cube_soc", tmpdir=tmp_path)
        pyi = (out / "__init__.pyi").read_text()
        assert "class cube_soc__cube_inner_0_array_t" in pyi
        assert "class cube_soc__cube_inner_1_array_t" in pyi
        assert "class cube_soc__cube_array_t" in pyi


class TestPhase3StopGaps:
    """Stop-gaps that survive Phase 3."""

    def test_addrmap_array_still_raises(self, tmp_path: Path) -> None:
        """Addrmap arrays remain out of scope (P5 / followup, #138)."""
        rdl_text = """
        addrmap inner {
            reg { field { sw=rw; hw=r; } data[31:0] = 0; } ctrl @ 0x0;
        };
        addrmap am_array_soc {
            inner blocks[2] @ 0x0;
        };
        """
        rdl_path = tmp_path / "x.rdl"
        rdl_path.write_text(rdl_text)
        rdl = RDLCompiler()
        rdl.compile_file(str(rdl_path))
        root = rdl.elaborate()
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(NotSupportedError) as exc:
            Pybind11Exporter().export(root.top, str(out), soc_name="am_array_soc")
        assert "addrmap arrays" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Phase 5 (#138) — codegen-level integration of arrays with the rest of
# the runtime metadata surface (``.info``, walk, tree, snapshot, schema).
# These tests don't need the C++ build; the runtime-shape tests live in
# ``test_array_integration.py``.
# ---------------------------------------------------------------------------


class TestPhase5ArrayPathsShape:
    """``_ARRAY_PATHS`` carries 4-tuples ``(path, shape, strides, entry_type)``."""

    def test_array_paths_includes_strides_and_entry_type(self, tmp_path: Path) -> None:
        """1-D array path tuple has ``(shape=(8,), strides=(4,), entry='lut_t')``."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        runtime = (out / "simple_array_soc" / "__init__.py").read_text()
        # 1-D: shape (8,), strides (4,), entry "simple_array_soc__lut_t".
        assert '"simple_array_soc.lut[]"' in runtime
        assert "(8, )" in runtime or "(8,)" in runtime
        assert "(4, )" in runtime or "(4,)" in runtime
        assert '"simple_array_soc__lut_t"' in runtime

    def test_multidim_array_paths_strides(self, tmp_path: Path) -> None:
        """2-D ``matrix[4][8]`` emits ``strides=(32, 4)`` in ``_ARRAY_PATHS``."""
        out = _export(MULTIDIM_REG_RDL, soc_name="multidim_reg_soc", tmpdir=tmp_path)
        runtime = (out / "multidim_reg_soc" / "__init__.py").read_text()
        # Outer-axis stride = 32 (=8*4); inner = 4.
        assert "(32, 4," in runtime


class TestPhase5RuntimeIntegration:
    """The generated runtime threads metadata into ``wrap_array``."""

    def test_wrap_arrays_passes_path_kwarg(self, tmp_path: Path) -> None:
        """``_wrap_arrays`` passes ``path=`` to ``wrap_array``."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        runtime = (out / "simple_array_soc" / "__init__.py").read_text()
        # ``path=view_path`` keyword argument present in the wrap_array call.
        assert "path=view_path" in runtime
        # The view path is built from the SoC root + relative path.
        assert "view_path = " in runtime

    def test_wrap_arrays_passes_strides_kwarg(self, tmp_path: Path) -> None:
        """``_wrap_arrays`` forwards strides + entry_type_name."""
        out = _export(SIMPLE_ARRAY_RDL, tmpdir=tmp_path)
        runtime = (out / "simple_array_soc" / "__init__.py").read_text()
        assert "strides=strides" in runtime
        assert "entry_type_name=entry_type_name" in runtime


class TestPhase5ArrayInfo:
    """:class:`ArrayInfo` is a separate dataclass shaped for arrays."""

    def test_array_info_distinct_from_info(self) -> None:
        """``ArrayInfo`` is *not* ``Info`` -- the two are sibling dataclasses."""
        from peakrdl_pybind11.runtime.info import ArrayInfo, Info
        assert ArrayInfo is not Info

    def test_array_info_default_kind(self) -> None:
        """``ArrayInfo.kind`` defaults to lowercase ``"array"``."""
        from peakrdl_pybind11.runtime.info import ArrayInfo
        info = ArrayInfo()
        assert info.kind == "array"

    def test_array_info_dims_alias_for_shape(self) -> None:
        """``info.dims`` is a list view of ``info.shape``."""
        from peakrdl_pybind11.runtime.info import ArrayInfo
        info = ArrayInfo(shape=(4, 8))
        assert info.dims == [4, 8]

    def test_array_view_info_property(self) -> None:
        """``ArrayView.info`` exposes an :class:`ArrayInfo`."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        from peakrdl_pybind11.runtime.info import ArrayInfo

        # Build a trivial ArrayView; we don't need real C++ elements for
        # the metadata path -- we just need ``shape`` and the wiring kwargs.
        class _StubReg:
            offset = 0

        view = ArrayView(
            [_StubReg(), _StubReg()],
            shape=(2,),
            path="soc.lut",
            strides=(4,),
            entry_type_name="lut_t",
        )
        info = view.info
        assert isinstance(info, ArrayInfo)
        assert info.shape == (2,)
        assert info.dims == [2]
        assert info.path == "soc.lut"
        assert info.strides == (4,)
        assert info.entry_type_name == "lut_t"
        assert info.kind == "array"
        assert info.name == "lut"  # last segment of path.

    def test_array_view_info_multidim(self) -> None:
        """Multi-dim shape + strides round-trip into ``info``."""
        from peakrdl_pybind11.runtime.arrays import ArrayView

        class _StubReg:
            offset = 0

        view = ArrayView(
            [_StubReg() for _ in range(32)],
            shape=(4, 8),
            path="soc.matrix",
            strides=(32, 4),
            entry_type_name="matrix_t",
        )
        info = view.info
        assert info.shape == (4, 8)
        assert info.dims == [4, 8]
        assert info.strides == (32, 4)


class TestPhase5Routing:
    """``_kind_for`` and walk descend into arrays."""

    def test_kind_for_array_view(self) -> None:
        """``_kind_for(ArrayView)`` returns ``"Array"``."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        from peakrdl_pybind11.runtime.routing import _kind_for

        view = ArrayView([], shape=(0,))
        assert _kind_for(view) == "Array"

    def test_walk_descends_into_array_entries(self) -> None:
        """``_walk(soc)`` yields the array, then each entry."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        from peakrdl_pybind11.runtime.routing import _walk

        class _Reg:
            def read(self) -> int:
                return 0

            def write(self, v: int) -> None:
                return None

        elements = [_Reg(), _Reg(), _Reg()]
        view = ArrayView(elements, shape=(3,), path="soc.lut")

        class _Soc:
            pass

        soc = _Soc()
        soc.lut = view  # type: ignore[attr-defined]

        nodes = list(_walk(soc))
        # soc + view + 3 entries = 5 (view is yielded, then each entry).
        assert len(nodes) == 5
        assert nodes[1] is view
        assert nodes[2:] == elements

    def test_walk_kind_array_filter(self) -> None:
        """``_walk(soc, kind='array')`` yields only the ArrayView."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        from peakrdl_pybind11.runtime.routing import _walk

        class _Reg:
            def read(self) -> int:
                return 0

            def write(self, v: int) -> None:
                return None

        elements = [_Reg(), _Reg()]
        view = ArrayView(elements, shape=(2,), path="soc.lut")

        class _Soc:
            pass

        soc = _Soc()
        soc.lut = view  # type: ignore[attr-defined]

        nodes = list(_walk(soc, kind="array"))
        assert len(nodes) == 1
        assert nodes[0] is view


class TestPhase5SchemaArrayNode:
    """Schema emits ``kind="array"`` entries with a nested ``entry``."""

    def test_schema_array_kind(self) -> None:
        from peakrdl_pybind11.runtime.arrays import ArrayView
        from peakrdl_pybind11.runtime.schema import to_dict

        class _Reg:
            class info:
                kind = "reg"
                name = "lut"
                path = "soc.lut"
                address = 0x100
                regwidth = 32
                desc = None
                fields = {}

            def read(self) -> int:
                return 0

            def write(self, v: int) -> None:
                return None

        view = ArrayView(
            [_Reg(), _Reg()],
            shape=(2,),
            path="soc.lut",
            strides=(4,),
            entry_type_name="lut_t",
        )

        class _Soc:
            pass

        soc = _Soc()
        soc.lut = view  # type: ignore[attr-defined]

        out = to_dict(soc)
        # Top-level SoC carries one child -- the array.
        children = out.get("children", [])
        assert len(children) == 1
        node = children[0]
        assert node["kind"] == "array"
        assert node["path"] == "soc.lut"
        assert node["dims"] == [2]
        assert node["shape"] == [2]
        assert node["strides"] == [4]
        assert node["entry_type_name"] == "lut_t"
        # The nested entry dict describes the entry shape.
        assert node["entry"]["kind"] == "reg"

    def test_schema_multidim_array(self) -> None:
        """2-D array schema includes ``dims=[4, 8]`` and ``strides=[32, 4]``."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        from peakrdl_pybind11.runtime.schema import to_dict

        class _Reg:
            class info:
                kind = "reg"
                name = "matrix"
                path = "soc.matrix"
                address = 0x100
                regwidth = 32
                desc = None
                fields = {}

            def read(self) -> int:
                return 0

            def write(self, v: int) -> None:
                return None

        elements = [_Reg() for _ in range(32)]
        view = ArrayView(
            elements,
            shape=(4, 8),
            path="soc.matrix",
            strides=(32, 4),
            entry_type_name="matrix_t",
        )

        class _Soc:
            pass

        soc = _Soc()
        soc.matrix = view  # type: ignore[attr-defined]

        out = to_dict(soc)
        children = out.get("children", [])
        assert len(children) == 1
        node = children[0]
        assert node["dims"] == [4, 8]
        assert node["strides"] == [32, 4]
        assert node["entry"]["kind"] == "reg"


class TestPhase5WidgetsArrayTreeRendering:
    """``tree()`` renders arrays as a single line with shape suffix."""

    def test_array_tree_line_includes_shape(self) -> None:
        """``soc.tree()`` shows ``lut[8] [Array @ 0x100]`` for a 1-D array."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        from peakrdl_pybind11.runtime.widgets import tree

        class _Reg:
            class info:
                name = "lut"
                path = "soc.lut"
                address = 0x100
                regwidth = 32
                desc = None
                fields = {}

            offset = 0x100

            def read(self) -> int:
                return 0

            def write(self, v: int) -> None:
                return None

        view = ArrayView(
            [_Reg(), _Reg()],
            shape=(2,),
            path="soc.lut",
            strides=(4,),
            entry_type_name="lut_t",
        )

        class _Soc:
            class info:
                name = "soc"
                path = "soc"
                address = 0
                regwidth = None
                desc = None

        soc = _Soc()
        soc.lut = view  # type: ignore[attr-defined]
        rendered = tree(soc)
        # Shape suffix in the name, ``[Array @ ...]`` bracket.
        assert "[2]" in rendered
        assert "[Array" in rendered

    def test_array_tree_does_not_explode_entries(self) -> None:
        """Default ``tree()`` does *not* expand per-entry rows."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        from peakrdl_pybind11.runtime.widgets import tree

        class _Reg:
            class info:
                name = "lut"
                path = "soc.lut"
                address = 0x100
                regwidth = 32
                desc = None
                fields = {}

            offset = 0x100

            def read(self) -> int:
                return 0

            def write(self, v: int) -> None:
                return None

        view = ArrayView(
            [_Reg() for _ in range(8)],
            shape=(8,),
            path="soc.lut",
            strides=(4,),
        )

        class _Soc:
            class info:
                name = "soc"
                path = "soc"

        soc = _Soc()
        soc.lut = view  # type: ignore[attr-defined]
        rendered = tree(soc)
        # Should be a small number of lines (soc + array, not 8+).
        assert rendered.count("\n") <= 1, f"expected <=2 lines, got:\n{rendered}"

    def test_array_tree_show_entries_expands(self) -> None:
        """``tree(show_array_entries=True)`` expands per-entry rows."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        from peakrdl_pybind11.runtime.widgets import tree

        class _Reg:
            class info:
                name = "lut"
                path = "soc.lut"
                address = 0x100
                regwidth = 32
                desc = None
                fields = {}

            offset = 0x100

            def read(self) -> int:
                return 0

            def write(self, v: int) -> None:
                return None

        view = ArrayView(
            [_Reg(), _Reg(), _Reg()],
            shape=(3,),
            path="soc.lut",
            strides=(4,),
        )

        class _Soc:
            class info:
                name = "soc"
                path = "soc"

        soc = _Soc()
        soc.lut = view  # type: ignore[attr-defined]
        rendered = tree(soc, show_array_entries=True)
        # soc + array + 3 entries = 5 lines.
        assert rendered.count("\n") >= 3


class TestPhase5SnapshotArrayEntries:
    """Snapshot synthesizes ``soc.lut[i]`` paths for array entries."""

    def test_snapshot_includes_synthesized_array_paths(self) -> None:
        """1-D array entries appear as ``soc.lut[0]`` ... ``soc.lut[7]``."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        from peakrdl_pybind11.runtime.snapshot import take_snapshot

        # Each _Reg has its own value + info.path = "soc.lut" (the array
        # node's path -- all entries share it on the C++ side).
        class _Reg:
            def __init__(self, value: int) -> None:
                self._v = value

            class info:
                name = "lut"
                path = "soc.lut"
                address = 0x100
                regwidth = 32
                access = "rw"
                on_read = None
                fields = {}

            def peek(self) -> int:
                return self._v

            def read(self) -> int:
                return self._v

            def write(self, v: int) -> None:
                self._v = int(v)

        regs = [_Reg(value=0xAA + i) for i in range(4)]
        view = ArrayView(regs, shape=(4,), path="soc.lut", strides=(4,))

        class _Soc:
            class info:
                name = "soc"
                path = "soc"

            def walk(self):
                yield self
                yield self.lut
                yield from regs

        soc = _Soc()
        soc.lut = view  # type: ignore[attr-defined]

        snap = take_snapshot(soc)
        # Synthesized indexed paths present.
        for i in range(4):
            assert f"soc.lut[{i}]" in snap.values
            assert snap.values[f"soc.lut[{i}]"] == 0xAA + i
        # The bare "soc.lut" path is *not* present -- it would collapse
        # the 4 distinct entries onto a single key.
        assert "soc.lut" not in snap.values

    def test_snapshot_includes_synthesized_multidim_paths(self) -> None:
        """2-D entries appear as ``soc.matrix[0,0]`` ... ``soc.matrix[3,7]``."""
        from peakrdl_pybind11.runtime.arrays import ArrayView
        from peakrdl_pybind11.runtime.snapshot import take_snapshot

        class _Reg:
            def __init__(self, value: int) -> None:
                self._v = value

            class info:
                name = "matrix"
                path = "soc.matrix"
                address = 0x100
                regwidth = 32
                access = "rw"
                on_read = None
                fields = {}

            def peek(self) -> int:
                return self._v

            def read(self) -> int:
                return self._v

            def write(self, v: int) -> None:
                self._v = int(v)

        regs = [_Reg(i) for i in range(12)]
        view = ArrayView(regs, shape=(3, 4), path="soc.matrix", strides=(16, 4))

        class _Soc:
            class info:
                name = "soc"
                path = "soc"

            def walk(self):
                yield self
                yield self.matrix
                yield from regs

        soc = _Soc()
        soc.matrix = view  # type: ignore[attr-defined]

        snap = take_snapshot(soc)
        # 3*4=12 synthesized paths.
        assert "soc.matrix[0,0]" in snap.values
        assert "soc.matrix[2,3]" in snap.values
        assert sum(1 for k in snap.values if k.startswith("soc.matrix[")) == 12
