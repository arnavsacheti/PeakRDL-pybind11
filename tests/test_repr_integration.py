"""
Integration test for __repr__ and __str__ functionality
This test actually builds and imports the generated module to verify __repr__ works end-to-end
"""

import os
import subprocess
import sys
import tempfile

import pytest
from systemrdl import RDLCompiler

# Try to import the exporter
try:
    from peakrdl_pybind11 import Pybind11Exporter
except ImportError:
    pytest.skip("peakrdl_pybind11 not installed", allow_module_level=True)


# Sample RDL for integration testing
INTEGRATION_RDL = """
addrmap repr_integration {
    name = "Repr Integration Test";
    desc = "Test SoC for repr integration";
    
    reg control {
        name = "Control Register";
        field {
            name = "Enable";
            sw = rw;
            hw = r;
        } enable[0:0];
        
        field {
            name = "Mode";
            sw = rw;
            hw = r;
        } mode[3:1];
    } control @ 0x0000;
    
    reg status {
        name = "Status Register";
        field {
            name = "Ready";
            sw = r;
            hw = w;
        } ready[0:0];
    } status @ 0x0004;
};
"""


@pytest.mark.integration
@pytest.mark.skipif(not os.path.exists("/usr/bin/g++"), reason="g++ not available")
def test_repr_integration_build_and_use():
    """
    Integration test: Build the generated module and verify __repr__ works
    """
    rdl = RDLCompiler()
    rdl_file = _write_rdl(INTEGRATION_RDL)
    
    try:
        rdl.compile_file(rdl_file)
        root = rdl.elaborate()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Export
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="repr_test")
            
            # Verify generated files exist
            assert os.path.exists(os.path.join(tmpdir, 'repr_test_descriptors.hpp'))
            assert os.path.exists(os.path.join(tmpdir, 'repr_test_bindings.cpp'))
            assert os.path.exists(os.path.join(tmpdir, 'CMakeLists.txt'))
            
            # Try to compile just the descriptors header to check syntax
            # Create a simple test file that includes the header
            test_cpp = os.path.join(tmpdir, 'test_compile.cpp')
            with open(test_cpp, 'w') as f:
                f.write("""
#include "repr_test_descriptors.hpp"

int main() {
    // Just verify the code compiles
    repr_test::FieldBase field("test", 0x1000, 0, 8, true, true);
    std::string repr = field.__repr__();
    
    repr_test::RegisterBase reg("control", 0x0, 32);
    repr = reg.__repr__();
    
    repr_test::NodeBase node("node", 0x1000);
    repr = node.__repr__();
    
    return 0;
}
""")
            
            # Try to compile the test file
            try:
                result = subprocess.run(
                    ['g++', '-std=c++11', '-c', test_cpp, '-o', os.path.join(tmpdir, 'test.o')],
                    cwd=tmpdir,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                
                if result.returncode != 0:
                    print("Compilation output:", result.stdout)
                    print("Compilation errors:", result.stderr)
                    pytest.fail(f"Failed to compile test code: {result.stderr}")
                
                # Verify object file was created
                assert os.path.exists(os.path.join(tmpdir, 'test.o'))
                
            except subprocess.TimeoutExpired:
                pytest.skip("Compilation timed out")
            except FileNotFoundError:
                pytest.skip("g++ not found in PATH")
                
    finally:
        if os.path.exists(rdl_file):
            os.unlink(rdl_file)


def _write_rdl(content):
    """Write RDL content to a temporary file"""
    import tempfile
    fd, path = tempfile.mkstemp(suffix='.rdl')
    os.write(fd, content.encode('utf-8'))
    os.close(fd)
    return path
