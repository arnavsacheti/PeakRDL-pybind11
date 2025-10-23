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
import shutil
import tempfile
import subprocess
from pathlib import Path

import pytest
from systemrdl import RDLCompiler
from peakrdl_pybind11 import Pybind11Exporter


class TestExportBenchmarks:
    """Benchmark the RDL to pybind11 export process"""
    
    @pytest.fixture(scope="class")
    def benchmark_dir(self):
        """Get the benchmarks directory"""
        return Path(__file__).parent / "rdl_files"
    
    def test_export_simple_rdl(self, benchmark, benchmark_dir):
        """Benchmark export of simple RDL file (3 registers)"""
        rdl_file = benchmark_dir / "simple.rdl"
        
        def export_simple():
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
    
    def test_export_medium_rdl(self, benchmark, benchmark_dir):
        """Benchmark export of medium RDL file (~20 registers, 4 peripherals)"""
        rdl_file = benchmark_dir / "medium.rdl"
        
        def export_medium():
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
    
    def test_export_large_rdl(self, benchmark, benchmark_dir):
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
    
    def test_export_large_rdl_with_splitting(self, benchmark, benchmark_dir):
        """Benchmark export with binding splitting enabled (split every 10 registers)"""
        rdl_file = benchmark_dir / "large.rdl"
        
        def export_with_splitting():
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
    
    def test_export_large_rdl_hierarchical_split(self, benchmark, benchmark_dir):
        """Benchmark export with hierarchical splitting enabled"""
        rdl_file = benchmark_dir / "large.rdl"
        
        def export_hierarchical():
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


class TestBuildBenchmarks:
    """Benchmark the build process for distribution files"""
    
    @pytest.fixture(scope="class")
    def benchmark_dir(self):
        """Get the benchmarks directory"""
        return Path(__file__).parent / "rdl_files"
    
    @pytest.fixture(scope="class")
    def simple_export_dir(self, benchmark_dir, tmp_path_factory):
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
    def medium_export_dir(self, benchmark_dir, tmp_path_factory):
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
    def large_export_dir(self, benchmark_dir, tmp_path_factory):
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
    def test_build_sdist_simple(self, benchmark, simple_export_dir):
        """Benchmark building source distribution (tar.gz) for simple project"""
        
        def build_sdist():
            result = subprocess.run(
                ["python", "-m", "build", "--sdist", "--outdir", "/tmp/dist"],
                cwd=simple_export_dir,
                capture_output=True,
                text=True,
                timeout=120
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
    def test_build_wheel_simple(self, benchmark, simple_export_dir):
        """Benchmark building wheel distribution for simple project"""
        
        def build_wheel():
            result = subprocess.run(
                ["python", "-m", "build", "--wheel", "--outdir", "/tmp/dist"],
                cwd=simple_export_dir,
                capture_output=True,
                text=True,
                timeout=180
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
    def test_build_sdist_medium(self, benchmark, medium_export_dir):
        """Benchmark building source distribution for medium project"""
        
        def build_sdist():
            result = subprocess.run(
                ["python", "-m", "build", "--sdist", "--outdir", "/tmp/dist"],
                cwd=medium_export_dir,
                capture_output=True,
                text=True,
                timeout=120
            )
            return result.returncode == 0
        
        try:
            import build
            success = benchmark(build_sdist)
            assert success, "sdist build failed"
        except ImportError:
            pytest.skip("python-build not installed")
    
    @pytest.mark.slow
    def test_build_wheel_medium(self, benchmark, medium_export_dir):
        """Benchmark building wheel distribution for medium project"""
        
        def build_wheel():
            result = subprocess.run(
                ["python", "-m", "build", "--wheel", "--outdir", "/tmp/dist"],
                cwd=medium_export_dir,
                capture_output=True,
                text=True,
                timeout=300
            )
            return result.returncode == 0
        
        try:
            import build
            success = benchmark(build_wheel)
            assert success, "wheel build failed"
        except ImportError:
            pytest.skip("python-build not installed")
    
    @pytest.mark.slow
    def test_build_sdist_large(self, benchmark, large_export_dir):
        """Benchmark building source distribution for large project"""
        
        def build_sdist():
            result = subprocess.run(
                ["python", "-m", "build", "--sdist", "--outdir", "/tmp/dist"],
                cwd=large_export_dir,
                capture_output=True,
                text=True,
                timeout=120
            )
            return result.returncode == 0
        
        try:
            import build
            success = benchmark(build_sdist)
            assert success, "sdist build failed"
        except ImportError:
            pytest.skip("python-build not installed")
    
    @pytest.mark.slow
    def test_build_wheel_large(self, benchmark, large_export_dir):
        """Benchmark building wheel distribution for large project with split bindings"""
        
        def build_wheel():
            result = subprocess.run(
                ["python", "-m", "build", "--wheel", "--outdir", "/tmp/dist"],
                cwd=large_export_dir,
                capture_output=True,
                text=True,
                timeout=600
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
    def benchmark_dir(self):
        """Get the benchmarks directory"""
        return Path(__file__).parent / "rdl_files"
    
    def test_memory_export_large(self, benchmark, benchmark_dir):
        """Measure peak memory during large RDL export"""
        rdl_file = benchmark_dir / "large.rdl"
        
        def export_and_measure():
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
    
    def test_scaling_with_register_count(self, benchmark):
        """Benchmark how export time scales with number of registers"""
        
        def create_and_export_n_registers(n):
            """Create RDL with n registers and export it"""
            rdl_content = f"addrmap scaling_test_{n} {{\n"
            for i in range(n):
                rdl_content += f"""
    reg {{
        field {{
            sw = rw;
            hw = r;
        }} data{i}[7:0];
    }} reg{i} @ 0x{i*4:04x};
"""
            rdl_content += "};\n"
            
            with tempfile.TemporaryDirectory() as tmpdir:
                # Write RDL file
                rdl_file = os.path.join(tmpdir, "test.rdl")
                with open(rdl_file, 'w') as f:
                    f.write(rdl_content)
                
                # Compile and export
                rdl = RDLCompiler()
                rdl.compile_file(rdl_file)
                root = rdl.elaborate()
                
                output_dir = os.path.join(tmpdir, "output")
                exporter = Pybind11Exporter()
                exporter.export(root.top, output_dir, soc_name=f"scale_{n}")
        
        # Test with 10 registers (baseline)
        benchmark.pedantic(
            lambda: create_and_export_n_registers(10),
            iterations=5,
            rounds=3
        )
    
    def test_scaling_50_registers(self, benchmark):
        """Benchmark export with 50 registers"""
        
        def create_and_export():
            n = 50
            rdl_content = f"addrmap scaling_test_{n} {{\n"
            for i in range(n):
                rdl_content += f"""
    reg {{
        field {{ sw = rw; hw = r; }} data{i}[7:0];
    }} reg{i} @ 0x{i*4:04x};
"""
            rdl_content += "};\n"
            
            with tempfile.TemporaryDirectory() as tmpdir:
                rdl_file = os.path.join(tmpdir, "test.rdl")
                with open(rdl_file, 'w') as f:
                    f.write(rdl_content)
                
                rdl = RDLCompiler()
                rdl.compile_file(rdl_file)
                root = rdl.elaborate()
                
                output_dir = os.path.join(tmpdir, "output")
                exporter = Pybind11Exporter()
                exporter.export(root.top, output_dir, soc_name=f"scale_{n}")
        
        benchmark(create_and_export)
    
    def test_scaling_100_registers(self, benchmark):
        """Benchmark export with 100 registers"""
        
        def create_and_export():
            n = 100
            rdl_content = f"addrmap scaling_test_{n} {{\n"
            for i in range(n):
                rdl_content += f"""
    reg {{
        field {{ sw = rw; hw = r; }} data{i}[7:0];
    }} reg{i} @ 0x{i*4:04x};
"""
            rdl_content += "};\n"
            
            with tempfile.TemporaryDirectory() as tmpdir:
                rdl_file = os.path.join(tmpdir, "test.rdl")
                with open(rdl_file, 'w') as f:
                    f.write(rdl_content)
                
                rdl = RDLCompiler()
                rdl.compile_file(rdl_file)
                root = rdl.elaborate()
                
                output_dir = os.path.join(tmpdir, "output")
                exporter = Pybind11Exporter()
                exporter.export(root.top, output_dir, soc_name=f"scale_{n}")
        
        benchmark(create_and_export)
