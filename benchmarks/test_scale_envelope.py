"""Correctness checks for the synthetic 100k-register scale envelope."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

from .benchmark_scale_envelope import (
    FIELD_PROFILES,
    _expected_field_count,
    _parser,
    _validate_region,
    _worker,
    _write_rdl,
)

EXPECTED_100K_PROFILE_FIELDS = {
    "word32": 100_001,
    "bytes4": 400_004,
    "mixed-1-8-4": 433_338,
    "nibbles5": 500_005,
    "flags8-packed": 800_008,
    "flags8-spread": 800_008,
}


def test_checked_scale_results_cover_100k_region() -> None:
    results_file = Path(__file__).parent / "results" / "scale_envelope.json"
    payload = json.loads(results_file.read_text())
    points = payload["points"]

    assert [point["registers"] for point in points] == [
        1_000,
        5_000,
        10_000,
        25_000,
        50_000,
        75_000,
        100_001,
    ]
    for point in points:
        assert point["fields"] == point["registers"] * 5
        assert point["first_address"] == 0
        assert point["last_address"] == (point["registers"] - 1) * 4
        assert point["region_bytes"] == point["registers"] * 4

    assert points[-1]["fields"] == 500_005
    assert points[-1]["binding_chunks"] == 391


def test_checked_sparse_results_span_2tib() -> None:
    results_file = Path(__file__).parent / "results" / "sparse_scale_envelope.json"
    payload = json.loads(results_file.read_text())
    points = payload["points"]
    max_address = 0x200_0000_0000

    assert [point["registers"] for point in points] == [1_000, 10_000, 100_001]
    for point in points:
        assert point["fields"] == point["registers"] * 5
        assert point["first_address"] == 0
        assert point["last_address"] == max_address
        assert point["region_bytes"] == max_address + 4
        assert point["occupied_bytes"] == point["registers"] * 4

    assert points[-1]["fields"] == 500_005
    assert points[-1]["address_density"] < 0.000001


def test_checked_field_profile_matrix_covers_all_100k_shapes() -> None:
    results_file = Path(__file__).parent / "results" / "field_profile_envelope.json"
    payload = json.loads(results_file.read_text())

    assert payload["schema_version"] == 2
    assert payload["kind"] == "field-profile-matrix"
    assert isinstance(payload["environment"]["git_dirty"], bool)
    profiles = payload["benchmark"]["profiles"]
    assert [profile["id"] for profile in profiles] == list(EXPECTED_100K_PROFILE_FIELDS)
    profile_metadata = {profile["id"]: profile for profile in profiles}
    packed_fields = profile_metadata["flags8-packed"]["layouts"][0]["fields"]
    spread_fields = profile_metadata["flags8-spread"]["layouts"][0]["fields"]
    assert [field["lsb"] for field in packed_fields] == list(range(8))
    assert [field["lsb"] for field in spread_fields] == list(range(0, 32, 4))

    assert [series["field_profile"] for series in payload["series"]] == list(EXPECTED_100K_PROFILE_FIELDS)
    for series in payload["series"]:
        profile_id = series["field_profile"]
        points = series["points"]
        assert [point["registers"] for point in points] == [1_000, 10_000, 100_001]
        for point in points:
            assert point["field_profile"] == profile_id
            assert point["fields"] == _expected_field_count(FIELD_PROFILES[profile_id], point["registers"])
            assert point["first_address"] == 0
            assert point["last_address"] == (point["registers"] - 1) * 4
            assert point["wheel_bytes"] is None
        assert points[-1]["fields"] == EXPECTED_100K_PROFILE_FIELDS[profile_id]
        assert points[-1]["binding_chunks"] == 391


@pytest.mark.parametrize(("profile_id", "expected_fields"), EXPECTED_100K_PROFILE_FIELDS.items())
def test_profile_field_totals_at_100k(profile_id: str, expected_fields: int) -> None:
    assert _expected_field_count(FIELD_PROFILES[profile_id], 100_001) == expected_fields


@pytest.mark.scaling
@pytest.mark.parametrize(
    ("profile_id", "expected_fields"),
    (
        ("nibbles5", 35),
        ("word32", 7),
        ("bytes4", 28),
        ("mixed-1-8-4", 27),
        ("flags8-packed", 56),
        ("flags8-spread", 56),
    ),
)
def test_field_profiles_compile_with_exact_layouts(
    tmp_path: Path, profile_id: str, expected_fields: int
) -> None:
    rdl_file = tmp_path / "profile.rdl"
    with rdl_file.open("w", encoding="utf-8") as stream:
        _write_rdl(
            stream,
            register_count=7,
            fields_per_register=None,
            registers_per_block=4,
            field_profile=profile_id,
        )

    compiler = RDLCompiler()
    compiler.compile_file(str(rdl_file))
    root = compiler.elaborate()
    region = _validate_region(
        root.top,
        register_count=7,
        fields_per_register=None,
        field_profile=profile_id,
    )

    assert region["fields"] == expected_fields
    assert region["field_profile"] == profile_id
    assert sum(region["layout_counts"].values()) == 7


@pytest.mark.scaling
def test_field_layout_validation_rejects_different_bit_positions(tmp_path: Path) -> None:
    rdl_file = tmp_path / "packed_flags.rdl"
    with rdl_file.open("w", encoding="utf-8") as stream:
        _write_rdl(
            stream,
            register_count=3,
            fields_per_register=None,
            registers_per_block=3,
            field_profile="flags8-packed",
        )

    compiler = RDLCompiler()
    compiler.compile_file(str(rdl_file))
    root = compiler.elaborate()
    with pytest.raises(RuntimeError, match="expected field layout 'flags8-spread'"):
        _validate_region(
            root.top,
            register_count=3,
            fields_per_register=None,
            field_profile="flags8-spread",
        )


def test_field_profile_cli_conflicts_with_legacy_field_count() -> None:
    parser = _parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--fields-per-register",
                "5",
                "--field-profiles",
                "word32",
            ]
        )


@pytest.mark.scaling
def test_scale_fixture_covers_entire_contiguous_region(tmp_path: Path) -> None:
    register_count = 257
    fields_per_register = 5
    rdl_file = tmp_path / "scale.rdl"
    with rdl_file.open("w", encoding="utf-8") as stream:
        _write_rdl(stream, register_count, fields_per_register, registers_per_block=256)

    compiler = RDLCompiler()
    compiler.compile_file(str(rdl_file))
    root = compiler.elaborate()
    region = _validate_region(root.top, register_count, fields_per_register)

    output_dir = tmp_path / "output"
    Pybind11Exporter().export(
        root.top,
        str(output_dir),
        soc_name="scale_fixture",
        split_by_hierarchy=True,
    )

    assert region == {
        "registers": 257,
        "fields": 1_285,
        "first_address": 0,
        "last_address": 1_024,
        "region_bytes": 1_028,
        "occupied_bytes": 1_028,
        "address_density": 1.0,
    }
    assert len(list(output_dir.glob("*_bindings_*.cpp"))) == 2


@pytest.mark.scaling
@pytest.mark.slow
@pytest.mark.stress
def test_export_100k_registers_500k_fields() -> None:
    if os.environ.get("PEAKRDL_RUN_100K_STRESS") != "1":
        pytest.skip("set PEAKRDL_RUN_100K_STRESS=1 to run the multi-GiB export")

    result = _worker(
        register_count=100_001,
        fields_per_register=5,
        registers_per_block=256,
        build=False,
        gen_pyi=True,
        max_address=None,
    )
    assert result["registers"] == 100_001
    assert result["fields"] == 500_005
    assert result["first_address"] == 0
    assert result["last_address"] == 400_000
    assert result["region_bytes"] == 400_004
    assert result["binding_chunks"] == 391


@pytest.mark.scaling
def test_sparse_region_reaches_2tib_and_tracks_only_occupied_bytes() -> None:
    max_address = 0x200_0000_0000
    result = _worker(
        register_count=257,
        fields_per_register=5,
        registers_per_block=256,
        build=False,
        gen_pyi=True,
        max_address=max_address,
    )

    assert result["registers"] == 257
    assert result["fields"] == 1_285
    assert result["first_address"] == 0
    assert result["last_address"] == max_address
    assert result["region_bytes"] == max_address + 4
    assert result["occupied_bytes"] == 1_028


@pytest.mark.scaling
def test_mixed_field_profile_reaches_2tib_with_exact_layout_counts() -> None:
    max_address = 0x200_0000_0000
    result = _worker(
        register_count=257,
        fields_per_register=None,
        registers_per_block=256,
        build=False,
        gen_pyi=False,
        max_address=max_address,
        field_profile="mixed-1-8-4",
    )

    assert result["registers"] == 257
    assert result["fields"] == 1_114
    assert result["field_bits"] == 6_160
    assert result["layout_counts"] == {
        "word32": 86,
        "flags8-packed": 86,
        "bytes4": 85,
    }
    assert result["first_address"] == 0
    assert result["last_address"] == max_address
    assert result["region_bytes"] == max_address + 4
