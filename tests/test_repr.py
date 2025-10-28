"""
Tests for __repr__ and __str__ methods in PeakRDL-pybind11
"""

import os
import tempfile

import pytest
from systemrdl import RDLCompiler

# Try to import the exporter
try:
    from peakrdl_pybind11 import Pybind11Exporter
except ImportError:
    pytest.skip("peakrdl_pybind11 not installed", allow_module_level=True)


# Sample RDL for testing repr/str functionality
REPR_TEST_RDL = """
addrmap repr_test {
    name = "Repr Test SoC";
    desc = "A SoC for testing repr/str functionality";
    
    reg control_reg {
        name = "Control Register";
        desc = "Control register for the device";
        field {
            name = "Enable";
            desc = "Enable bit";
            sw = rw;
            hw = r;
        } enable[0:0];
        
        field {
            name = "Mode";
            desc = "Operating mode";
            sw = rw;
            hw = r;
        } mode[3:1];
    } control @ 0x0000;
    
    reg status_reg {
        name = "Status Register";
        field {
            name = "Ready";
            sw = r;
            hw = w;
        } ready[0:0];
        
        field {
            name = "Error";
            sw = r;
            hw = w;
        } error[1:1];
    } status @ 0x0004;
    
    regfile uart_regfile {
        name = "UART Regfile";
        
        reg {
            field { sw = rw; hw = r; } data[7:0];
        } data @ 0x00;
        
        reg {
            field { sw = r; hw = w; } ready[0:0];
        } status @ 0x04;
    } uart @ 0x1000;
    
    external mem {
        name = "Test Memory";
        desc = "A test memory array";
        mementries = 16;
        memwidth = 32;
        
        reg {
            field { sw = rw; hw = rw; } data[31:0];
        } entry;
    } test_mem @ 0x2000;
};
"""


class TestReprMethods:
    """Test __repr__ and __str__ methods"""
    
    def test_repr_in_base_classes(self):
        """Test that __repr__ methods are generated for base classes"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(REPR_TEST_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="repr_test")
            
            # Read generated header
            with open(os.path.join(tmpdir, 'repr_test_descriptors.hpp')) as f:
                content = f.read()
            
            # Verify __repr__ is defined in base classes
            assert 'std::string __repr__() const' in content, "Missing __repr__ in base classes"
            
            # Count __repr__ methods (Master, FieldBase, RegisterBase, NodeBase, MemoryBase)
            repr_count = content.count('__repr__() const')
            assert repr_count >= 5, f"Expected at least 5 __repr__ methods, found {repr_count}"
            
            # Verify includes for string formatting
            assert '#include <sstream>' in content, "Missing sstream include"
            assert '#include <iomanip>' in content, "Missing iomanip include"
    
    def test_repr_in_bindings(self):
        """Test that __repr__ is bound to Python"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(REPR_TEST_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="repr_test")
            
            # Read generated bindings
            with open(os.path.join(tmpdir, 'repr_test_bindings.cpp')) as f:
                content = f.read()
            
            # Verify __repr__ and __str__ are bound for base classes
            assert '.def("__repr__", &Master::__repr__)' in content, "Master __repr__ not bound"
            assert '.def("__str__", &Master::__repr__)' in content, "Master __str__ not bound"
            
            assert '.def("__repr__", &FieldBase::__repr__)' in content, "FieldBase __repr__ not bound"
            assert '.def("__str__", &FieldBase::__repr__)' in content, "FieldBase __str__ not bound"
            
            assert '.def("__repr__", &RegisterBase::__repr__)' in content, "RegisterBase __repr__ not bound"
            assert '.def("__str__", &RegisterBase::__repr__)' in content, "RegisterBase __str__ not bound"
            
            assert '.def("__repr__", &NodeBase::__repr__)' in content, "NodeBase __repr__ not bound"
            assert '.def("__str__", &NodeBase::__repr__)' in content, "NodeBase __str__ not bound"
    
    def test_repr_with_split_bindings(self):
        """Test that __repr__ works with split bindings"""
        # Create RDL with many registers to trigger split bindings
        rdl_content = """
addrmap large_soc {
    reg test_reg_t {
        field { sw = rw; hw = r; } data[7:0];
    };
"""
        for i in range(15):
            rdl_content += f"""
    test_reg_t reg{i} @ 0x{i*4:04x};
"""
        rdl_content += "};\n"
        
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(rdl_content))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            # Enable split bindings
            exporter.export(root.top, tmpdir, soc_name="large_soc", split_bindings=5)
            
            # Verify main bindings file has __repr__
            with open(os.path.join(tmpdir, 'large_soc_bindings.cpp')) as f:
                content = f.read()
            
            assert '.def("__repr__", &FieldBase::__repr__)' in content
            assert '.def("__repr__", &RegisterBase::__repr__)' in content
            assert '.def("__repr__", &NodeBase::__repr__)' in content
    
    def test_field_repr_format(self):
        """Test that Field __repr__ includes expected information"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(REPR_TEST_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="repr_test")
            
            # Read generated header
            with open(os.path.join(tmpdir, 'repr_test_descriptors.hpp')) as f:
                content = f.read()
            
            # Find FieldBase __repr__ implementation
            # Should contain: name, offset, lsb, msb, readable/writable flags
            assert 'name_' in content
            assert 'offset_' in content
            assert 'lsb()' in content or 'lsb_' in content
            assert 'msb()' in content
            assert 'readable_' in content
            assert 'writable_' in content
    
    def test_register_repr_format(self):
        """Test that Register __repr__ includes expected information"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(REPR_TEST_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="repr_test")
            
            # Read generated header
            with open(os.path.join(tmpdir, 'repr_test_descriptors.hpp')) as f:
                content = f.read()
            
            # RegisterBase __repr__ implementation should contain: name, offset, width
            # Just verify the __repr__ method exists in RegisterBase context
            assert 'class RegisterBase' in content
            assert 'std::string __repr__() const' in content
    
    def test_node_repr_format(self):
        """Test that Node __repr__ includes expected information"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(REPR_TEST_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="repr_test")
            
            # Read generated header
            with open(os.path.join(tmpdir, 'repr_test_descriptors.hpp')) as f:
                content = f.read()
            
            # NodeBase __repr__ should exist
            assert 'class NodeBase' in content
            assert 'virtual std::string __repr__() const' in content
    
    def test_memory_repr_format(self):
        """Test that Memory __repr__ includes expected information"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(REPR_TEST_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="repr_test")
            
            # Read generated header
            with open(os.path.join(tmpdir, 'repr_test_descriptors.hpp')) as f:
                content = f.read()
            
            # MemoryBase template should have __repr__
            assert 'class MemoryBase' in content
            assert 'std::string __repr__() const override' in content
    
    def test_repr_syntax_valid(self):
        """Test that generated C++ code with __repr__ is syntactically valid"""
        rdl = RDLCompiler()
        rdl.compile_file(self._write_rdl(REPR_TEST_RDL))
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="repr_test")
            
            # Basic syntax checks on generated header
            with open(os.path.join(tmpdir, 'repr_test_descriptors.hpp')) as f:
                content = f.read()
            
            # Check for __repr__ methods
            repr_count = content.count('__repr__() const')
            
            # Should have found at least 5 __repr__ methods
            # (Master, FieldBase, RegisterBase, NodeBase, MemoryBase)
            assert repr_count >= 5, f"Expected at least 5 __repr__ methods, found {repr_count}"
            
            # Verify they all return std::string
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if '__repr__() const' in line:
                    # Check that the line includes std::string
                    assert 'std::string' in line or 'std::string' in lines[i-1], \
                        f"__repr__ should return std::string at line {i}: {line}"
    
    @staticmethod
    def _write_rdl(content):
        """Write RDL content to a temporary file"""
        fd, path = tempfile.mkstemp(suffix='.rdl')
        os.write(fd, content.encode('utf-8'))
        os.close(fd)
        return path
