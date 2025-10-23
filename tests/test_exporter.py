"""
Basic tests for PeakRDL-pybind11 exporter
"""

import os
import tempfile
import shutil
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

# Try to import the exporter
try:
    from peakrdl_pybind11 import Pybind11Exporter
except ImportError:
    pytest.skip("peakrdl_pybind11 not installed", allow_module_level=True)


# Sample SystemRDL for testing
SIMPLE_RDL = """
addrmap simple_soc {
    name = "Simple SoC";
    desc = "A simple SoC for testing";
    
    reg {
        name = "Control Register";
        field {
            sw = rw;
            hw = r;
        } enable[0:0];
        
        field {
            sw = rw;
            hw = r;
        } mode[2:1];
    } control @ 0x0000;
    
    reg {
        name = "Status Register";
        field {
            sw = r;
            hw = w;
        } ready[0:0];
        
        field {
            sw = r;
            hw = w;
        } error[1:1];
    } status @ 0x0004;
};
"""


class TestExporter:
    """Test the Pybind11Exporter"""
    
    def test_basic_export(self):
        """Test basic export functionality"""
        # Compile SystemRDL
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(SIMPLE_RDL))
        root = rdl.elaborate()
        
        # Create temporary output directory
        with tempfile.TemporaryDirectory() as tmpdir:
            # Export
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="simple_soc")
            
            # Verify expected files were created
            expected_files = [
                'simple_soc_descriptors.hpp',
                'simple_soc_bindings.cpp',
                '__init__.py',
                'CMakeLists.txt',
                'pyproject.toml',
                '__init__.pyi',
            ]
            
            for filename in expected_files:
                filepath = os.path.join(tmpdir, filename)
                assert os.path.exists(filepath), f"Expected file not found: {filename}"
                
                # Verify files are not empty
                assert os.path.getsize(filepath) > 0, f"File is empty: {filename}"
    
    def test_custom_soc_name(self):
        """Test export with custom SoC name"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(SIMPLE_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="custom_name")
            
            # Verify files use custom name
            assert os.path.exists(os.path.join(tmpdir, 'custom_name_descriptors.hpp'))
            assert os.path.exists(os.path.join(tmpdir, 'custom_name_bindings.cpp'))
    
    def test_no_pyi_generation(self):
        """Test export without .pyi stub generation"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(SIMPLE_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="test_soc", gen_pyi=False)
            
            # Verify .pyi file was not created
            assert not os.path.exists(os.path.join(tmpdir, '__init__.pyi'))
    
    def test_generated_header_content(self):
        """Test that generated header contains expected content"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(SIMPLE_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="test_soc")
            
            # Read generated header
            with open(os.path.join(tmpdir, 'test_soc_descriptors.hpp'), 'r') as f:
                content = f.read()
            
            # Verify key elements are present
            assert 'namespace test_soc' in content
            assert 'class Master' in content
            assert 'class RegisterBase' in content
            assert 'class FieldBase' in content
    
    def test_generated_bindings_content(self):
        """Test that generated bindings contain expected content"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(SIMPLE_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="test_soc")
            
            # Read generated bindings
            with open(os.path.join(tmpdir, 'test_soc_bindings.cpp'), 'r') as f:
                content = f.read()
            
            # Verify key elements are present
            assert '#include <pybind11/pybind11.h>' in content
            assert 'PYBIND11_MODULE' in content
    
    def test_split_bindings(self):
        """Test that bindings are split when register count exceeds threshold"""
        # Create a large RDL with many registers
        rdl_content = "addrmap large_soc {\n"
        for i in range(10):
            rdl_content += f"""
    reg {{
        field {{
            sw = rw;
            hw = r;
        }} field{i}[7:0];
    }} reg{i} @ 0x{i*4:04x};
"""
        rdl_content += "};\n"
        
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(rdl_content))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            # Split when more than 5 registers (we have 10, so should split into 2 chunks)
            exporter.export(root.top, tmpdir, soc_name="large_soc", split_bindings=5)
            
            # Verify main bindings file exists
            assert os.path.exists(os.path.join(tmpdir, 'large_soc_bindings.cpp'))
            
            # Verify chunk files exist (should have 2 chunks: 0-4 and 5-9)
            assert os.path.exists(os.path.join(tmpdir, 'large_soc_bindings_0.cpp'))
            assert os.path.exists(os.path.join(tmpdir, 'large_soc_bindings_1.cpp'))
            
            # Read main bindings file
            with open(os.path.join(tmpdir, 'large_soc_bindings.cpp'), 'r') as f:
                main_content = f.read()
            
            # Verify it has forward declarations and calls to chunk functions
            assert 'bind_registers_chunk_0' in main_content
            assert 'bind_registers_chunk_1' in main_content
            
            # Read chunk file
            with open(os.path.join(tmpdir, 'large_soc_bindings_0.cpp'), 'r') as f:
                chunk_content = f.read()
            
            # Verify chunk has register bindings
            assert 'void bind_registers_chunk_0' in chunk_content
            assert '#include <pybind11/pybind11.h>' in chunk_content
            
            # Verify CMakeLists.txt includes all source files
            with open(os.path.join(tmpdir, 'CMakeLists.txt'), 'r') as f:
                cmake_content = f.read()
            
            assert 'large_soc_bindings.cpp' in cmake_content
            assert 'large_soc_bindings_0.cpp' in cmake_content
            assert 'large_soc_bindings_1.cpp' in cmake_content
    
    def test_hierarchical_split_bindings(self):
        """Test that bindings are split by hierarchy (addrmap/regfile)"""
        # Create RDL with hierarchical structure
        rdl_content = """
addrmap hierarchical_soc {
    regfile peripheral1 {
        reg {
            field { sw = rw; hw = r; } data[7:0];
        } reg0 @ 0x00;
        
        reg {
            field { sw = rw; hw = r; } data[7:0];
        } reg1 @ 0x04;
    } peripheral1 @ 0x0000;
    
    regfile peripheral2 {
        reg {
            field { sw = rw; hw = r; } data[7:0];
        } reg2 @ 0x00;
        
        reg {
            field { sw = rw; hw = r; } data[7:0];
        } reg3 @ 0x04;
    } peripheral2 @ 0x1000;
};
"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(rdl_content))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            # Enable hierarchical splitting
            exporter.export(root.top, tmpdir, soc_name="hierarchical_soc", split_by_hierarchy=True)
            
            # Verify main bindings file exists
            assert os.path.exists(os.path.join(tmpdir, 'hierarchical_soc_bindings.cpp'))
            
            # Should have chunk files (one per regfile: peripheral1 and peripheral2)
            assert os.path.exists(os.path.join(tmpdir, 'hierarchical_soc_bindings_0.cpp'))
            assert os.path.exists(os.path.join(tmpdir, 'hierarchical_soc_bindings_1.cpp'))
            
            # Read main bindings file
            with open(os.path.join(tmpdir, 'hierarchical_soc_bindings.cpp'), 'r') as f:
                main_content = f.read()
            
            # Verify it has forward declarations and calls to chunk functions
            assert 'bind_registers_chunk_0' in main_content
            assert 'bind_registers_chunk_1' in main_content
            
            # Read chunk file and verify it has register bindings
            with open(os.path.join(tmpdir, 'hierarchical_soc_bindings_0.cpp'), 'r') as f:
                chunk_content = f.read()
            
            assert 'void bind_registers_chunk_0' in chunk_content
            
            # Verify CMakeLists.txt includes all source files
            with open(os.path.join(tmpdir, 'CMakeLists.txt'), 'r') as f:
                cmake_content = f.read()
            
            assert 'hierarchical_soc_bindings.cpp' in cmake_content
            assert 'hierarchical_soc_bindings_0.cpp' in cmake_content
            assert 'hierarchical_soc_bindings_1.cpp' in cmake_content
    
    def test_no_split_bindings(self):
        """Test that bindings are not split when below threshold"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(SIMPLE_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            # With split_bindings=100, our 2 registers shouldn't trigger splitting
            exporter.export(root.top, tmpdir, soc_name="test_soc", split_bindings=100)
            
            # Verify only main bindings file exists
            assert os.path.exists(os.path.join(tmpdir, 'test_soc_bindings.cpp'))
            
            # Verify no chunk files exist
            assert not os.path.exists(os.path.join(tmpdir, 'test_soc_bindings_0.cpp'))
            
            # Read bindings file and verify it's not using split mode
            with open(os.path.join(tmpdir, 'test_soc_bindings.cpp'), 'r') as f:
                content = f.read()
            
            assert 'bind_registers_chunk' not in content
            assert 'PYBIND11_MODULE' in content
    
    def test_disable_split_bindings(self):
        """Test that split_bindings=0 disables splitting"""
        # Create RDL with many registers
        rdl_content = "addrmap large_soc {\n"
        for i in range(200):
            rdl_content += f"""
    reg {{
        field {{
            sw = rw;
        }} field{i}[7:0];
    }} reg{i} @ 0x{i*4:04x};
"""
        rdl_content += "};\n"
        
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(rdl_content))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            # Disable splitting with split_bindings=0
            exporter.export(root.top, tmpdir, soc_name="large_soc", split_bindings=0)
            
            # Verify only main bindings file exists
            assert os.path.exists(os.path.join(tmpdir, 'large_soc_bindings.cpp'))
            
            # Verify no chunk files exist
            assert not os.path.exists(os.path.join(tmpdir, 'large_soc_bindings_0.cpp'))
    
    @staticmethod
    def _write_rdl(content):
        """Write RDL content to a temporary file"""
        fd, path = tempfile.mkstemp(suffix='.rdl')
        os.write(fd, content.encode('utf-8'))
        os.close(fd)
        return path


class TestMasters:
    """Test Master implementations"""
    
    def test_mock_master(self):
        """Test MockMaster basic functionality"""
        from peakrdl_pybind11.masters import MockMaster
        
        master = MockMaster()
        
        # Test write and read
        master.write(0x1000, 0x12345678, 4)
        value = master.read(0x1000, 4)
        assert value == 0x12345678
        
        # Test reading unwritten address returns 0
        value = master.read(0x2000, 4)
        assert value == 0
        
        # Test reset
        master.reset()
        value = master.read(0x1000, 4)
        assert value == 0
    
    def test_callback_master(self):
        """Test CallbackMaster functionality"""
        from peakrdl_pybind11.masters import CallbackMaster
        
        read_calls = []
        write_calls = []
        
        def read_cb(addr, width):
            read_calls.append((addr, width))
            return 0x42
        
        def write_cb(addr, value, width):
            write_calls.append((addr, value, width))
        
        master = CallbackMaster(read_callback=read_cb, write_callback=write_cb)
        
        # Test read
        value = master.read(0x1000, 4)
        assert value == 0x42
        assert len(read_calls) == 1
        assert read_calls[0] == (0x1000, 4)
        
        # Test write
        master.write(0x2000, 0x99, 2)
        assert len(write_calls) == 1
        assert write_calls[0] == (0x2000, 0x99, 2)
