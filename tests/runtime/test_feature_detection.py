"""Tests for the feature_detection exporter plugin (Unit 23)."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

try:
    from peakrdl_pybind11 import Pybind11Exporter
    from peakrdl_pybind11.exporter_plugins import feature_detection
except ImportError:  # pragma: no cover
    pytest.skip("peakrdl_pybind11 not installed", allow_module_level=True)


INTR_RDL = """
addrmap intr_soc {
    name = "Interrupt SoC";

    reg {
        name = "Interrupt status";
        field { sw=rw; hw=w; intr; } tx_done[0:0];
        field { sw=rw; hw=w; intr; } rx_overflow[1:1];
        field { sw=rw; hw=w; intr; } error[2:2];
    } INTR_STATE @ 0x0;

    reg {
        name = "Interrupt enable";
        field { sw=rw; hw=r; } tx_done[0:0];
        field { sw=rw; hw=r; } rx_overflow[1:1];
        field { sw=rw; hw=r; } error[2:2];
    } INTR_ENABLE @ 0x4;

    reg {
        name = "Interrupt test";
        field { sw=rw; hw=r; } tx_done[0:0];
        field { sw=rw; hw=r; } rx_overflow[1:1];
        field { sw=rw; hw=r; } error[2:2];
    } INTR_TEST @ 0x8;
};
"""

ALIAS_RDL = """
addrmap alias_soc {
    reg ctrl_t {
        field { sw=rw; hw=r; } enable[0:0];
        field { sw=rw; hw=r; } mode[2:1];
    };
    ctrl_t control @ 0x0;
    alias control ctrl_t control_alt @ 0x10;
};
"""

SCHEMA_RDL = """
addrmap schema_soc {
    name = "Schema SoC";
    desc = "Drives schema.json shape tests";
    regfile uart {
        reg {
            name = "Control";
            field { sw=rw; hw=r; } enable[0:0];
            field { sw=rw; hw=r; } baudrate[3:1];
        } control @ 0x00;
        reg {
            name = "Status";
            field { sw=r; hw=w; } tx_ready[0:0];
        } status @ 0x04;
    } uart @ 0x1000;
};
"""

NO_TRIO_RDL = """
addrmap empty_soc {
    reg {
        field { sw=rw; hw=r; } enable[0:0];
    } control @ 0x0;
};
"""

LOWERCASE_INTR_RDL = """
addrmap lower_soc {
    reg {
        field { sw=rw; hw=w; intr; } a[0:0];
        field { sw=rw; hw=w; intr; } b[1:1];
    } intr_status @ 0x0;
    reg {
        field { sw=rw; hw=r; } a[0:0];
        field { sw=rw; hw=r; } b[1:1];
    } intr_enable @ 0x4;
    reg {
        field { sw=rw; hw=r; } a[0:0];
        field { sw=rw; hw=r; } b[1:1];
    } intr_test @ 0x8;
};
"""


def _compile(rdl_src: str) -> object:
    """Compile RDL source from a string and return the elaborated root."""
    rdl = RDLCompiler()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".rdl", delete=False) as f:
        f.write(rdl_src)
        fn = f.name
    try:
        rdl.compile_file(fn)
        return rdl.elaborate()
    finally:
        os.unlink(fn)


def _export(rdl_src: str, soc_name: str, **kw: object) -> Path:
    """Export ``rdl_src`` into a fresh temp dir and return its path."""
    root = _compile(rdl_src)
    tmpdir = Path(tempfile.mkdtemp(prefix="feat_detect_"))
    Pybind11Exporter().export(root.top, str(tmpdir), soc_name=soc_name, **kw)
    return tmpdir


def _load_module(path: Path, name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# detect_interrupt_groups
# ---------------------------------------------------------------------------


class TestInterruptDetection:
    def test_detects_full_trio(self) -> None:
        out = _export(INTR_RDL, "intr_soc")
        intr_path = out / "interrupts_detected.py"
        assert intr_path.exists()
        module = _load_module(intr_path, "intr_emit")
        groups = module.interrupt_groups
        assert len(groups) == 1
        g = groups[0]
        assert g["state_reg"].endswith("INTR_STATE")
        assert g["enable_reg"].endswith("INTR_ENABLE")
        assert g["test_reg"].endswith("INTR_TEST")
        assert sorted(g["sources"]) == ["error", "rx_overflow", "tx_done"]

    def test_detects_lowercase_trio(self) -> None:
        out = _export(LOWERCASE_INTR_RDL, "lower_soc")
        module = _load_module(out / "interrupts_detected.py", "lower_emit")
        groups = module.interrupt_groups
        assert len(groups) == 1
        g = groups[0]
        assert g["state_reg"].endswith("intr_status")
        assert g["enable_reg"].endswith("intr_enable")
        assert g["test_reg"].endswith("intr_test")
        assert sorted(g["sources"]) == ["a", "b"]

    def test_emits_empty_list_when_no_trio(self) -> None:
        out = _export(NO_TRIO_RDL, "empty_soc")
        module = _load_module(out / "interrupts_detected.py", "empty_emit")
        assert module.interrupt_groups == []

    def test_pattern_string_override(self) -> None:
        # A status register that doesn't match the default pattern but
        # we override with --interrupt-pattern.
        rdl_src = """
        addrmap custom_soc {
            reg {
                field { sw=rw; hw=w; intr; } x[0:0];
            } MY_FLAGS @ 0x0;
            reg {
                field { sw=rw; hw=r; } x[0:0];
            } MY_ENABLE @ 0x4;
        };
        """
        out = _export(rdl_src, "custom_soc", interrupt_pattern=r"MY_FLAGS")
        module = _load_module(out / "interrupts_detected.py", "custom_emit")
        groups = module.interrupt_groups
        # ``MY_FLAGS`` doesn't end in STATE/STATUS so partner-matching
        # falls back; we still report the group with the state register
        # alone if no partners match. Sources come from the state reg.
        assert len(groups) == 1
        assert groups[0]["state_reg"].endswith("MY_FLAGS")

    def test_invalid_pattern_type_raises(self) -> None:
        with pytest.raises(TypeError):
            feature_detection._normalise_pattern(123)

    def test_pattern_callable_override(self) -> None:
        out = _export(
            INTR_RDL,
            "intr_soc",
            interrupt_pattern=lambda name: name == "INTR_STATE",
        )
        module = _load_module(out / "interrupts_detected.py", "intr_callable_emit")
        assert len(module.interrupt_groups) == 1


# ---------------------------------------------------------------------------
# detect_aliases
# ---------------------------------------------------------------------------


class TestAliasDetection:
    def test_records_alias(self) -> None:
        out = _export(ALIAS_RDL, "alias_soc")
        aliases_path = out / "aliases.py"
        assert aliases_path.exists()
        module = _load_module(aliases_path, "alias_emit")
        aliases = module.aliases
        assert len(aliases) == 1
        (alt_path, target_path), = aliases.items()
        assert alt_path.endswith("control_alt")
        assert target_path.endswith("control")
        assert "control_alt" not in target_path  # primary, not the alias itself

    def test_empty_aliases_when_none(self) -> None:
        out = _export(SCHEMA_RDL, "schema_soc")
        module = _load_module(out / "aliases.py", "noalias_emit")
        assert module.aliases == {}


# ---------------------------------------------------------------------------
# schema.json
# ---------------------------------------------------------------------------


class TestSchema:
    def test_schema_has_expected_paths_and_addresses(self) -> None:
        out = _export(SCHEMA_RDL, "schema_soc")
        schema_path = out / "schema.json"
        assert schema_path.exists()
        data = json.loads(schema_path.read_text())
        assert data["version"] == 1
        soc = data["soc"]
        assert soc["kind"] == "addrmap"
        assert soc["inst_name"] == "schema_soc"

        # Walk the tree to find the uart.control register.
        def _find(node: dict, path: str) -> dict | None:
            if node.get("path") == path:
                return node
            for child in node.get("children", []) or []:
                hit = _find(child, path)
                if hit is not None:
                    return hit
            return None

        ctrl = _find(soc, "schema_soc.uart.control")
        assert ctrl is not None
        assert ctrl["kind"] == "reg"
        assert ctrl["absolute_address"] == 0x1000
        field_names = [f["inst_name"] for f in ctrl["fields"]]
        assert "enable" in field_names
        assert "baudrate" in field_names

    def test_schema_round_trips(self) -> None:
        out = _export(INTR_RDL, "intr_soc")
        # Should be valid JSON parsable end-to-end.
        data = json.loads((out / "schema.json").read_text())
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# stubs enrichment
# ---------------------------------------------------------------------------


class TestStubsEnrichment:
    def test_stubs_contain_typed_dict_and_unpack(self) -> None:
        out = _export(INTR_RDL, "intr_soc")
        stubs = (out / "__init__.pyi").read_text()
        assert "TypedDict" in stubs
        assert "Unpack[" in stubs
        # Field-level Annotated[int, Range(0, ...)] for bounded fields.
        assert "Annotated[int, Range(" in stubs
        # The block delimiters are present.
        assert "BEGIN feature_detection" in stubs
        assert "END   feature_detection" in stubs

    def test_stubs_idempotent(self) -> None:
        out = _export(INTR_RDL, "intr_soc")
        path = out / "__init__.pyi"
        first = path.read_text()
        # Re-run plugin manually; should produce identical output
        # (our block strip-and-replace is idempotent).
        from peakrdl_pybind11.exporter_plugins import feature_detection as fd

        rdl_root = _compile(INTR_RDL)
        # Re-export to populate exporter state, then call plugin again.
        exporter = Pybind11Exporter()
        exporter.export(rdl_root.top, str(out), soc_name="intr_soc")
        second = path.read_text()
        # Counts of begin markers stay at exactly one.
        assert first.count(fd._STUBS_BEGIN) == 1
        assert second.count(fd._STUBS_BEGIN) == 1


# ---------------------------------------------------------------------------
# Output layout
# ---------------------------------------------------------------------------


def test_outputs_appear_in_package_dir_too() -> None:
    out = _export(INTR_RDL, "intr_soc")
    pkg = out / "intr_soc"
    assert (pkg / "interrupts_detected.py").exists()
    assert (pkg / "aliases.py").exists()
    assert (pkg / "schema.json").exists()


def test_plugin_discovery_includes_feature_detection() -> None:
    from peakrdl_pybind11.exporter_plugins import discover_plugins

    plugins = discover_plugins()
    assert any(
        type(p).__name__ == "FeatureDetectionPlugin" for p in plugins
    ), [type(p).__name__ for p in plugins]
