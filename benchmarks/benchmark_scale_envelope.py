#!/usr/bin/env python3
"""Stress the current exporter across 1k to 100k+ unique registers.

Each register contains five fields by default, so the largest default point is
100,001 registers / 500,005 fields. ``--field-profiles`` instead compares exact
one-word, byte, packed/spread flag, and mixed layouts in a separate schema-v2
matrix. Registers are contiguous by default or can be spread over a sparse
address span with ``--max-address``. Regfiles only give hierarchical binding
splitting realistic compilation units. Every point runs in a fresh worker
process so peak RSS is comparable and temporary generated sources are removed
after measurement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TextIO

from systemrdl import RDLCompiler
from systemrdl.node import RegNode

from peakrdl_pybind11 import Pybind11Exporter

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIZES = (1_000, 5_000, 10_000, 25_000, 50_000, 75_000, 100_001)


@dataclass(frozen=True)
class FieldSpec:
    """One expected field in a synthetic 32-bit register layout."""

    name: str
    lsb: int
    width: int


@dataclass(frozen=True)
class LayoutSpec:
    """A reusable SystemRDL register type."""

    id: str
    label: str
    fields: tuple[FieldSpec, ...]


@dataclass(frozen=True)
class ProfileSpec:
    """A deterministic cycle of register layouts."""

    id: str
    label: str
    cycle: tuple[LayoutSpec, ...]


WORD32 = LayoutSpec("word32", "one 32-bit field", (FieldSpec("value", 0, 32),))
BYTES4 = LayoutSpec(
    "bytes4",
    "four packed 8-bit fields",
    tuple(FieldSpec(f"byte{index}", index * 8, 8) for index in range(4)),
)
FLAGS8_PACKED = LayoutSpec(
    "flags8-packed",
    "eight packed 1-bit flags",
    tuple(FieldSpec(f"flag{index}", index, 1) for index in range(8)),
)
FLAGS8_SPREAD = LayoutSpec(
    "flags8-spread",
    "eight 1-bit flags spread across the word",
    tuple(FieldSpec(f"flag{index}", index * 4, 1) for index in range(8)),
)
NIBBLES5 = LayoutSpec(
    "nibbles5",
    "five packed 4-bit fields",
    tuple(FieldSpec(f"f{index}", index * 4, 4) for index in range(5)),
)

FIELD_PROFILES = {
    "nibbles5": ProfileSpec("nibbles5", "five 4-bit fields (baseline)", (NIBBLES5,)),
    "word32": ProfileSpec("word32", "one 32-bit field", (WORD32,)),
    "bytes4": ProfileSpec("bytes4", "four 8-bit fields", (BYTES4,)),
    "mixed-1-8-4": ProfileSpec(
        "mixed-1-8-4",
        "repeating one-field, eight-flag, four-byte layouts",
        (WORD32, FLAGS8_PACKED, BYTES4),
    ),
    "flags8-packed": ProfileSpec("flags8-packed", "eight packed 1-bit flags", (FLAGS8_PACKED,)),
    "flags8-spread": ProfileSpec(
        "flags8-spread",
        "eight 1-bit flags spread across the word",
        (FLAGS8_SPREAD,),
    ),
}
DEFAULT_FIELD_PROFILE = "nibbles5"


def _validate_profile_spec(profile: ProfileSpec) -> None:
    if not profile.cycle:
        raise ValueError(f"field profile {profile.id!r} has no register layouts")

    layouts: dict[str, LayoutSpec] = {}
    for layout in profile.cycle:
        previous = layouts.setdefault(layout.id, layout)
        if previous != layout:
            raise ValueError(f"field profile {profile.id!r} reuses layout ID {layout.id!r}")
        if not layout.fields:
            raise ValueError(f"register layout {layout.id!r} has no fields")

        names: set[str] = set()
        occupied = 0
        for field in layout.fields:
            if field.name in names:
                raise ValueError(f"register layout {layout.id!r} repeats field {field.name!r}")
            names.add(field.name)
            if field.width < 1 or field.lsb < 0 or field.lsb + field.width > 32:
                raise ValueError(
                    f"field {layout.id}.{field.name} [{field.lsb + field.width - 1}:{field.lsb}] "
                    "does not fit a 32-bit register"
                )
            mask = ((1 << field.width) - 1) << field.lsb
            if occupied & mask:
                raise ValueError(f"field {layout.id}.{field.name} overlaps another field")
            occupied |= mask


for _profile in FIELD_PROFILES.values():
    _validate_profile_spec(_profile)


def _legacy_profile(fields_per_register: int) -> ProfileSpec:
    if not 1 <= fields_per_register <= 8:
        raise ValueError("a 32-bit synthetic register supports one to eight 4-bit fields")
    if fields_per_register == 5:
        return FIELD_PROFILES[DEFAULT_FIELD_PROFILE]
    layout = LayoutSpec(
        f"nibbles{fields_per_register}",
        f"{fields_per_register} packed 4-bit fields",
        tuple(FieldSpec(f"f{index}", index * 4, 4) for index in range(fields_per_register)),
    )
    return ProfileSpec(layout.id, layout.label, (layout,))


def _resolve_profile(
    fields_per_register: int | None,
    field_profile: str | ProfileSpec | None,
) -> ProfileSpec:
    if field_profile is not None and fields_per_register is not None:
        raise ValueError("field_profile and fields_per_register are mutually exclusive")
    if isinstance(field_profile, ProfileSpec):
        _validate_profile_spec(field_profile)
        return field_profile
    if isinstance(field_profile, str):
        try:
            return FIELD_PROFILES[field_profile]
        except KeyError as error:
            raise ValueError(f"unknown field profile {field_profile!r}") from error
    return _legacy_profile(5 if fields_per_register is None else fields_per_register)


def _layout_for_register(profile: ProfileSpec, register_index: int) -> LayoutSpec:
    return profile.cycle[register_index % len(profile.cycle)]


def _layout_counts(profile: ProfileSpec, register_count: int) -> dict[str, int]:
    counts = dict.fromkeys((layout.id for layout in profile.cycle), 0)
    for register_index in range(register_count):
        layout = _layout_for_register(profile, register_index)
        counts[layout.id] += 1
    return counts


def _expected_field_count(profile: ProfileSpec, register_count: int) -> int:
    counts = _layout_counts(profile, register_count)
    layouts = {layout.id: layout for layout in profile.cycle}
    return sum(count * len(layouts[layout_id].fields) for layout_id, count in counts.items())


def _expected_field_bits(profile: ProfileSpec, register_count: int) -> int:
    counts = _layout_counts(profile, register_count)
    layouts = {layout.id: layout for layout in profile.cycle}
    return sum(
        count * sum(field.width for field in layouts[layout_id].fields) for layout_id, count in counts.items()
    )


def _profile_json(profile: ProfileSpec) -> dict:
    layouts = list(dict.fromkeys(profile.cycle))
    return {
        "id": profile.id,
        "label": profile.label,
        "cycle": [layout.id for layout in profile.cycle],
        "layouts": [
            {
                "id": layout.id,
                "label": layout.label,
                "fields": [
                    {"name": field.name, "lsb": field.lsb, "width": field.width} for field in layout.fields
                ],
            }
            for layout in layouts
        ],
    }


def _peak_rss_mib(who: int) -> float:
    maximum = resource.getrusage(who).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return maximum / divisor


def _write_rdl(
    stream: TextIO,
    register_count: int,
    fields_per_register: int | None,
    registers_per_block: int,
    max_address: int | None = None,
    *,
    field_profile: str | ProfileSpec | None = None,
) -> None:
    if register_count < 1:
        raise ValueError("register_count must be positive")
    if registers_per_block < 1:
        raise ValueError("registers_per_block must be positive")
    profile = _resolve_profile(fields_per_register, field_profile)

    layouts = list(dict.fromkeys(profile.cycle))
    type_names = {
        layout.id: ("scale_register_t" if len(layouts) == 1 else f"scale_register_{index}_t")
        for index, layout in enumerate(layouts)
    }
    for layout in layouts:
        stream.write(f"reg {type_names[layout.id]} {{\n    regwidth = 32;\n")
        for field in layout.fields:
            high = field.lsb + field.width - 1
            stream.write(f"    field {{ sw=rw; hw=r; }} {field.name}[{high}:{field.lsb}] = 0;\n")
        stream.write("};\n\n")
    stream.write("addrmap scale_envelope {\n")

    if max_address is not None:
        if max_address % 4:
            raise ValueError("max_address must be 32-bit aligned")
        if max_address < (register_count - 1) * 4:
            raise ValueError("max_address is too small for unique 32-bit register addresses")

    register_index = 0
    block_index = 0
    while register_index < register_count:
        stream.write("    regfile {\n")
        block_end = min(register_index + registers_per_block, register_count)
        block_start = register_index
        block_address = _register_address(block_start, register_count, max_address)
        while register_index < block_end:
            absolute_address = _register_address(register_index, register_count, max_address)
            relative_address = absolute_address - block_address
            layout = _layout_for_register(profile, register_index)
            stream.write(
                f"        {type_names[layout.id]} reg_{register_index:06d} @ 0x{relative_address:x};\n"
            )
            register_index += 1
        stream.write(f"    }} block_{block_index:06d} @ 0x{block_address:x};\n")
        block_index += 1
    stream.write("};\n")


def _register_address(index: int, register_count: int, max_address: int | None) -> int:
    if max_address is None:
        return index * 4
    if register_count == 1 or index == register_count - 1:
        return max_address
    return (index * max_address // (register_count - 1)) & ~0x3


def _validate_region(
    top: object,
    register_count: int,
    fields_per_register: int | None,
    max_address: int | None = None,
    *,
    field_profile: str | ProfileSpec | None = None,
) -> dict:
    profile = _resolve_profile(fields_per_register, field_profile)
    profile_mode = field_profile is not None
    observed_registers = 0
    observed_fields = 0
    observed_field_bits = 0
    first_address: int | None = None
    last_address: int | None = None

    for node in top.descendants():
        if isinstance(node, RegNode):
            if observed_registers >= register_count:
                raise RuntimeError(f"expected {register_count} registers, found additional registers")
            expected_address = _register_address(observed_registers, register_count, max_address)
            address = int(node.absolute_address)
            if address != expected_address:
                raise RuntimeError(
                    f"register {observed_registers} expected at 0x{expected_address:x}, found 0x{address:x}"
                )
            layout = _layout_for_register(profile, observed_registers)
            expected_layout = tuple((field.name, field.lsb, field.width) for field in layout.fields)
            actual_layout = tuple(
                (str(field.inst_name), int(field.low), int(field.width)) for field in node.fields()
            )
            if actual_layout != expected_layout:
                raise RuntimeError(
                    f"register {observed_registers} expected field layout {layout.id!r} "
                    f"{expected_layout!r}, found {actual_layout!r}"
                )
            observed_fields += len(actual_layout)
            observed_field_bits += sum(width for _, _, width in actual_layout)
            observed_registers += 1
            first_address = address if first_address is None else min(first_address, address)
            last_address = address if last_address is None else max(last_address, address)

    expected_fields = _expected_field_count(profile, register_count)
    expected_field_bits = _expected_field_bits(profile, register_count)
    expected_last_address = _register_address(register_count - 1, register_count, max_address)
    if observed_registers != register_count:
        raise RuntimeError(f"expected {register_count} registers, found {observed_registers}")
    if observed_fields != expected_fields:
        raise RuntimeError(f"expected {expected_fields} fields, found {observed_fields}")
    if observed_field_bits != expected_field_bits:
        raise RuntimeError(
            f"expected {expected_field_bits} populated field bits, found {observed_field_bits}"
        )
    if first_address != 0 or last_address != expected_last_address:
        raise RuntimeError(
            f"expected address region 0x0..0x{expected_last_address:x}, "
            f"found {first_address!r}..{last_address!r}"
        )

    result = {
        "registers": observed_registers,
        "fields": observed_fields,
        "first_address": first_address,
        "last_address": last_address,
        "region_bytes": expected_last_address + 4,
        "occupied_bytes": register_count * 4,
        "address_density": register_count * 4 / (expected_last_address + 4),
    }
    if profile_mode:
        result.update(
            {
                "field_profile": profile.id,
                "layout_counts": _layout_counts(profile, register_count),
                "field_bits": observed_field_bits,
            }
        )
    return result


def _output_sizes(output_dir: Path) -> dict:
    files = [path for path in output_dir.rglob("*") if path.is_file()]

    def total_for(suffixes: tuple[str, ...]) -> int:
        return sum(path.stat().st_size for path in files if path.suffix in suffixes)

    return {
        "source_bytes": sum(path.stat().st_size for path in files),
        "cpp_bytes": total_for((".cpp", ".hpp", ".h")),
        "python_bytes": total_for((".py", ".pyi")),
        "source_files": len(files),
        "binding_chunks": len(list(output_dir.glob("*_bindings_*.cpp"))),
    }


def _build_wheel(output_dir: Path, work_dir: Path) -> dict:
    import pybind11

    distribution_dir = work_dir / "dist"
    environment = os.environ.copy()
    prefixes = [pybind11.get_cmake_dir()]
    if environment.get("CMAKE_PREFIX_PATH"):
        prefixes.append(environment["CMAKE_PREFIX_PATH"])
    environment["CMAKE_PREFIX_PATH"] = os.pathsep.join(prefixes)

    started = time.perf_counter()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(distribution_dir),
        ],
        cwd=output_dir,
        env=environment,
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - started
    if result.returncode != 0:
        details = "\n".join((result.stdout, result.stderr)).strip()
        raise RuntimeError(f"wheel build failed:\n{details}")
    wheels = list(distribution_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one wheel, found {len(wheels)}")
    return {
        "build_s": elapsed,
        "build_peak_rss_mib": _peak_rss_mib(resource.RUSAGE_CHILDREN),
        "wheel_bytes": wheels[0].stat().st_size,
    }


def _worker(
    register_count: int,
    fields_per_register: int | None,
    registers_per_block: int,
    build: bool,
    gen_pyi: bool,
    max_address: int | None,
    field_profile: str | ProfileSpec | None = None,
) -> dict:
    profile = _resolve_profile(fields_per_register, field_profile)
    prefix = f"peakrdl-scale-{profile.id}-{register_count}-"
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
        work_dir = Path(temporary)
        rdl_file = work_dir / "scale.rdl"

        started = time.perf_counter()
        with rdl_file.open("w", encoding="utf-8") as stream:
            _write_rdl(
                stream,
                register_count,
                fields_per_register,
                registers_per_block,
                max_address,
                field_profile=profile if field_profile is not None else None,
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
            fields_per_register,
            max_address,
            field_profile=profile if field_profile is not None else None,
        )
        validate_s = time.perf_counter() - started

        output_dir = work_dir / "output"
        started = time.perf_counter()
        Pybind11Exporter().export(
            root.top,
            str(output_dir),
            soc_name=f"scale_{register_count}",
            gen_pyi=gen_pyi,
            split_by_hierarchy=True,
        )
        export_s = time.perf_counter() - started
        output = _output_sizes(output_dir)

        result = {
            **region,
            **output,
            "rdl_bytes": rdl_file.stat().st_size,
            "rdl_write_s": rdl_write_s,
            "compile_s": compile_s,
            "validate_s": validate_s,
            "export_s": export_s,
            "generation_peak_rss_mib": _peak_rss_mib(resource.RUSAGE_SELF),
            "build_s": None,
            "build_peak_rss_mib": None,
            "wheel_bytes": None,
        }
        if build:
            result.update(_build_wheel(output_dir, work_dir))
        return result


def _collect(
    sizes: list[int],
    fields_per_register: int,
    registers_per_block: int,
    build_max_registers: int,
    gen_pyi: bool,
    max_address: int | None,
    output: Path,
) -> None:
    points = []
    for register_count in sizes:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            str(register_count),
            "--fields-per-register",
            str(fields_per_register),
            "--registers-per-block",
            str(registers_per_block),
        ]
        if build_max_registers and register_count <= build_max_registers:
            command.append("--build")
        if not gen_pyi:
            command.append("--no-pyi")
        if max_address is not None:
            command.extend(("--max-address", hex(max_address)))
        worker = subprocess.run(command, capture_output=True, text=True)
        if worker.returncode != 0:
            details = "\n".join((worker.stdout, worker.stderr)).strip()
            raise RuntimeError(f"scale worker failed at {register_count} registers:\n{details}")
        point = json.loads(worker.stdout)
        points.append(point)
        print(
            f"collected {point['registers']:,} registers / {point['fields']:,} fields",
            file=sys.stderr,
        )

    payload = {
        "schema_version": 1,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": {
            "fields_per_register": fields_per_register,
            "registers_per_block": registers_per_block,
            "gen_pyi": gen_pyi,
            "build_max_registers": build_max_registers,
            "max_address": max_address,
            "layout": (
                "contiguous 32-bit registers from address zero"
                if max_address is None
                else f"sparse 32-bit registers spanning 0x0..0x{max_address:x}"
            ),
        },
        "environment": _environment(),
        "points": points,
    }
    payload["benchmark"]["shape_sha256"] = hashlib.sha256(
        json.dumps(payload["benchmark"], sort_keys=True).encode()
    ).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")


def _collect_profile_matrix(
    sizes: list[int],
    field_profiles: list[str],
    registers_per_block: int,
    build_max_registers: int,
    gen_pyi: bool,
    max_address: int | None,
    output: Path,
) -> None:
    profiles = [FIELD_PROFILES[profile_id] for profile_id in field_profiles]
    series = []
    for profile in profiles:
        points = []
        for register_count in sizes:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                str(register_count),
                "--field-profile",
                profile.id,
                "--registers-per-block",
                str(registers_per_block),
            ]
            if build_max_registers and register_count <= build_max_registers:
                command.append("--build")
            if not gen_pyi:
                command.append("--no-pyi")
            if max_address is not None:
                command.extend(("--max-address", hex(max_address)))
            worker = subprocess.run(command, capture_output=True, text=True)
            if worker.returncode != 0:
                details = "\n".join((worker.stdout, worker.stderr)).strip()
                raise RuntimeError(
                    f"scale worker failed for {profile.id} at {register_count} registers:\n{details}"
                )
            point = json.loads(worker.stdout)
            points.append(point)
            print(
                f"collected {profile.id}: {point['registers']:,} registers / {point['fields']:,} fields",
                file=sys.stderr,
            )
        series.append({"field_profile": profile.id, "points": points})

    benchmark = {
        "sizes": sizes,
        "registers_per_block": registers_per_block,
        "gen_pyi": gen_pyi,
        "build_max_registers": build_max_registers,
        "max_address": max_address,
        "layout": (
            "contiguous 32-bit registers from address zero"
            if max_address is None
            else f"sparse 32-bit registers spanning 0x0..0x{max_address:x}"
        ),
        "profiles": [_profile_json(profile) for profile in profiles],
    }
    benchmark["shape_sha256"] = hashlib.sha256(json.dumps(benchmark, sort_keys=True).encode()).hexdigest()
    payload = {
        "schema_version": 2,
        "kind": "field-profile-matrix",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": benchmark,
        "environment": _environment(),
        "series": series,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")


def _environment() -> dict:
    return {
        "system": platform.platform(),
        "machine": platform.machine(),
        "cpu": _cpu_model(),
        "python": platform.python_version(),
        "compiler": platform.python_compiler(),
        "cmake": _command_version(["cmake", "--version"]),
        "pybind11": _package_version("pybind11"),
        "scikit_build_core": _package_version("scikit-build-core"),
        "ninja": _command_version(["ninja", "--version"]),
        "commit": _git_commit(),
        "git_dirty": _git_dirty(),
    }


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_dirty() -> bool:
    """Record whether measurements include uncommitted source changes."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def _command_version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        return "unavailable"
    if result.returncode != 0:
        return "unavailable"
    return result.stdout.splitlines()[0].strip()


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unavailable"


def _cpu_model() -> str:
    if sys.platform == "darwin":
        result = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return platform.processor() or platform.machine()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=DEFAULT_SIZES)
    field_shape = parser.add_mutually_exclusive_group()
    field_shape.add_argument(
        "--fields-per-register",
        type=int,
        help="legacy uniform count of packed 4-bit fields (default: 5)",
    )
    field_shape.add_argument(
        "--field-profiles",
        nargs="+",
        choices=tuple(FIELD_PROFILES),
        help="collect a schema-v2 comparison matrix for one or more field profiles",
    )
    parser.add_argument("--registers-per-block", type=int, default=256)
    parser.add_argument(
        "--max-address",
        type=lambda value: int(value.replace("_", ""), 0),
        help="spread registers across 0..ADDRESS (decimal or 0x-prefixed)",
    )
    parser.add_argument(
        "--build-max-registers",
        type=int,
        default=0,
        help="build wheels only through this size (zero disables builds)",
    )
    parser.add_argument(
        "--output",
        type=Path,
    )
    parser.add_argument("--no-pyi", action="store_true", help="omit generated type stubs")
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--field-profile", choices=tuple(FIELD_PROFILES), help=argparse.SUPPRESS)
    parser.add_argument("--build", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.field_profile is not None and (
        args.field_profiles is not None or args.fields_per_register is not None
    ):
        parser.error("--field-profile is an internal worker option and cannot be combined")
    legacy_fields = 5 if args.fields_per_register is None else args.fields_per_register
    if args.worker is not None:
        result = _worker(
            args.worker,
            None if args.field_profile is not None else legacy_fields,
            args.registers_per_block,
            args.build,
            not args.no_pyi,
            args.max_address,
            args.field_profile,
        )
        print(json.dumps(result))
        return 0

    if args.field_profile is not None:
        parser.error("--field-profile is only valid for an internal worker")
    if args.field_profiles is not None:
        if len(set(args.field_profiles)) != len(args.field_profiles):
            parser.error("--field-profiles cannot contain duplicates")
        output = args.output or (REPOSITORY_ROOT / "benchmarks" / "results" / "field_profile_envelope.json")
        _collect_profile_matrix(
            args.sizes,
            args.field_profiles,
            args.registers_per_block,
            args.build_max_registers,
            not args.no_pyi,
            args.max_address,
            output,
        )
        return 0

    output = args.output or (REPOSITORY_ROOT / "benchmarks" / "results" / "scale_envelope.json")
    _collect(
        args.sizes,
        legacy_fields,
        args.registers_per_block,
        args.build_max_registers,
        not args.no_pyi,
        args.max_address,
        output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
