#!/usr/bin/env python3
"""
Benchmark runner script for PeakRDL-pybind11

This script provides convenient commands for running different benchmark scenarios.
"""

import subprocess
import sys
from pathlib import Path
from typing import TypedDict


class Command(TypedDict):
    cmd: list[str]
    description: str


def run_command(cmd: list[str], description: str) -> int:
    """Run a command and print description"""
    print(f"\n{'=' * 70}")
    print(f"{description}")
    print(f"{'=' * 70}")
    print(f"Command: {' '.join(cmd)}\n")

    result = subprocess.run(cmd)
    return result.returncode


def main() -> int:
    """Main entry point"""
    benchmarks_dir = Path(__file__).parent

    commands: dict[str, Command] = {
        "all": {
            "description": "Run all benchmarks (including slow build tests)",
            "cmd": ["pytest", str(benchmarks_dir), "--benchmark-only", "-v"],
        },
        "fast": {
            "description": "Run fast export benchmarks only (skip slow build tests)",
            "cmd": ["pytest", str(benchmarks_dir), "--benchmark-only", "-v", "-m", "not slow"],
        },
        "export": {
            "description": "Run export benchmarks",
            "cmd": [
                "pytest",
                f"{benchmarks_dir}/test_benchmarks.py::TestExportBenchmarks",
                "--benchmark-only",
                "-v",
            ],
        },
        "scaling": {
            "description": "Run scalability benchmarks",
            "cmd": [
                "pytest",
                f"{benchmarks_dir}/test_benchmarks.py::TestScalabilityBenchmarks",
                "--benchmark-only",
                "-v",
            ],
        },
        "memory": {
            "description": "Run memory benchmarks",
            "cmd": [
                "pytest",
                f"{benchmarks_dir}/test_benchmarks.py::TestMemoryBenchmarks",
                "--benchmark-only",
                "-v",
            ],
        },
        "build": {
            "description": "Run build benchmarks (requires python-build, cmake, compiler)",
            "cmd": [
                "pytest",
                f"{benchmarks_dir}/test_benchmarks.py::TestBuildBenchmarks",
                "--benchmark-only",
                "-v",
            ],
        },
        "compare": {
            "description": "Run benchmarks and save for comparison",
            "cmd": [
                "pytest",
                str(benchmarks_dir),
                "--benchmark-only",
                "-v",
                "--benchmark-autosave",
                "--benchmark-save=baseline",
            ],
        },
        "histogram": {
            "description": "Generate benchmark histogram",
            "cmd": [
                "pytest",
                str(benchmarks_dir),
                "--benchmark-only",
                "--benchmark-histogram=benchmark_histogram",
            ],
        },
    }

    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print("PeakRDL-pybind11 Benchmark Runner")
        print("=" * 70)
        print("\nUsage: python run_benchmarks.py <command>")
        print("\nAvailable commands:\n")

        for name, info in commands.items():
            print(f"  {name:12s} - {info['description']}")

        print("\nExamples:")
        print("  python run_benchmarks.py fast       # Quick export benchmarks")
        print("  python run_benchmarks.py export     # All export benchmarks")
        print("  python run_benchmarks.py all        # Everything")
        print("\nFor more options, run pytest directly:")
        print("  pytest benchmarks/ --benchmark-only --help")

        return 1

    command = sys.argv[1]
    cmd_info = commands[command]

    return run_command(cmd_info["cmd"], cmd_info["description"])


if __name__ == "__main__":
    sys.exit(main())
