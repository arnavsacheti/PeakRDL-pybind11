"""Correctness checks for the synthetic 100k-register scale envelope."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

from .benchmark_scale_envelope import _validate_region, _worker, _write_rdl


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
