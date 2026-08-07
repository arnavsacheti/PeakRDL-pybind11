"""Focused contract tests for :mod:`benchmark_output_profiles`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .benchmark_output_profiles import (
    KIND,
    SCHEMA_VERSION,
    _collect,
    _effective_output_config,
    _worker,
)


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (
            "full",
            {
                "gen_pyi": True,
                "gen_schema": True,
                "gen_interrupts": True,
                "gen_aliases": True,
                "root_mirror": True,
            },
        ),
        (
            "compact",
            {
                "gen_pyi": True,
                "gen_schema": False,
                "gen_interrupts": True,
                "gen_aliases": True,
                "root_mirror": False,
            },
        ),
        (
            "minimal",
            {
                "gen_pyi": False,
                "gen_schema": False,
                "gen_interrupts": False,
                "gen_aliases": False,
                "root_mirror": False,
            },
        ),
    ],
)
def test_effective_output_configs_are_exact(profile: str, expected: dict[str, bool]) -> None:
    assert _effective_output_config(profile) == expected


@pytest.mark.parametrize(
    ("profile", "present", "absent"),
    [
        ("full", {"__init__.py", "schema.json", "__init__.pyi"}, set()),
        (
            "compact",
            {"output_profile_2/__init__.py", "output_profile_2/__init__.pyi"},
            {"__init__.py", "schema.json", "__init__.pyi", "output_profile_2/schema.json"},
        ),
        (
            "minimal",
            {"output_profile_2/__init__.py"},
            {
                "__init__.py",
                "schema.json",
                "__init__.pyi",
                "output_profile_2/__init__.pyi",
                "output_profile_2/schema.json",
                "output_profile_2/aliases.py",
                "output_profile_2/interrupts_detected.py",
            },
        ),
    ],
)
def test_tiny_worker_records_manifest_and_all_size_metrics(
    profile: str, present: set[str], absent: set[str]
) -> None:
    point = _worker(2, profile, registers_per_block=2)

    assert point["registers"] == 2
    assert point["fields"] == 10
    assert point["effective_output_config"] == _effective_output_config(profile)
    assert present.issubset(point["manifest"])
    assert absent.isdisjoint(point["manifest"])
    assert point["build_s"] is None
    assert point["wheel_bytes"] is None
    for category in ("total", "cpp", "python", "package", "root_mirror", "schema", "stub"):
        assert isinstance(point[f"{category}_bytes"], int)
        assert point[f"{category}_bytes"] >= 0
        assert isinstance(point[f"{category}_files"], int)
        assert point[f"{category}_files"] >= 0
    assert point["package_text_deflate_bytes_proxy"] > 0
    assert point["compile_s"] >= 0
    assert point["validate_s"] >= 0
    assert point["export_s"] >= 0
    assert point["peak_rss_mib"] > 0


def test_collection_writes_schema_v2_output_profile_matrix(tmp_path: Path) -> None:
    output = tmp_path / "matrix.json"
    payload = _collect([1], ["full", "minimal"], registers_per_block=1, build_max_registers=0, output=output)
    on_disk = json.loads(output.read_text(encoding="utf-8"))

    assert on_disk == payload
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["kind"] == KIND
    assert payload["environment"]
    benchmark = payload["benchmark"]
    assert benchmark["build_wheels_enabled"] is False
    assert benchmark["build_max_registers"] == 0
    assert benchmark["field_profile"]["id"] == "nibbles5"
    assert "not a wheel size" in benchmark["package_text_deflate_metric"]
    assert len(benchmark["shape_sha256"]) == 64
    assert [item["id"] for item in benchmark["profiles"]] == ["full", "minimal"]
    assert benchmark["profiles"][1]["effective_output_config"] == _effective_output_config("minimal")
    assert [item["profile"] for item in payload["series"]] == ["full", "minimal"]
    for series in payload["series"]:
        assert series["effective_output_config"] == _effective_output_config(series["profile"])
        assert len(series["points"]) == 1


def test_checked_output_profile_matrix_covers_100k_without_claiming_wheels() -> None:
    results_file = Path(__file__).parent / "results" / "output_profile_envelope.json"
    payload = json.loads(results_file.read_text(encoding="utf-8"))

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["kind"] == KIND
    assert isinstance(payload["environment"]["git_dirty"], bool)
    assert [item["id"] for item in payload["benchmark"]["profiles"]] == [
        "full",
        "compact",
        "minimal",
    ]

    series_by_profile = {series["profile"]: series for series in payload["series"]}
    assert list(series_by_profile) == ["full", "compact", "minimal"]
    for profile, series in series_by_profile.items():
        assert series["effective_output_config"] == _effective_output_config(profile)
        assert [point["registers"] for point in series["points"]] == [1_000, 10_000, 100_001]
        for point in series["points"]:
            assert point["fields"] == point["registers"] * 5
            assert point["wheel_bytes"] is None
            assert point["build_s"] is None

    full = series_by_profile["full"]["points"][-1]
    compact = series_by_profile["compact"]["points"][-1]
    minimal = series_by_profile["minimal"]["points"][-1]
    assert full["cpp_bytes"] == compact["cpp_bytes"] == minimal["cpp_bytes"]
    assert minimal["total_bytes"] < compact["total_bytes"] < full["total_bytes"]
    assert full["root_mirror_bytes"] > 0
    assert compact["root_mirror_bytes"] == minimal["root_mirror_bytes"] == 0
    assert full["schema_bytes"] > 0
    assert compact["schema_bytes"] == minimal["schema_bytes"] == 0
    assert full["stub_bytes"] > compact["stub_bytes"] > minimal["stub_bytes"] == 0
