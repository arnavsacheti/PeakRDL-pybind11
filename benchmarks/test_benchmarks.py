"""
Benchmark tests for PeakRDL-pybind11

This module contains performance benchmarks to measure:
1. RDL compilation and export to pybind11 modules
2. Building distribution files (tar and wheel)

The benchmarks use different complexity levels:
- Simple: 3 registers (basic performance baseline)
- Medium: ~20 registers across 4 peripherals
- Large: ~70 registers across 15+ peripherals
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from pytest_benchmark.fixture import BenchmarkFixture
from systemrdl.compiler import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter


class TestExportBenchmarks:
    """Benchmark the RDL to pybind11 export process"""

    @pytest.fixture(scope="class")
    def benchmark_dir(self) -> Path:
        """Get the benchmarks directory"""
        return Path(__file__).parent / "rdl_files"

    def test_export_simple_rdl(self, benchmark: BenchmarkFixture, benchmark_dir: Path) -> None:
        """Benchmark export of simple RDL file (3 registers)"""
        rdl_file = benchmark_dir / "simple.rdl"

        def export_simple() -> str:
            with tempfile.TemporaryDirectory() as tmpdir:
                rdl = RDLCompiler()
                rdl.compile_file(str(rdl_file))
                root = rdl.elaborate()

                exporter = Pybind11Exporter()
                exporter.export(root.top, tmpdir, soc_name="simple_bench")

                # Verify output was created
                assert os.path.exists(os.path.join(tmpdir, "simple_bench_descriptors.hpp"))
                return tmpdir

        result = benchmark(export_simple)

    def test_export_medium_rdl(self, benchmark: BenchmarkFixture, benchmark_dir: Path) -> None:
        """Benchmark export of medium RDL file (~20 registers, 4 peripherals)"""
        rdl_file = benchmark_dir / "medium.rdl"

        def export_medium() -> str:
            with tempfile.TemporaryDirectory() as tmpdir:
                rdl = RDLCompiler()
                rdl.compile_file(str(rdl_file))
                root = rdl.elaborate()

                exporter = Pybind11Exporter()
                exporter.export(root.top, tmpdir, soc_name="medium_bench")

                # Verify output was created
                assert os.path.exists(os.path.join(tmpdir, "medium_bench_descriptors.hpp"))
                return tmpdir

        result = benchmark(export_medium)

    def test_export_large_rdl(self, benchmark: BenchmarkFixture, benchmark_dir: Path) -> None:
        """Benchmark export of large RDL file (~70 registers, 15+ peripherals)"""
        rdl_file = benchmark_dir / "large.rdl"

        def export_large():
            with tempfile.TemporaryDirectory() as tmpdir:
                rdl = RDLCompiler()
                rdl.compile_file(str(rdl_file))
                root = rdl.elaborate()

                exporter = Pybind11Exporter()
                exporter.export(root.top, tmpdir, soc_name="large_bench")

                # Verify output was created
                assert os.path.exists(os.path.join(tmpdir, "large_bench_descriptors.hpp"))
                return tmpdir

        result = benchmark(export_large)

    def test_export_large_rdl_with_splitting(self, benchmark: BenchmarkFixture, benchmark_dir: Path) -> None:
        """Benchmark export with binding splitting enabled (split every 10 registers)"""
        rdl_file = benchmark_dir / "large.rdl"

        def export_with_splitting() -> str:
            with tempfile.TemporaryDirectory() as tmpdir:
                rdl = RDLCompiler()
                rdl.compile_file(str(rdl_file))
                root = rdl.elaborate()

                exporter = Pybind11Exporter()
                exporter.export(root.top, tmpdir, soc_name="large_bench", split_bindings=10)

                # Verify split files were created
                assert os.path.exists(os.path.join(tmpdir, "large_bench_bindings_0.cpp"))
                return tmpdir

        result = benchmark(export_with_splitting)

    def test_export_large_rdl_hierarchical_split(self, benchmark: BenchmarkFixture, benchmark_dir: Path) -> None:
        """Benchmark export with hierarchical splitting enabled"""
        rdl_file = benchmark_dir / "large.rdl"

        def export_hierarchical() -> str:
            with tempfile.TemporaryDirectory() as tmpdir:
                rdl = RDLCompiler()
                rdl.compile_file(str(rdl_file))
                root = rdl.elaborate()

                exporter = Pybind11Exporter()
                exporter.export(root.top, tmpdir, soc_name="large_bench", split_by_hierarchy=True)

                # Verify output was created
                assert os.path.exists(os.path.join(tmpdir, "large_bench_descriptors.hpp"))
                return tmpdir

        result = benchmark(export_hierarchical)

    def test_export_realistic_mcu(self, benchmark: BenchmarkFixture, benchmark_dir: Path) -> None:
        """Benchmark export of realistic MCU RDL file (~288 registers, real-world complexity)

        This test uses a realistic microcontroller register map inspired by ARM Cortex-M
        based MCUs with multiple UARTs, SPIs, I2C, Timers, ADC, DMA, GPIO banks, etc.
        Represents real-world embedded systems design with 300+ registers.
        """
        rdl_file = benchmark_dir / "realistic_mcu.rdl"

        def export_realistic() -> str:
            with tempfile.TemporaryDirectory() as tmpdir:
                rdl = RDLCompiler()
                rdl.compile_file(str(rdl_file))
                root = rdl.elaborate()

                exporter = Pybind11Exporter()
                exporter.export(root.top, tmpdir, soc_name="realistic_mcu")

                # Verify output was created
                assert os.path.exists(os.path.join(tmpdir, "realistic_mcu_descriptors.hpp"))
                return tmpdir

        result = benchmark(export_realistic)

    def test_export_realistic_mcu_with_splitting(self, benchmark: BenchmarkFixture, benchmark_dir: Path) -> None:
        """Benchmark export of realistic MCU with binding splitting (split every 50 registers)"""
        rdl_file = benchmark_dir / "realistic_mcu.rdl"

        def export_with_splitting():
            with tempfile.TemporaryDirectory() as tmpdir:
                rdl = RDLCompiler()
                rdl.compile_file(str(rdl_file))
                root = rdl.elaborate()

                exporter = Pybind11Exporter()
                exporter.export(root.top, tmpdir, soc_name="realistic_mcu", split_bindings=50)

                # Verify split files were created
                assert os.path.exists(os.path.join(tmpdir, "realistic_mcu_bindings_0.cpp"))
                return tmpdir

        result = benchmark(export_with_splitting)


class TestBuildBenchmarks:
    """Benchmark the build process for distribution files"""

    @pytest.fixture(scope="class")
    def benchmark_dir(self) -> Path:
        """Get the benchmarks directory"""
        return Path(__file__).parent / "rdl_files"

    @pytest.fixture(scope="class")
    def simple_export_dir(self, benchmark_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
        """Create a simple export for build benchmarking"""
        tmpdir = tmp_path_factory.mktemp("simple_export")
        rdl_file = benchmark_dir / "simple.rdl"

        rdl = RDLCompiler()
        rdl.compile_file(str(rdl_file))
        root = rdl.elaborate()

        exporter = Pybind11Exporter()
        exporter.export(root.top, str(tmpdir), soc_name="simple_bench")

        return tmpdir

    @pytest.fixture(scope="class")
    def medium_export_dir(self, benchmark_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
        """Create a medium export for build benchmarking"""
        tmpdir = tmp_path_factory.mktemp("medium_export")
        rdl_file = benchmark_dir / "medium.rdl"

        rdl = RDLCompiler()
        rdl.compile_file(str(rdl_file))
        root = rdl.elaborate()

        exporter = Pybind11Exporter()
        exporter.export(root.top, str(tmpdir), soc_name="medium_bench")

        return tmpdir

    @pytest.fixture(scope="class")
    def large_export_dir(self, benchmark_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
        """Create a large export for build benchmarking"""
        tmpdir = tmp_path_factory.mktemp("large_export")
        rdl_file = benchmark_dir / "large.rdl"

        rdl = RDLCompiler()
        rdl.compile_file(str(rdl_file))
        root = rdl.elaborate()

        exporter = Pybind11Exporter()
        exporter.export(root.top, str(tmpdir), soc_name="large_bench", split_bindings=10)

        return tmpdir

    @pytest.mark.slow
    def test_build_sdist_simple(self, benchmark: BenchmarkFixture, simple_export_dir: Path) -> None:
        """Benchmark building source distribution (tar.gz) for simple project"""

        def build_sdist() -> bool:
            result = subprocess.run(
                ["python", "-m", "build", "--sdist", "--outdir", "/tmp/dist"],
                cwd=simple_export_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return result.returncode == 0

        # Only run if build tools are available
        try:
            import build

            success = benchmark(build_sdist)
            assert success, "sdist build failed"
        except ImportError:
            pytest.skip("python-build not installed")

    @pytest.mark.slow
    def test_build_wheel_simple(self, benchmark: BenchmarkFixture, simple_export_dir: Path) -> None:
        """Benchmark building wheel distribution for simple project"""

        def build_wheel() -> bool:
            result = subprocess.run(
                ["python", "-m", "build", "--wheel", "--outdir", "/tmp/dist"],
                cwd=simple_export_dir,
                capture_output=True,
                text=True,
                timeout=180,
            )
            return result.returncode == 0

        # Only run if build tools are available
        try:
            import build

            success = benchmark(build_wheel)
            assert success, "wheel build failed"
        except ImportError:
            pytest.skip("python-build not installed")

    @pytest.mark.slow
    def test_build_sdist_medium(self, benchmark: BenchmarkFixture, medium_export_dir: Path) -> None:
        """Benchmark building source distribution for medium project"""

        def build_sdist():
            result = subprocess.run(
                ["python", "-m", "build", "--sdist", "--outdir", "/tmp/dist"],
                cwd=medium_export_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return result.returncode == 0

        try:
            import build

            success = benchmark(build_sdist)
            assert success, "sdist build failed"
        except ImportError:
            pytest.skip("python-build not installed")

    @pytest.mark.slow
    def test_build_wheel_medium(self, benchmark: BenchmarkFixture, medium_export_dir: Path) -> None:
        """Benchmark building wheel distribution for medium project"""

        def build_wheel():
            result = subprocess.run(
                ["python", "-m", "build", "--wheel", "--outdir", "/tmp/dist"],
                cwd=medium_export_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return result.returncode == 0

        try:
            import build

            success = benchmark(build_wheel)
            assert success, "wheel build failed"
        except ImportError:
            pytest.skip("python-build not installed")

    @pytest.mark.slow
    def test_build_sdist_large(self, benchmark: BenchmarkFixture, large_export_dir: Path) -> None:
        """Benchmark building source distribution for large project"""

        def build_sdist():
            result = subprocess.run(
                ["python", "-m", "build", "--sdist", "--outdir", "/tmp/dist"],
                cwd=large_export_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return result.returncode == 0

        try:
            import build

            success = benchmark(build_sdist)
            assert success, "sdist build failed"
        except ImportError:
            pytest.skip("python-build not installed")

    @pytest.mark.slow
    def test_build_wheel_large(self, benchmark: BenchmarkFixture, large_export_dir: Path) -> None:
        """Benchmark building wheel distribution for large project with split bindings"""

        def build_wheel() -> bool:
            result = subprocess.run(
                ["python", "-m", "build", "--wheel", "--outdir", "/tmp/dist"],
                cwd=large_export_dir,
                capture_output=True,
                text=True,
                timeout=600,
            )
            return result.returncode == 0

        try:
            import build

            success = benchmark(build_wheel)
            assert success, "wheel build failed"
        except ImportError:
            pytest.skip("python-build not installed")


class TestMemoryBenchmarks:
    """Benchmark memory usage during export and build"""

    @pytest.fixture(scope="class")
    def benchmark_dir(self) -> Path:
        """Get the benchmarks directory"""
        return Path(__file__).parent / "rdl_files"

    def test_memory_export_large(self, benchmark: BenchmarkFixture, benchmark_dir: Path) -> None:
        """Measure peak memory during large RDL export"""
        rdl_file = benchmark_dir / "large.rdl"

        def export_and_measure() -> dict[str, float]:
            import tracemalloc

            tracemalloc.start()

            with tempfile.TemporaryDirectory() as tmpdir:
                rdl = RDLCompiler()
                rdl.compile_file(str(rdl_file))
                root = rdl.elaborate()

                exporter = Pybind11Exporter()
                exporter.export(root.top, tmpdir, soc_name="large_bench")

                current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()

                return {"current_mb": current / 1024 / 1024, "peak_mb": peak / 1024 / 1024}

        result = benchmark(export_and_measure)


class TestScalabilityBenchmarks:
    """Test how performance scales with register count"""

    def test_scaling_with_register_count(self, benchmark: BenchmarkFixture) -> None:
        """Benchmark how export time scales with number of registers"""

        def create_and_export_n_registers(n: int) -> None:
            """Create RDL with n registers and export it"""
            rdl_content = f"addrmap scaling_test_{n} {{\n"
            for i in range(n):
                rdl_content += f"""
    reg {{
        field {{
            sw = rw;
            hw = r;
        }} data{i}[7:0];
    }} reg{i} @ 0x{i * 4:04x};
"""
            rdl_content += "};\n"

            with tempfile.TemporaryDirectory() as tmpdir:
                # Write RDL file
                rdl_file = os.path.join(tmpdir, "test.rdl")
                with open(rdl_file, "w") as f:
                    f.write(rdl_content)

                # Compile and export
                rdl = RDLCompiler()
                rdl.compile_file(rdl_file)
                root = rdl.elaborate()

                output_dir = os.path.join(tmpdir, "output")
                exporter = Pybind11Exporter()
                exporter.export(root.top, output_dir, soc_name=f"scale_{n}")

        # Test with 10 registers (baseline)
        benchmark.pedantic(lambda: create_and_export_n_registers(10), iterations=5, rounds=3)

    def test_scaling_50_registers(self, benchmark: BenchmarkFixture) -> None:
        """Benchmark export with 50 registers"""

        def create_and_export() -> None:
            n = 50
            rdl_content = f"addrmap scaling_test_{n} {{\n"
            for i in range(n):
                rdl_content += f"""
    reg {{
        field {{ sw = rw; hw = r; }} data{i}[7:0];
    }} reg{i} @ 0x{i * 4:04x};
"""
            rdl_content += "};\n"

            with tempfile.TemporaryDirectory() as tmpdir:
                rdl_file = os.path.join(tmpdir, "test.rdl")
                with open(rdl_file, "w") as f:
                    f.write(rdl_content)

                rdl = RDLCompiler()
                rdl.compile_file(rdl_file)
                root = rdl.elaborate()

                output_dir = os.path.join(tmpdir, "output")
                exporter = Pybind11Exporter()
                exporter.export(root.top, output_dir, soc_name=f"scale_{n}")

        benchmark(create_and_export)

    def test_scaling_100_registers(self, benchmark: BenchmarkFixture) -> None:
        """Benchmark export with 100 registers"""

        def create_and_export() -> None:
            n = 100
            rdl_content = f"addrmap scaling_test_{n} {{\n"
            for i in range(n):
                rdl_content += f"""
    reg {{
        field {{ sw = rw; hw = r; }} data{i}[7:0];
    }} reg{i} @ 0x{i * 4:04x};
"""
            rdl_content += "};\n"

            with tempfile.TemporaryDirectory() as tmpdir:
                rdl_file = os.path.join(tmpdir, "test.rdl")
                with open(rdl_file, "w") as f:
                    f.write(rdl_content)

                rdl = RDLCompiler()
                rdl.compile_file(rdl_file)
                root = rdl.elaborate()

                output_dir = os.path.join(tmpdir, "output")
                exporter = Pybind11Exporter()
                exporter.export(root.top, output_dir, soc_name=f"scale_{n}")

        benchmark(create_and_export)

    def test_scaling_large_hierarchical(self, benchmark: BenchmarkFixture) -> None:
        """Benchmark hierarchical export with 10k registers (100 regfiles x 100 regs each)
        
        This test validates the O(n) performance optimization for hierarchical splitting.
        Previously this would take 5+ minutes, now should complete in <1 second.
        """

        def create_and_export() -> None:
            # Create 10k registers in hierarchical structure
            rdl_content = "addrmap large_hierarchical {\n"
            for i in range(100):  # 100 regfiles
                rdl_content += f"  regfile rf{i} {{\n"
                for j in range(100):  # 100 registers per regfile
                    rdl_content += f"    reg {{ field {{ sw = rw; }} f[7:0]; }} r{j} @ 0x{j*4:x};\n"
                rdl_content += f"  }} rf{i} @ 0x{i*0x1000:x};\n"
            rdl_content += "};\n"

            with tempfile.TemporaryDirectory() as tmpdir:
                rdl_file = os.path.join(tmpdir, "test.rdl")
                with open(rdl_file, "w") as f:
                    f.write(rdl_content)

                rdl = RDLCompiler()
                rdl.compile_file(rdl_file)
                root = rdl.elaborate()

                output_dir = os.path.join(tmpdir, "output")
                exporter = Pybind11Exporter()
                # Use hierarchical splitting - this is what we optimized
                exporter.export(root.top, output_dir, soc_name="large_hier", split_by_hierarchy=True)

        benchmark(create_and_export)
