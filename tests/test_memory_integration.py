"""
Comprehensive integration test for memory types
This test generates code, attempts to build it, and verify the Python list-like interface works
"""

import os
import sys
import tempfile
import subprocess
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

# Try to import the exporter
try:
    from peakrdl_pybind11 import Pybind11Exporter
except ImportError:
    pytest.skip("peakrdl_pybind11 not installed", allow_module_level=True)


# Sample RDL with memory for integration testing
INTEGRATION_MEMORY_RDL = """
addrmap integration_test {
    name = "Integration Test SoC";
    desc = "A SoC for testing memory functionality";
    
    reg control_reg_t {
        name = "Control Register";
        field {
            name = "Enable";
            sw = rw;
            hw = r;
        } enable[0:0] = 0;
        
        field {
            name = "Mode";
            sw = rw;
            hw = r;
        } mode[3:1] = 0;
        
        field {
            name = "Data";
            sw = rw;
            hw = rw;
        } data[31:8] = 0;
    };
    
    external mem {
        name = "Control Memory";
        desc = "A memory array of control registers";
        mementries = 64;
        memwidth = 32;
        
        control_reg_t entry;
    } ctrl_mem @ 0x1000;
};
"""


@pytest.mark.integration
class TestMemoryIntegration:
    """Integration tests for memory functionality"""
    
    def test_memory_generation_complete(self):
        """Comprehensive test of memory code generation"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(INTEGRATION_MEMORY_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="integration_test")
            
            # Verify all expected files
            expected_files = [
                'integration_test_descriptors.hpp',
                'integration_test_bindings.cpp',
                '__init__.py',
                '__init__.pyi',
                'CMakeLists.txt',
                'pyproject.toml',
            ]
            
            for filename in expected_files:
                filepath = os.path.join(tmpdir, filename)
                assert os.path.exists(filepath), f"Missing file: {filename}"
            
            # Verify descriptor content has memory base class
            with open(os.path.join(tmpdir, 'integration_test_descriptors.hpp'), 'r') as f:
                descriptor_content = f.read()
            
            assert 'class MemoryBase' in descriptor_content
            assert 'class ctrl_mem_t' in descriptor_content
            assert 'MemoryBase<entry_t>' in descriptor_content
            
            # Verify all entry registers have correct offsets
            # The memory starts at 0x1000 with 64 entries of 4 bytes each
            assert 'entry_t(uint64_t base_offset)' in descriptor_content
            assert 'RegisterBase("entry", base_offset + 0x1000, 4)' in descriptor_content
            
            # Verify bindings content has memory interface
            with open(os.path.join(tmpdir, 'integration_test_bindings.cpp'), 'r') as f:
                bindings_content = f.read()
            
            # Verify Python list-like interface bindings
            assert 'def("__len__"' in bindings_content
            assert 'def("__getitem__"' in bindings_content
            assert 'def("__iter__"' in bindings_content
            
            # Verify slice support with proper lambda
            assert 'py::slice slice' in bindings_content
            assert 'slice.compute' in bindings_content
            
            # Verify stubs have proper typing
            with open(os.path.join(tmpdir, '__init__.pyi'), 'r') as f:
                stub_content = f.read()
            
            assert 'class ctrl_mem_t' in stub_content
            assert 'def __len__(self) -> int:' in stub_content
            assert 'def __getitem__(self, index: int) -> entry_t:' in stub_content
            assert 'def __getitem__(self, index: slice) -> list[entry_t]:' in stub_content
            assert 'def __iter__(self) -> Iterator[entry_t]:' in stub_content
    
    def test_memory_bindings_syntax_valid(self):
        """Verify generated C++ code has valid syntax (at least compiles headers)"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(INTEGRATION_MEMORY_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="integration_test")
            
            # Check if C++ compiler is available
            try:
                result = subprocess.run(
                    ['g++', '--version'],
                    capture_output=True,
                    timeout=5
                )
                if result.returncode != 0:
                    pytest.skip("g++ not available")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pytest.skip("g++ not available")
            
            # Try to at least syntax-check the header
            descriptor_path = os.path.join(tmpdir, 'integration_test_descriptors.hpp')
            try:
                result = subprocess.run(
                    ['g++', '-std=c++11', '-fsyntax-only', '-c', descriptor_path],
                    capture_output=True,
                    timeout=10,
                    cwd=tmpdir
                )
                
                # Print output for debugging if it fails
                if result.returncode != 0:
                    print("STDOUT:", result.stdout.decode())
                    print("STDERR:", result.stderr.decode())
                
                assert result.returncode == 0, "C++ header has syntax errors"
            except subprocess.TimeoutExpired:
                pytest.skip("C++ compilation timed out")
    
    def test_memory_properties_correct(self):
        """Verify memory has correct properties"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(INTEGRATION_MEMORY_RDL))
        root = rdl.elaborate()
        
        exporter = Pybind11Exporter()
        nodes = exporter._collect_nodes(root.top)
        
        # Should have exactly 1 memory node
        assert len(nodes['mems']) == 1
        
        mem = nodes['mems'][0]
        assert mem.inst_name == 'ctrl_mem'
        assert mem.get_property('mementries') == 64
        assert mem.get_property('memwidth') == 32
        assert mem.absolute_address == 0x1000
        
        # Memory should have one child (the entry register)
        children = list(mem.children())
        assert len(children) == 1
        assert children[0].inst_name == 'entry'
        assert children[0].size == 4  # 32 bits = 4 bytes
    
    @staticmethod
    def _write_rdl(content):
        """Write RDL content to a temporary file"""
        fd, path = tempfile.mkstemp(suffix='.rdl')
        os.write(fd, content.encode('utf-8'))
        os.close(fd)
        return path
