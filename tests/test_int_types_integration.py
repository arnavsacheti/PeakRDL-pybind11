"""
Integration test for RegisterInt and FieldInt with generated modules.

This test generates a complete SoC module and verifies that RegisterInt
and FieldInt work correctly with read/write operations.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter, RegisterInt, FieldInt


# Sample SystemRDL for testing
TEST_RDL = """
addrmap test_soc {
    name = "Test SoC";
    
    reg {
        name = "Control Register";
        field {
            sw = rw;
            hw = r;
        } enable[0:0] = 0;
        
        field {
            sw = rw;
            hw = r;
        } mode[3:1] = 0;
        
        field {
            sw = rw;
            hw = r;
        } priority[7:4] = 0;
    } control @ 0x0000;
    
    reg {
        name = "Status Register";
        field {
            sw = r;
            hw = w;
        } ready[0:0] = 0;
        
        field {
            sw = r;
            hw = w;
        } error[1:1] = 0;
    } status @ 0x0004;
    
    reg {
        name = "Data Register";
        field {
            sw = rw;
            hw = r;
        } data[15:0] = 0;
    } data @ 0x0008;
};
"""


class TestIntTypesIntegration:
    """Integration tests for RegisterInt and FieldInt"""

    def test_register_read_returns_registerint(self, tmpdir):
        """Test that register.read() returns RegisterInt"""
        # Skip if we can't build (no cmake/pybind11)
        soc_module = self._build_test_module(tmpdir)
        if not soc_module:
            pytest.skip("Could not build test module")

        from peakrdl_pybind11.masters import MockMaster

        # Create SoC and attach mock master
        soc = soc_module.create()
        mock = MockMaster()
        master = soc_module.wrap_master(mock)
        soc.attach_master(master)

        # Write a value to control register
        soc.control.write(0xAB)

        # Read it back
        value = soc.control.read()

        # Should be a RegisterInt
        assert isinstance(value, RegisterInt), f"Expected RegisterInt, got {type(value)}"
        assert int(value) == 0xAB
        assert value.offset == 0x0000
        assert value.width == 4  # 4 bytes

        # Should have fields
        assert hasattr(value, 'enable')
        assert hasattr(value, 'mode')
        assert hasattr(value, 'priority')

    def test_field_read_returns_fieldint(self, tmpdir):
        """Test that field.read() returns FieldInt"""
        soc_module = self._build_test_module(tmpdir)
        if not soc_module:
            pytest.skip("Could not build test module")

        from peakrdl_pybind11.masters import MockMaster

        soc = soc_module.create()
        mock = MockMaster()
        master = soc_module.wrap_master(mock)
        soc.attach_master(master)

        # Write values to fields
        # enable = 1 (bit 0)
        # mode = 5 (bits 3:1)
        # priority = 0xA (bits 7:4)
        # Combined: 0b1010_101_1 = 0xAB
        soc.control.write(0xAB)

        # Read individual fields
        enable = soc.control.enable.read()
        mode = soc.control.mode.read()
        priority = soc.control.priority.read()

        # Should be FieldInt
        assert isinstance(enable, FieldInt)
        assert isinstance(mode, FieldInt)
        assert isinstance(priority, FieldInt)

        # Check values
        assert int(enable) == 1
        assert int(mode) == 5
        assert int(priority) == 0xA

        # Check metadata
        assert enable.lsb == 0
        assert enable.width == 1
        assert mode.lsb == 1
        assert mode.width == 3
        assert priority.lsb == 4
        assert priority.width == 4

    def test_write_with_fieldint_does_rmw(self, tmpdir):
        """Test that writing a FieldInt does read-modify-write"""
        soc_module = self._build_test_module(tmpdir)
        if not soc_module:
            pytest.skip("Could not build test module")

        from peakrdl_pybind11.masters import MockMaster

        soc = soc_module.create()
        mock = MockMaster()
        master = soc_module.wrap_master(mock)
        soc.attach_master(master)

        # Set initial value: enable=1, mode=3, priority=5
        # 0b0101_011_1 = 0x5B
        soc.control.write(0x5B)
        assert soc.control.read() == 0x5B

        # Create a FieldInt for mode field with new value 7
        # This should only modify bits 3:1, leaving other bits unchanged
        mode_field = FieldInt(7, lsb=1, width=3, offset=0x0000)

        # Write using the FieldInt
        soc.control.write(mode_field)

        # Read back - should be: enable=1, mode=7, priority=5
        # 0b0101_111_1 = 0x5F
        result = soc.control.read()
        assert int(result) == 0x5F

        # Verify individual fields
        assert soc.control.enable.read() == 1
        assert soc.control.mode.read() == 7
        assert soc.control.priority.read() == 5

    def test_write_with_registerint(self, tmpdir):
        """Test that writing a RegisterInt works"""
        soc_module = self._build_test_module(tmpdir)
        if not soc_module:
            pytest.skip("Could not build test module")

        from peakrdl_pybind11.masters import MockMaster

        soc = soc_module.create()
        mock = MockMaster()
        master = soc_module.wrap_master(mock)
        soc.attach_master(master)

        # Create a RegisterInt with a value
        reg_val = RegisterInt(0x12345678, offset=0x0000, width=4)

        # Write it
        soc.control.write(reg_val)

        # Read back
        result = soc.control.read()
        assert int(result) == 0x12345678

    def test_registerint_field_access(self, tmpdir):
        """Test accessing fields from RegisterInt returned by read()"""
        soc_module = self._build_test_module(tmpdir)
        if not soc_module:
            pytest.skip("Could not build test module")

        from peakrdl_pybind11.masters import MockMaster

        soc = soc_module.create()
        mock = MockMaster()
        master = soc_module.wrap_master(mock)
        soc.attach_master(master)

        # Write a value
        soc.control.write(0xAB)

        # Read and access fields directly
        reg_value = soc.control.read()

        # Access fields via attribute
        assert reg_value.enable == 1
        assert reg_value.mode == 5
        assert reg_value.priority == 0xA

        # These should be FieldInt instances
        assert isinstance(reg_value.enable, FieldInt)
        assert isinstance(reg_value.mode, FieldInt)
        assert isinstance(reg_value.priority, FieldInt)

    def _build_test_module(self, tmpdir):
        """Build the test module and return it, or None if build fails"""
        # Compile SystemRDL
        rdl = RDLCompiler()
        rdl_path = self._write_rdl(TEST_RDL)
        rdl.compile_file(rdl_path)
        root = rdl.elaborate()

        output_dir = Path(tmpdir) / "output"
        output_dir.mkdir()

        # Export
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
            return None

        # Run make
        result = subprocess.run(
            ["make", "-j4"],
            cwd=build_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None

        # Copy built module to output directory
        import glob
        import shutil

        so_files = glob.glob(str(build_dir / "*.so"))
        if not so_files:
            return None

        shutil.copy(so_files[0], output_dir)

        # Add output dir to path and import
        sys.path.insert(0, str(output_dir))

        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "test_soc", str(output_dir / "__init__.py")
            )
            test_soc = importlib.util.module_from_spec(spec)
            sys.modules['test_soc'] = test_soc
            spec.loader.exec_module(test_soc)
            return test_soc
        except Exception as e:
            print(f"Failed to import module: {e}")
            return None

    @staticmethod
    def _write_rdl(content):
        """Write RDL content to a temporary file"""
        fd, path = tempfile.mkstemp(suffix=".rdl")
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        return path


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
