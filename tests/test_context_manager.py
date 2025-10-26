"""
Tests for register context manager functionality
"""

import os
import tempfile
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter


# Sample SystemRDL for testing context managers
CONTEXT_MANAGER_RDL = """
addrmap test_soc {
    name = "Test SoC for Context Managers";
    
    reg {
        name = "Control Register";
        field {
            sw = rw;
            hw = r;
        } field1[0:0] = 0;
        
        field {
            sw = rw;
            hw = r;
        } field2[7:1] = 0;
        
        field {
            sw = rw;
            hw = r;
        } field3[8:8] = 0;
    } control @ 0x0000;
    
    reg {
        name = "Status Register";
        field {
            sw = r;
            hw = w;
        } ready[0:0];
    } status @ 0x0004;
};
"""


class TestContextManager:
    """Test register context manager functionality"""

    def test_context_manager_basic(self, tmpdir):
        """Test basic context manager usage"""
        import subprocess
        import sys

        # Compile and export
        rdl = RDLCompiler()
        rdl_path = self._write_rdl(CONTEXT_MANAGER_RDL)
        rdl.compile_file(rdl_path)
        root = rdl.elaborate()

        output_dir = Path(tmpdir) / "output"
        output_dir.mkdir()

        exporter = Pybind11Exporter()
        exporter.export(root.top, str(output_dir), soc_name="test_soc")

        # Build the extension
        build_dir = output_dir / "build"
        build_dir.mkdir()

        # Run cmake
        result = subprocess.run(
            ["cmake", ".."],
            cwd=build_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"CMake failed (pybind11 may not be installed): {result.stderr}")

        # Run make
        result = subprocess.run(
            ["make", "-j4"],
            cwd=build_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"Build failed: {result.stderr}")

        # Copy built module to output directory
        import glob

        so_files = glob.glob(str(build_dir / "*.so"))
        if not so_files:
            pytest.skip("No .so file found after build")

        import shutil

        shutil.copy(so_files[0], output_dir)

        # Create a test script
        test_script = output_dir / "test_context.py"
        test_script.write_text(
            """
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, %r)

import importlib.util
spec = importlib.util.spec_from_file_location("test_soc", os.path.join(os.path.dirname(__file__), "__init__.py"))
test_soc = importlib.util.module_from_spec(spec)
sys.modules['test_soc'] = test_soc
spec.loader.exec_module(test_soc)

from peakrdl_pybind11.masters import MockMaster

# Test basic context manager
soc = test_soc.create()
mock = MockMaster()
master = test_soc.wrap_master(mock)
soc.attach_master(master)

# Set initial value
soc.control.write(0x00)
assert soc.control.read() == 0x00

# Use context manager
with soc.control as reg:
    # Modify value in context
    reg.write(0x15)
    
    # Cached value should be updated
    assert reg.read() == 0x15
    
    # But hardware value should not be written yet
    assert mock.read(soc.control.offset, soc.control.width) == 0x00

# After exiting context, value should be written
assert soc.control.read() == 0x15
assert mock.read(soc.control.offset, soc.control.width) == 0x15

# Test field modification in context
soc.control.write(0x00)
with soc.control as reg:
    reg.field1.write(1)
    reg.field2.write(0x7F)
    reg.field3.write(1)
    
    # Verify cached value
    # field1 = 1 (bit 0)
    # field2 = 0x7F (bits 7:1)
    # field3 = 1 (bit 8)
    expected = 0b1_1111111_1
    assert reg.read() == expected

# After exit, verify it was written
assert soc.control.read() == expected

print("All context manager tests passed!")
"""
            % str(Path(__file__).parent.parent / "src")
        )

        # Run the test script
        result = subprocess.run(
            [sys.executable, str(test_script)],
            capture_output=True,
            text=True,
            cwd=output_dir,
        )

        # Check result
        assert result.returncode == 0, f"Test script failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        assert "All context manager tests passed!" in result.stdout

    def test_nested_context_error(self, tmpdir):
        """Test that nested contexts raise an error"""
        import subprocess
        import sys

        # Compile and export
        rdl = RDLCompiler()
        rdl_path = self._write_rdl(CONTEXT_MANAGER_RDL)
        rdl.compile_file(rdl_path)
        root = rdl.elaborate()

        output_dir = Path(tmpdir) / "output"
        output_dir.mkdir()

        exporter = Pybind11Exporter()
        exporter.export(root.top, str(output_dir), soc_name="test_soc")

        # Build the extension
        build_dir = output_dir / "build"
        build_dir.mkdir()

        # Run cmake
        result = subprocess.run(
            ["cmake", ".."],
            cwd=build_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"CMake failed (pybind11 may not be installed): {result.stderr}")

        # Run make
        result = subprocess.run(
            ["make", "-j4"],
            cwd=build_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"Build failed: {result.stderr}")

        # Copy built module
        import glob
        import shutil

        so_files = glob.glob(str(build_dir / "*.so"))
        if not so_files:
            pytest.skip("No .so file found after build")
        shutil.copy(so_files[0], output_dir)

        # Create a test script that tests nested context error
        test_script = output_dir / "test_nested.py"
        test_script.write_text(
            """
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, %r)

import importlib.util
spec = importlib.util.spec_from_file_location("test_soc", os.path.join(os.path.dirname(__file__), "__init__.py"))
test_soc = importlib.util.module_from_spec(spec)
sys.modules['test_soc'] = test_soc
spec.loader.exec_module(test_soc)

from peakrdl_pybind11.masters import MockMaster

soc = test_soc.create()
mock = MockMaster()
master = test_soc.wrap_master(mock)
soc.attach_master(master)

# Try nested context - should raise error
try:
    with soc.control as reg:
        with soc.control as reg2:
            pass
    print("ERROR: Expected RuntimeError for nested context")
    sys.exit(1)
except RuntimeError as e:
    if "already in a context" in str(e):
        print("Correctly raised error for nested context")
    else:
        print(f"ERROR: Wrong error message: {e}")
        sys.exit(1)

print("Nested context test passed!")
"""
            % str(Path(__file__).parent.parent / "src")
        )

        # Run the test script
        result = subprocess.run(
            [sys.executable, str(test_script)],
            capture_output=True,
            text=True,
            cwd=output_dir,
        )

        # Check result
        assert result.returncode == 0, f"Test script failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        assert "Nested context test passed!" in result.stdout

    @staticmethod
    def _write_rdl(content):
        """Write RDL content to a temporary file"""
        fd, path = tempfile.mkstemp(suffix=".rdl")
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        return path
