#!/usr/bin/env python3
"""Measure generated-output profiles on the nibbles5 scale-envelope fixture.

Each matrix point runs in a new Python process.  This makes the reported peak
RSS comparable and prevents Python/SystemRDL caches from leaking between
profiles.  Native wheel builds are deliberately off by default: enable them
only with ``--build-max-registers`` and keep that limit below a size suitable
for the local compiler.

``package_text_deflate_bytes_proxy`` is a deterministic deflate size for text
files in the generated Python package.  It is useful for comparing text
payloads, but is explicitly *not* a wheel-size measurement.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import resource
import subprocess
import sys
import tempfile
import time
import zlib
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from systemrdl import RDLCompiler

from peakrdl_pybind11 import OutputConfig, Pybind11Exporter

# Keep the RDL fixture and its whole-region validation in one place.  This
# benchmark changes only output selection, not the generated register shape.
try:  # Package import for pytest and direct-script import for fresh workers.
    from .benchmark_scale_envelope import (
        DEFAULT_FIELD_PROFILE,
        FIELD_PROFILES,
        REPOSITORY_ROOT,
        _build_wheel,
        _environment,
        _peak_rss_mib,
        _profile_json,
        _validate_region,
        _write_rdl,
    )
except ImportError:  # pragma: no cover - exercised by the worker subprocess.
    from benchmark_scale_envelope import (
        DEFAULT_FIELD_PROFILE,
        FIELD_PROFILES,
        REPOSITORY_ROOT,
        _build_wheel,
        _environment,
        _peak_rss_mib,
        _profile_json,
        _validate_region,
        _write_rdl,
    )

SCHEMA_VERSION = 2
KIND = "output-profile-matrix"
DEFAULT_SIZES = (1_000, 10_000, 100_001)
DEFAULT_PROFILES = ("full", "compact", "minimal")
DEFAULT_REGISTERS_PER_BLOCK = 256

_CPP_SUFFIXES = (".cpp", ".hpp", ".h")
_PYTHON_SUFFIXES = (".py", ".pyi")
_TEXT_SUFFIXES = (".py", ".pyi", ".json", ".toml")
_ROOT_MIRROR_FILENAMES = {
    "__init__.py",
    "__init__.pyi",
    "aliases.py",
    "interrupts_detected.py",
    "schema.json",
}


def _effective_output_config(profile: str) -> dict[str, bool]:
    """Return every effective artifact-selection boolean for ``profile``."""
    config = OutputConfig.from_profile(profile)
    return {field.name: getattr(config, field.name) for field in dataclasses.fields(OutputConfig)}


def _manifest(output_dir: Path) -> list[str]:
    return sorted(path.relative_to(output_dir).as_posix() for path in output_dir.rglob("*") if path.is_file())


def _output_metrics(output_dir: Path, soc_name: str) -> dict[str, int]:
    """Return byte and file-count metrics for a generated output tree.

    Categories overlap intentionally: for example, a package ``.pyi`` file is
    included in package, Python, and stub totals.  This makes each category
    independently useful when comparing output profiles.
    """
    files = [path for path in output_dir.rglob("*") if path.is_file()]
    package_dir = output_dir / soc_name

    def selected(predicate: Callable[[Path], bool]) -> list[Path]:
        # ``Path`` predicates stay local to this function and keep the
        # category definitions visibly paired with their counts below.
        return [path for path in files if predicate(path)]

    categories = {
        "total": files,
        "cpp": selected(lambda path: path.suffix in _CPP_SUFFIXES),
        "python": selected(lambda path: path.suffix in _PYTHON_SUFFIXES),
        "package": selected(lambda path: path.is_relative_to(package_dir)),
        "root_mirror": selected(
            lambda path: path.parent == output_dir and path.name in _ROOT_MIRROR_FILENAMES
        ),
        "schema": selected(lambda path: path.name == "schema.json"),
        "stub": selected(lambda path: path.suffix == ".pyi"),
    }
    metrics = {
        f"{name}_bytes": sum(path.stat().st_size for path in category)
        for name, category in categories.items()
    }
    metrics.update({f"{name}_files": len(category) for name, category in categories.items()})

    package_text = bytearray()
    for path in sorted(categories["package"]):
        if path.suffix not in _TEXT_SUFFIXES:
            continue
        # Include names and separators, so two different package layouts do
        # not accidentally hash/compress as the same concatenated payload.
        package_text.extend(path.relative_to(package_dir).as_posix().encode("utf-8"))
        package_text.extend(b"\0")
        package_text.extend(path.read_bytes())
        package_text.extend(b"\0")
    metrics["package_text_deflate_bytes_proxy"] = len(zlib.compress(bytes(package_text), level=9))
    return metrics


def _worker(
    register_count: int,
    profile: str,
    registers_per_block: int,
    build: bool = False,
) -> dict:
    """Collect one profile/size point in an isolated Python process."""
    if register_count < 1:
        raise ValueError("register_count must be positive")
    if profile not in DEFAULT_PROFILES:
        raise ValueError(f"unknown output profile {profile!r}")

    output_config = OutputConfig.from_profile(profile)
    with tempfile.TemporaryDirectory(prefix=f"peakrdl-output-{profile}-{register_count}-") as temporary:
        work_dir = Path(temporary)
        rdl_file = work_dir / "scale.rdl"
        output_dir = work_dir / "output"
        soc_name = f"output_profile_{register_count}"

        started = time.perf_counter()
        with rdl_file.open("w", encoding="utf-8") as stream:
            _write_rdl(
                stream,
                register_count,
                None,
                registers_per_block,
                field_profile=DEFAULT_FIELD_PROFILE,
            )
        rdl_write_s = time.perf_counter() - started

        started = time.perf_counter()
        compiler = RDLCompiler()
        compiler.compile_file(str(rdl_file))
        root = compiler.elaborate()
        compile_s = time.perf_counter() - started

        started = time.perf_counter()
        region = _validate_region(
            root.top,
            register_count,
            None,
            field_profile=DEFAULT_FIELD_PROFILE,
        )
        validate_s = time.perf_counter() - started

        started = time.perf_counter()
        Pybind11Exporter().export(
            root.top,
            str(output_dir),
            soc_name=soc_name,
            output_config=output_config,
            split_by_hierarchy=True,
        )
        export_s = time.perf_counter() - started

        result = {
            **region,
            **_output_metrics(output_dir, soc_name),
            "profile": profile,
            "effective_output_config": _effective_output_config(profile),
            "rdl_bytes": rdl_file.stat().st_size,
            "rdl_write_s": rdl_write_s,
            "compile_s": compile_s,
            "validate_s": validate_s,
            "export_s": export_s,
            "peak_rss_mib": _peak_rss_mib(resource.RUSAGE_SELF),
            "manifest": _manifest(output_dir),
            "build_s": None,
            "build_peak_rss_mib": None,
            "wheel_bytes": None,
        }
        if build:
            result.update(_build_wheel(output_dir, work_dir))
        return result


def _run_worker(register_count: int, profile: str, registers_per_block: int, build: bool) -> dict:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        str(register_count),
        "--worker-profile",
        profile,
        "--registers-per-block",
        str(registers_per_block),
    ]
    if build:
        command.append("--build")
    worker = subprocess.run(command, capture_output=True, text=True)
    if worker.returncode != 0:
        details = "\n".join((worker.stdout, worker.stderr)).strip()
        raise RuntimeError(
            f"output-profile worker failed for {profile} at {register_count} registers:\n{details}"
        )
    return json.loads(worker.stdout)


def _profile_record(profile: str) -> dict:
    return {"id": profile, "effective_output_config": _effective_output_config(profile)}


def _collect(
    sizes: list[int],
    profiles: list[str],
    registers_per_block: int,
    build_max_registers: int,
    output: Path,
) -> dict:
    series = []
    for profile in profiles:
        points = []
        for register_count in sizes:
            point = _run_worker(
                register_count,
                profile,
                registers_per_block,
                build=build_max_registers > 0 and register_count <= build_max_registers,
            )
            points.append(point)
            print(
                f"collected {profile}: {point['registers']:,} registers / {point['fields']:,} fields",
                file=sys.stderr,
            )
        series.append(
            {
                "profile": profile,
                "effective_output_config": _effective_output_config(profile),
                "points": points,
            }
        )

    benchmark = {
        "sizes": sizes,
        "field_profile": _profile_json(FIELD_PROFILES[DEFAULT_FIELD_PROFILE]),
        "registers_per_block": registers_per_block,
        "build_max_registers": build_max_registers,
        "build_wheels_enabled": build_max_registers > 0,
        "profiles": [_profile_record(profile) for profile in profiles],
        "package_text_deflate_metric": (
            "package_text_deflate_bytes_proxy: deflate-compressed package text; not a wheel size"
        ),
    }
    benchmark["shape_sha256"] = hashlib.sha256(json.dumps(benchmark, sort_keys=True).encode()).hexdigest()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": benchmark,
        "environment": _environment(),
        "series": series,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=DEFAULT_SIZES)
    parser.add_argument("--profiles", nargs="+", choices=DEFAULT_PROFILES, default=DEFAULT_PROFILES)
    parser.add_argument("--registers-per-block", type=int, default=DEFAULT_REGISTERS_PER_BLOCK)
    parser.add_argument(
        "--build-max-registers",
        type=int,
        default=0,
        help="build wheels only through this size (zero, the default, disables native builds)",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--worker-profile", choices=DEFAULT_PROFILES, help=argparse.SUPPRESS)
    parser.add_argument("--build", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if any(size < 1 for size in args.sizes):
        parser.error("--sizes values must be positive")
    if args.registers_per_block < 1:
        parser.error("--registers-per-block must be positive")
    if args.build_max_registers < 0:
        parser.error("--build-max-registers cannot be negative")
    if len(set(args.profiles)) != len(args.profiles):
        parser.error("--profiles cannot contain duplicates")

    if args.worker is not None:
        if args.worker_profile is None:
            parser.error("--worker-profile is required with --worker")
        if args.output is not None or args.build_max_registers:
            parser.error("--output and --build-max-registers are not valid for a worker")
        print(json.dumps(_worker(args.worker, args.worker_profile, args.registers_per_block, args.build)))
        return 0
    if args.worker_profile is not None or args.build:
        parser.error("--worker-profile and --build are internal worker options")

    output = args.output or (REPOSITORY_ROOT / "benchmarks" / "results" / "output_profile_envelope.json")
    _collect(args.sizes, args.profiles, args.registers_per_block, args.build_max_registers, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
