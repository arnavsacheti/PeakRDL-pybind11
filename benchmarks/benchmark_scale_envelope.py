#!/usr/bin/env python3
"""Stress the current exporter across 1k to 100k+ unique registers.

Each register contains five fields by default, so the largest default point is
100,001 registers / 500,005 fields.  Registers are contiguous by default or
can be spread over a sparse address span with ``--max-address``.  Regfiles only
give hierarchical binding splitting realistic compilation units.  Every point
runs in a fresh worker process so peak RSS is comparable and temporary
generated sources are removed after measurement.
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
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TextIO

from systemrdl import RDLCompiler
from systemrdl.node import FieldNode, RegNode

from peakrdl_pybind11 import Pybind11Exporter

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIZES = (1_000, 5_000, 10_000, 25_000, 50_000, 75_000, 100_001)


def _peak_rss_mib(who: int) -> float:
    maximum = resource.getrusage(who).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return maximum / divisor


def _write_rdl(
    stream: TextIO,
    register_count: int,
    fields_per_register: int,
    registers_per_block: int,
    max_address: int | None = None,
) -> None:
    if fields_per_register > 8:
        raise ValueError("a 32-bit synthetic register supports at most eight 4-bit fields")

    stream.write("reg scale_register_t {\n    regwidth = 32;\n")
    for field_index in range(fields_per_register):
        low = field_index * 4
        high = low + 3
        stream.write(f"    field {{ sw=rw; hw=r; }} f{field_index}[{high}:{low}] = 0;\n")
    stream.write("};\n\naddrmap scale_envelope {\n")

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
            stream.write(f"        scale_register_t reg_{register_index:06d} @ 0x{relative_address:x};\n")
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
    fields_per_register: int,
    max_address: int | None = None,
) -> dict:
    observed_registers = 0
    observed_fields = 0
    first_address: int | None = None
    last_address: int | None = None

    for node in top.descendants():
        if isinstance(node, RegNode):
            expected_address = _register_address(observed_registers, register_count, max_address)
            observed_registers += 1
            address = int(node.absolute_address)
            if address != expected_address:
                raise RuntimeError(
                    f"register {observed_registers - 1} expected at 0x{expected_address:x}, "
                    f"found 0x{address:x}"
                )
            first_address = address if first_address is None else min(first_address, address)
            last_address = address if last_address is None else max(last_address, address)
        elif isinstance(node, FieldNode):
            observed_fields += 1

    expected_fields = register_count * fields_per_register
    expected_last_address = _register_address(register_count - 1, register_count, max_address)
    if observed_registers != register_count:
        raise RuntimeError(f"expected {register_count} registers, found {observed_registers}")
    if observed_fields != expected_fields:
        raise RuntimeError(f"expected {expected_fields} fields, found {observed_fields}")
    if first_address != 0 or last_address != expected_last_address:
        raise RuntimeError(
            f"expected address region 0x0..0x{expected_last_address:x}, "
            f"found {first_address!r}..{last_address!r}"
        )

    return {
        "registers": observed_registers,
        "fields": observed_fields,
        "first_address": first_address,
        "last_address": last_address,
        "region_bytes": expected_last_address + 4,
        "occupied_bytes": register_count * 4,
        "address_density": register_count * 4 / (expected_last_address + 4),
    }


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
    fields_per_register: int,
    registers_per_block: int,
    build: bool,
    gen_pyi: bool,
    max_address: int | None,
) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"peakrdl-scale-{register_count}-") as temporary:
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
            )
        rdl_write_s = time.perf_counter() - started

        started = time.perf_counter()
        compiler = RDLCompiler()
        compiler.compile_file(str(rdl_file))
        root = compiler.elaborate()
        compile_s = time.perf_counter() - started

        started = time.perf_counter()
        region = _validate_region(root.top, register_count, fields_per_register, max_address)
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
        "environment": {
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
        },
        "points": points,
    }
    payload["benchmark"]["shape_sha256"] = hashlib.sha256(
        json.dumps(payload["benchmark"], sort_keys=True).encode()
    ).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


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
    parser.add_argument("--fields-per-register", type=int, default=5)
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
        default=REPOSITORY_ROOT / "benchmarks" / "results" / "scale_envelope.json",
    )
    parser.add_argument("--no-pyi", action="store_true", help="omit generated type stubs")
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--build", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.worker is not None:
        result = _worker(
            args.worker,
            args.fields_per_register,
            args.registers_per_block,
            args.build,
            not args.no_pyi,
            args.max_address,
        )
        print(json.dumps(result))
        return 0

    _collect(
        args.sizes,
        args.fields_per_register,
        args.registers_per_block,
        args.build_max_registers,
        not args.no_pyi,
        args.max_address,
        args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
