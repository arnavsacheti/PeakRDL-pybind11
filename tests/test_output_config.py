"""Focused tests for generated-output profiles, flags, and manifests."""

from __future__ import annotations

import argparse
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest
from peakrdl.config.loader import load_cfg
from systemrdl import RDLCompiler
from systemrdl.node import AddrmapNode

from peakrdl_pybind11 import OutputConfig, Pybind11Exporter
from peakrdl_pybind11.__peakrdl__ import Exporter
from peakrdl_pybind11.exporter_plugins import PluginContext
from peakrdl_pybind11.exporter_plugins.feature_detection import FeatureDetectionPlugin

SIMPLE_RDL = """
addrmap output_soc {
    reg {
        field { sw = rw; hw = r; } enable[0:0];
    } control @ 0x0;
};
"""


def _compile(tmp_path: Path) -> AddrmapNode:
    source = tmp_path / "output.rdl"
    source.write_text(SIMPLE_RDL, encoding="utf-8")
    compiler = RDLCompiler()
    compiler.compile_file(str(source))
    return compiler.elaborate().top


def _manifest(path: Path) -> set[str]:
    return {item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    Exporter().add_exporter_arguments(parser)
    return parser


def test_output_config_profiles_are_immutable_and_runtime_safe() -> None:
    assert OutputConfig.full() == OutputConfig()
    assert OutputConfig.compact() == OutputConfig(
        gen_pyi=True,
        gen_schema=False,
        gen_interrupts=True,
        gen_aliases=True,
        root_mirror=False,
    )
    assert OutputConfig.minimal() == OutputConfig(
        gen_pyi=False,
        gen_schema=False,
        gen_interrupts=False,
        gen_aliases=False,
        root_mirror=False,
    )
    with pytest.raises(FrozenInstanceError):
        OutputConfig.full().gen_schema = False  # type: ignore[misc]
    with pytest.raises(ValueError, match="unknown output profile"):
        OutputConfig.from_profile("tiny")


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], (None, None, None, None, None, None)),
        (
            [
                "--output-profile",
                "minimal",
                "--gen-pyi",
                "--no-gen-schema",
                "--gen-interrupts",
                "--no-gen-aliases",
                "--root-mirror",
            ],
            ("minimal", True, False, True, False, True),
        ),
        (
            [
                "--no-gen-pyi",
                "--gen-schema",
                "--no-gen-interrupts",
                "--gen-aliases",
                "--no-root-mirror",
            ],
            (None, False, True, False, True, False),
        ),
    ],
)
def test_cli_parser_supports_symmetric_output_flags(
    args: list[str],
    expected: tuple[str | None, bool | None, bool | None, bool | None, bool | None, bool | None],
) -> None:
    ns = _parser().parse_args(args)
    assert (
        ns.output_profile,
        ns.gen_pyi,
        ns.gen_schema,
        ns.gen_interrupts,
        ns.gen_aliases,
        ns.root_mirror,
    ) == expected


def test_native_peakrdl_config_and_cli_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = tmp_path / "peakrdl.toml"
    cfg_path.write_text(
        """
[pybind11]
output_profile = "compact"
gen_schema = true
gen_aliases = false
root_mirror = true
""",
        encoding="utf-8",
    )
    plugin = Exporter()
    plugin._load_cfg(load_cfg(str(cfg_path)))

    calls: list[dict[str, object]] = []

    class CapturingExporter:
        def export(self, _top: object, _output: str, **kwargs: object) -> None:
            calls.append(kwargs)

    import peakrdl_pybind11.__peakrdl__ as entrypoint
    import peakrdl_pybind11.cli as cli

    monkeypatch.setattr(entrypoint, "Pybind11Exporter", CapturingExporter)
    monkeypatch.setattr(entrypoint._cli, "try_handle", lambda _options: False)
    monkeypatch.setattr(cli, "run_handlers", lambda _options: None)

    options = _parser().parse_args(["--no-gen-pyi", "--strict-fields=false"])
    options.output = str(tmp_path / "out")
    plugin.do_export(SimpleNamespace(inst_name="output_soc"), options)
    assert calls[-1]["output_profile"] == "compact"
    assert calls[-1]["gen_pyi"] is False  # explicit CLI beats TOML/profile
    assert calls[-1]["gen_schema"] is True  # TOML artifact beats TOML profile
    assert calls[-1]["gen_aliases"] is False
    assert calls[-1]["root_mirror"] is True
    assert calls[-1]["strict_fields"] is False

    # An explicit CLI profile replaces the TOML output selection as a unit;
    # an explicit artifact flag can then refine that CLI profile.
    options = _parser().parse_args(["--output-profile", "minimal", "--gen-interrupts"])
    options.output = str(tmp_path / "out2")
    plugin.do_export(SimpleNamespace(inst_name="output_soc"), options)
    assert calls[-1]["output_profile"] == "minimal"
    assert calls[-1]["gen_schema"] is None
    assert calls[-1]["gen_aliases"] is None
    assert calls[-1]["root_mirror"] is None
    assert calls[-1]["gen_interrupts"] is True
    assert calls[-1]["strict_fields"] is None


def test_feature_plugin_none_output_config_falls_back_to_full(tmp_path: Path) -> None:
    top = _compile(tmp_path)
    output_dir = tmp_path / "plugin"
    context = PluginContext(
        exporter=SimpleNamespace(),
        top_node=top,
        output_dir=output_dir,
        soc_name="output_soc",
        nodes={"regs": []},
        options={"output_config": None},
    )

    FeatureDetectionPlugin().post_export(context)

    expected = {
        "aliases.py",
        "interrupts_detected.py",
        "schema.json",
        "output_soc/aliases.py",
        "output_soc/interrupts_detected.py",
        "output_soc/schema.json",
    }
    assert expected.issubset(_manifest(output_dir))


@pytest.mark.parametrize(
    ("profile", "present", "absent"),
    [
        (
            "full",
            {
                "__init__.py",
                "__init__.pyi",
                "aliases.py",
                "interrupts_detected.py",
                "schema.json",
                "output_soc/__init__.py",
                "output_soc/__init__.pyi",
                "output_soc/aliases.py",
                "output_soc/interrupts_detected.py",
                "output_soc/schema.json",
            },
            set(),
        ),
        (
            "compact",
            {
                "output_soc/__init__.py",
                "output_soc/__init__.pyi",
                "output_soc/aliases.py",
                "output_soc/interrupts_detected.py",
            },
            {
                "__init__.py",
                "__init__.pyi",
                "aliases.py",
                "interrupts_detected.py",
                "schema.json",
                "output_soc/schema.json",
            },
        ),
        (
            "minimal",
            {"output_soc/__init__.py"},
            {
                "__init__.py",
                "__init__.pyi",
                "aliases.py",
                "interrupts_detected.py",
                "schema.json",
                "output_soc/__init__.pyi",
                "output_soc/aliases.py",
                "output_soc/interrupts_detected.py",
                "output_soc/schema.json",
            },
        ),
    ],
)
def test_export_profile_manifests(tmp_path: Path, profile: str, present: set[str], absent: set[str]) -> None:
    top = _compile(tmp_path)
    out = tmp_path / profile
    Pybind11Exporter().export(top, str(out), soc_name="output_soc", output_profile=profile)
    manifest = _manifest(out)

    # Functional build output is invariant across profiles.
    assert {
        "output_soc_descriptors.hpp",
        "output_soc_bindings.cpp",
        "CMakeLists.txt",
        "pyproject.toml",
    }.issubset(manifest)
    assert present.issubset(manifest)
    assert absent.isdisjoint(manifest)


def test_programmatic_overrides_and_output_config_object(tmp_path: Path) -> None:
    top = _compile(tmp_path)
    out = tmp_path / "overrides"
    Pybind11Exporter().export(
        top,
        str(out),
        soc_name="output_soc",
        output_config=OutputConfig.minimal(),
        gen_schema=True,
        gen_pyi=True,
    )
    manifest = _manifest(out)
    assert "output_soc/__init__.pyi" in manifest
    assert "output_soc/schema.json" in manifest
    assert "output_soc/aliases.py" not in manifest
    assert "output_soc/interrupts_detected.py" not in manifest
    assert "__init__.py" not in manifest


def test_smaller_profile_removes_stale_optional_outputs(tmp_path: Path) -> None:
    top = _compile(tmp_path)
    out = tmp_path / "reused"
    exporter = Pybind11Exporter()
    exporter.export(top, str(out), soc_name="output_soc", output_profile="full")
    assert "schema.json" in _manifest(out)

    exporter.export(top, str(out), soc_name="output_soc", output_profile="minimal")
    manifest = _manifest(out)
    assert "output_soc/__init__.py" in manifest
    assert not any(
        relative.endswith((".pyi", "schema.json", "aliases.py", "interrupts_detected.py"))
        for relative in manifest
    )
    assert "__init__.py" not in manifest


def test_profile_cleanup_preserves_files_in_unowned_directories(tmp_path: Path) -> None:
    top = _compile(tmp_path)
    out = tmp_path / "shared"
    pkg = out / "output_soc"
    pkg.mkdir(parents=True)
    custom_files = {
        out / "__init__.py": "# user-owned root package\n",
        out / "schema.json": '{"owner": "user"}\n',
        out / "aliases.py": "# user aliases\n",
        pkg / "__init__.py": "# user-owned package\n",
        pkg / "schema.json": '{"owner": "user package"}\n',
        pkg / "interrupts_detected.py": "# user interrupts\n",
    }
    for path, content in custom_files.items():
        path.write_text(content, encoding="utf-8")

    Pybind11Exporter().export(
        top,
        str(out),
        soc_name="output_soc",
        output_profile="minimal",
    )

    # The functional package runtime is the exporter's normal destination and
    # is replaced. Optional files and the unused root runtime were not proven
    # exporter-owned at cleanup time, so their user content survives.
    assert "Generated by PeakRDL-pybind11" in (pkg / "__init__.py").read_text(encoding="utf-8")
    for path, content in custom_files.items():
        if path == pkg / "__init__.py":
            continue
        assert path.read_text(encoding="utf-8") == content


def test_default_manifest_and_bytes_equal_explicit_full(tmp_path: Path) -> None:
    top = _compile(tmp_path)
    default_out = tmp_path / "default"
    full_out = tmp_path / "full"
    Pybind11Exporter().export(top, str(default_out), soc_name="output_soc")
    Pybind11Exporter().export(
        top,
        str(full_out),
        soc_name="output_soc",
        output_config=OutputConfig.full(),
    )
    assert _manifest(default_out) == _manifest(full_out)
    for relative in _manifest(default_out):
        assert (default_out / relative).read_bytes() == (full_out / relative).read_bytes()


def test_legacy_strict_fields_attribute_is_preserved(tmp_path: Path) -> None:
    top = _compile(tmp_path)
    out = tmp_path / "loose"
    exporter = Pybind11Exporter()
    exporter.strict_fields = False
    exporter.export(top, str(out), soc_name="output_soc", output_profile="minimal")
    runtime = (out / "output_soc" / "__init__.py").read_text(encoding="utf-8")
    assert "_PEAKRDL_STRICT_FIELDS: bool = False" in runtime


def test_disabled_feature_artifacts_skip_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import peakrdl_pybind11.exporter_plugins.feature_detection as feature_detection

    top = _compile(tmp_path)

    def unexpected(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("disabled feature detection should not run")

    monkeypatch.setattr(feature_detection, "detect_interrupt_groups", unexpected)
    monkeypatch.setattr(feature_detection, "detect_aliases", unexpected)
    monkeypatch.setattr(feature_detection, "build_schema", unexpected)
    Pybind11Exporter().export(
        top,
        str(tmp_path / "minimal"),
        soc_name="output_soc",
        output_profile="minimal",
    )
