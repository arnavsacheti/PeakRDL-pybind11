#!/usr/bin/env python3
"""Collect comparable generation, build, artifact, and runtime release metrics.

The public entry point archives each requested Git ref and launches an isolated
worker process against that source tree.  Keeping one process per release makes
the peak-RSS readings comparable and prevents modules from different releases
from contaminating one another.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import resource
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFS = ("v0.2.0", "v0.4.0", "v0.5.0", "v0.6.0", "v0.7.0", "v0.8.5")
SOC_NAME = "release_bench"


def _peak_rss_mib(who: int) -> float:
    """Return ru_maxrss in MiB on Linux and macOS."""
    maximum = resource.getrusage(who).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return maximum / divisor


def _median_seconds(operation: Callable[[], None], rounds: int) -> float:
    samples = []
    for _ in range(rounds):
        started = time.perf_counter()
        operation()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def _generated_size(output_dir: Path) -> int:
    return sum(path.stat().st_size for path in output_dir.rglob("*") if path.is_file())


def _prepare_import_tree(output_dir: Path, wheel: Path, destination: Path) -> None:
    """Unpack a wheel and normalize early releases into an importable package."""
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(destination)

    package_dir = destination / SOC_NAME
    package_dir.mkdir(exist_ok=True)
    wrapper = output_dir / SOC_NAME / "__init__.py"
    if not wrapper.exists():
        wrapper = output_dir / "__init__.py"
    shutil.copy2(wrapper, package_dir / "__init__.py")

    # Early generated wheels installed the extension at wheel root while later
    # releases install it inside the package.  Copying it makes both layouts
    # importable without changing what was timed or counted in the wheel.
    native_modules = list(destination.rglob(f"_{SOC_NAME}_native*.so"))
    native_modules += list(destination.rglob(f"_{SOC_NAME}_native*.pyd"))
    if not native_modules:
        raise RuntimeError("built wheel contains no native extension")
    if native_modules[0].parent != package_dir:
        shutil.copy2(native_modules[0], package_dir / native_modules[0].name)


def _runtime_metrics(output_dir: Path, wheel: Path, work_dir: Path, rounds: int, operations: int) -> dict:
    import_dir = work_dir / "wheel"
    _prepare_import_tree(output_dir, wheel, import_dir)
    sys.path.insert(0, str(import_dir))
    generated = importlib.import_module(SOC_NAME)

    class Store:
        def __init__(self) -> None:
            self.values: dict[int, int] = {}

        def read(self, address: int, width: int) -> int:
            return self.values.get(address, 0)

        def write(self, address: int, value: int, width: int) -> None:
            self.values[address] = value

    store = Store()
    if hasattr(generated, "wrap_master"):
        master = generated.wrap_master(store)
    else:

        class WrappedMaster(generated.Master):
            def __init__(self) -> None:
                generated.Master.__init__(self)

            def read(self, address: int, width: int) -> int:
                return store.read(address, width)

            def write(self, address: int, value: int, width: int) -> None:
                store.write(address, value, width)

        master = WrappedMaster()

    soc = generated.create()
    soc.attach_master(master)
    register = soc.control

    def read_batch() -> None:
        for _ in range(operations):
            register.read()

    def write_batch() -> None:
        for value in range(operations):
            register.write(value & 0x7)

    write_batch()
    read_batch()
    read_seconds = _median_seconds(read_batch, rounds)
    write_seconds = _median_seconds(write_batch, rounds)
    return {
        "read_us": read_seconds * 1_000_000 / operations,
        "write_us": write_seconds * 1_000_000 / operations,
    }


def _collect_worker(snapshot: Path, ref: str, generation_rounds: int, runtime_rounds: int) -> dict:
    sys.path.insert(0, str(snapshot / "src"))

    import pybind11
    from systemrdl import RDLCompiler

    from peakrdl_pybind11 import Pybind11Exporter

    rdl_file = snapshot / "benchmarks" / "rdl_files" / "simple.rdl"
    generation_samples = []

    with tempfile.TemporaryDirectory(prefix=f"peakrdl-{ref}-") as temporary:
        work_dir = Path(temporary)
        output_dir = work_dir / "output"

        for round_index in range(generation_rounds):
            round_output = (
                output_dir if round_index == generation_rounds - 1 else work_dir / f"gen-{round_index}"
            )
            started = time.perf_counter()
            compiler = RDLCompiler()
            compiler.compile_file(str(rdl_file))
            root = compiler.elaborate()
            Pybind11Exporter().export(root.top, str(round_output), soc_name=SOC_NAME)
            generation_samples.append(time.perf_counter() - started)

        source_bytes = _generated_size(output_dir)
        generation_peak_rss_mib = _peak_rss_mib(resource.RUSAGE_SELF)

        distribution_dir = work_dir / "dist"
        build_environment = os.environ.copy()
        prefixes = [pybind11.get_cmake_dir()]
        if build_environment.get("CMAKE_PREFIX_PATH"):
            prefixes.append(build_environment["CMAKE_PREFIX_PATH"])
        build_environment["CMAKE_PREFIX_PATH"] = os.pathsep.join(prefixes)

        started = time.perf_counter()
        build = subprocess.run(
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
            env=build_environment,
            capture_output=True,
            text=True,
        )
        build_seconds = time.perf_counter() - started
        if build.returncode != 0:
            details = "\n".join((build.stdout, build.stderr)).strip()
            raise RuntimeError(f"wheel build failed for {ref}:\n{details}")

        wheels = list(distribution_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one wheel for {ref}, found {len(wheels)}")
        wheel = wheels[0]
        runtime = _runtime_metrics(output_dir, wheel, work_dir, runtime_rounds, operations=20_000)

        return {
            "ref": ref,
            "generation_ms": statistics.median(generation_samples) * 1_000,
            "build_s": build_seconds,
            "read_us": runtime["read_us"],
            "write_us": runtime["write_us"],
            "source_kib": source_bytes / 1024,
            "wheel_kib": wheel.stat().st_size / 1024,
            "generation_peak_rss_mib": generation_peak_rss_mib,
            "build_peak_rss_mib": _peak_rss_mib(resource.RUSAGE_CHILDREN),
        }


def _git_value(arguments: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=REPOSITORY_ROOT, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _cpu_model() -> str:
    if sys.platform == "darwin":
        result = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return platform.processor() or platform.machine()


def _command_version(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        return "unavailable"
    if result.returncode != 0:
        return "unavailable"
    return result.stdout.splitlines()[0].strip()


def _collect_refs(refs: Sequence[str], output: Path, generation_rounds: int, runtime_rounds: int) -> None:
    releases = []
    design_path = Path("benchmarks") / "rdl_files" / "simple.rdl"
    design_sha256 = hashlib.sha256((REPOSITORY_ROOT / design_path).read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="peakrdl-release-history-") as temporary:
        temporary_dir = Path(temporary)
        for ref in refs:
            archive = temporary_dir / f"{ref}.tar"
            snapshot = temporary_dir / ref
            snapshot.mkdir()
            subprocess.run(
                ["git", "archive", "--format=tar", f"--output={archive}", ref],
                cwd=REPOSITORY_ROOT,
                check=True,
            )
            # The archive is produced locally by ``git archive`` from this
            # repository.  Python 3.10 does not yet accept ``filter=``.
            with tarfile.open(archive) as tar:
                try:
                    tar.extractall(snapshot, filter="data")
                except TypeError:
                    tar.extractall(snapshot)
            snapshot_sha256 = hashlib.sha256((snapshot / design_path).read_bytes()).hexdigest()
            if snapshot_sha256 != design_sha256:
                raise RuntimeError(f"benchmark input changed at {ref}")

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(snapshot),
                    ref,
                    "--generation-rounds",
                    str(generation_rounds),
                    "--runtime-rounds",
                    str(runtime_rounds),
                ],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                details = "\n".join((result.stdout, result.stderr)).strip()
                raise RuntimeError(f"benchmark worker failed for {ref}:\n{details}")
            release = json.loads(result.stdout)
            commit_ref = f"{ref}^{{commit}}"
            release["commit"] = _git_value(["rev-parse", commit_ref])
            release["commit_date"] = _git_value(["show", "-s", "--format=%cs", commit_ref])
            releases.append(release)
            print(f"collected {ref}", file=sys.stderr)

    payload = {
        "schema_version": 1,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": {
            "design": design_path.as_posix(),
            "design_sha256": design_sha256,
            "registers": 3,
            "generation_rounds": generation_rounds,
            "runtime_rounds": runtime_rounds,
            "runtime_operations_per_round": 20_000,
        },
        "environment": {
            "system": platform.platform(),
            "cpu": _cpu_model(),
            "python": platform.python_version(),
            "compiler": platform.python_compiler(),
            "cmake": _command_version(["cmake", "--version"]),
            "pybind11": version("pybind11"),
            "scikit_build_core": version("scikit-build-core"),
            "ninja": _command_version(["ninja", "--version"]),
        },
        "releases": releases,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("refs", nargs="*", default=DEFAULT_REFS, help="Git refs to benchmark")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "benchmarks" / "results" / "release_history.json",
    )
    parser.add_argument("--generation-rounds", type=int, default=7)
    parser.add_argument("--runtime-rounds", type=int, default=7)
    parser.add_argument("--worker", nargs=2, metavar=("SNAPSHOT", "REF"), help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.worker:
        snapshot, ref = args.worker
        result = _collect_worker(Path(snapshot), ref, args.generation_rounds, args.runtime_rounds)
        print(json.dumps(result))
        return 0

    _collect_refs(args.refs, args.output, args.generation_rounds, args.runtime_rounds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
