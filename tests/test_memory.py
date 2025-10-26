"""
Tests for memory type support in PeakRDL-pybind11
"""

import os
import tempfile
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

# Try to import the exporter
try:
    from peakrdl_pybind11 import Pybind11Exporter
except ImportError:
    pytest.skip("peakrdl_pybind11 not installed", allow_module_level=True)


# Sample RDL with memory for testing
MEMORY_RDL = """
addrmap mem_test {
    name = "Memory Test SoC";
    desc = "A SoC with memory arrays for testing";
    
    reg data_reg_t {
        name = "Data Register";
        field {
            name = "Data";
            sw = rw;
            hw = rw;
        } data[31:0];
    };
    
    external mem {
        name = "Data Memory";
        desc = "A memory array for data storage";
        mementries = 256;
        memwidth = 32;
        
        data_reg_t entry;
    } data_mem @ 0x1000;
    
    external mem {
        name = "Small Memory";
        desc = "A smaller memory array";
        mementries = 16;
        memwidth = 32;
        
        data_reg_t entry;
    } small_mem @ 0x2000;
};
"""


class TestMemoryExport:
    """Test memory export functionality"""
    
    def test_memory_node_collection(self):
        """Test that memory nodes are collected during export"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(MEMORY_RDL))
        root = rdl.elaborate()
        
        exporter = Pybind11Exporter()
        nodes = exporter._collect_nodes(root.top)
        
        # Should have 2 memory nodes
        assert len(nodes['mems']) == 2
        
        # Check memory properties
        mem_names = {mem.inst_name for mem in nodes['mems']}
        assert 'data_mem' in mem_names
        assert 'small_mem' in mem_names
        
        # Check memory entries
        data_mem = next(m for m in nodes['mems'] if m.inst_name == 'data_mem')
        assert data_mem.get_property('mementries') == 256
        assert data_mem.get_property('memwidth') == 32
        
        small_mem = next(m for m in nodes['mems'] if m.inst_name == 'small_mem')
        assert small_mem.get_property('mementries') == 16
        assert small_mem.get_property('memwidth') == 32
    
    def test_memory_descriptor_generation(self):
        """Test that memory descriptors are generated in C++ header"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(MEMORY_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="mem_test")
            
            # Read generated header
            with open(os.path.join(tmpdir, 'mem_test_descriptors.hpp'), 'r') as f:
                content = f.read()
            
            # Verify MemoryBase class is present
            assert 'class MemoryBase' in content
            assert 'template<typename EntryType>' in content
            
            # Verify memory classes are generated
            assert 'class data_mem_t' in content
            assert 'class small_mem_t' in content
            
            # Verify MemoryBase inheritance
            assert 'MemoryBase<entry_t>' in content
            
            # Verify vector header included
            assert '#include <vector>' in content
    
    def test_memory_bindings_generation(self):
        """Test that memory bindings are generated with list-like interface"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(MEMORY_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="mem_test")
            
            # Read generated bindings
            with open(os.path.join(tmpdir, 'mem_test_bindings.cpp'), 'r') as f:
                content = f.read()
            
            # Verify list-like interface bindings
            assert '__len__' in content
            assert '__getitem__' in content
            assert '__iter__' in content
            
            # Verify slice support
            assert 'py::slice' in content
            
            # Verify pybind11/stl.h included
            assert '#include <pybind11/stl.h>' in content
            
            # Verify memory class bindings
            assert 'data_mem_t' in content
            assert 'small_mem_t' in content
    
    def test_memory_stubs_generation(self):
        """Test that memory type stubs are generated with proper type hints"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(MEMORY_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="mem_test")
            
            # Read generated stubs
            with open(os.path.join(tmpdir, '__init__.pyi'), 'r') as f:
                content = f.read()
            
            # Verify memory class stubs
            assert 'class data_mem_t' in content
            assert 'class small_mem_t' in content
            
            # Verify type hints for list-like interface
            assert '__len__' in content
            assert '__getitem__' in content
            assert '__iter__' in content
            
            # Verify typing imports
            assert 'Iterator' in content
            assert 'overload' in content
            
            # Verify overload decorators for __getitem__
            assert '@overload' in content
            
            # Verify return types
            assert 'list[entry_t]' in content or 'list[' in content
    
    def test_memory_in_top_level_soc(self):
        """Test that memory is accessible from top-level SoC class"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(MEMORY_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="mem_test")
            
            # Read generated header
            with open(os.path.join(tmpdir, 'mem_test_descriptors.hpp'), 'r') as f:
                content = f.read()
            
            # Verify memory members in top-level class
            assert 'data_mem_t data_mem' in content
            assert 'small_mem_t small_mem' in content
    
    def test_memory_with_split_bindings(self):
        """Test that memory works with split bindings"""
        # Create RDL with both memory and many registers
        rdl_content = """
addrmap large_soc {
    reg test_reg_t {
        field { sw = rw; hw = r; } data[7:0];
    };
    
    external mem {
        mementries = 128;
        memwidth = 32;
        test_reg_t entry;
    } test_mem @ 0x1000;
"""
        for i in range(10):
            rdl_content += f"""
    reg {{
        field {{ sw = rw; hw = r; }} field{i}[7:0];
    }} reg{i} @ 0x{0x2000 + i*4:04x};
"""
        rdl_content += "};\n"
        
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(rdl_content))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            # Enable split bindings
            exporter.export(root.top, tmpdir, soc_name="large_soc", split_bindings=5)
            
            # Verify main bindings file exists
            assert os.path.exists(os.path.join(tmpdir, 'large_soc_bindings.cpp'))
            
            # Read main bindings and verify memory is bound
            with open(os.path.join(tmpdir, 'large_soc_bindings.cpp'), 'r') as f:
                content = f.read()
            
            # Memory bindings should be in the main file
            assert 'test_mem_t' in content
            assert '__getitem__' in content
    
    def test_memory_set_offset(self):
        """Test that memory has set_offset method for proper initialization"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(MEMORY_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="mem_test")
            
            # Read generated header
            with open(os.path.join(tmpdir, 'mem_test_descriptors.hpp'), 'r') as f:
                content = f.read()
            
            # Verify set_offset method exists in memory class
            assert 'void set_offset(uint64_t base_offset)' in content
    
    @staticmethod
    def _write_rdl(content):
        """Write RDL content to a temporary file"""
        fd, path = tempfile.mkstemp(suffix='.rdl')
        os.write(fd, content.encode('utf-8'))
        os.close(fd)
        return path
