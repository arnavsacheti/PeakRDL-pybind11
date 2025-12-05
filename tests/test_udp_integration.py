"""
Integration tests for flag and enum UDP properties
"""

import tempfile
import shutil
import subprocess
from pathlib import Path

import pytest
from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter


# RDL content with flag and enum UDP properties
RDL_CONTENT = '''
property flag {
    component = reg;
    type = boolean;
};

property is_enum {
    component = reg;
    type = boolean;
};

addrmap test_udp {
    name = "Test UDP SoC";
    desc = "Test SoC with flag and enum registers";
    
    // Normal register - no special properties
    reg {
        name = "Normal Register";
        field {
            name = "Data";
            sw = rw;
            hw = r;
        } data[7:0] = 0;
    } normal @ 0x00;
    
    // Flag register - each field becomes a flag bit
    reg {
        name = "Status Flags";
        flag = true;
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
        
        field {
            name = "Busy";
            sw = r;
            hw = w;
        } busy[2:2];
    } status_flags @ 0x04;
    
    // Enum register - each field becomes an enum value
    reg {
        name = "Mode Enum";
        is_enum = true;
        field {
            name = "Idle";
            sw = rw;
            hw = r;
        } idle[0:0];
        
        field {
            name = "Running";
            sw = rw;
            hw = r;
        } running[1:1];
    } mode_enum @ 0x08;
};
'''


@pytest.fixture
def temp_output_dir():
    """Create a temporary directory for test output"""
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    shutil.rmtree(tmpdir)


@pytest.fixture
def rdl_file(temp_output_dir):
    """Create a temporary RDL file"""
    rdl_path = temp_output_dir / "test_udp.rdl"
    rdl_path.write_text(RDL_CONTENT)
    return rdl_path


def test_udp_detection(rdl_file, temp_output_dir):
    """Test that UDP properties are detected and collected"""
    # Compile RDL
    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_file))
    root = rdl.elaborate()
    
    # Export with Pybind11Exporter
    exporter = Pybind11Exporter()
    output_dir = temp_output_dir / "output"
    exporter.export(root, str(output_dir), soc_name="test_udp")
    
    # Check that the runtime was generated
    runtime_file = output_dir / "__init__.py"
    assert runtime_file.exists(), "Runtime file should exist"
    
    # Read the runtime file and check for flag/enum generation
    runtime_content = runtime_file.read_text()
    
    # Check for flag register
    assert "status_flags_Flags" in runtime_content, "Flag class should be generated"
    assert "RegisterIntFlag" in runtime_content, "RegisterIntFlag should be imported"
    assert "READY" in runtime_content, "READY flag member should exist"
    assert "ERROR" in runtime_content, "ERROR flag member should exist"
    assert "BUSY" in runtime_content, "BUSY flag member should exist"
    
    # Check for enum register
    assert "mode_enum_Enum" in runtime_content, "Enum class should be generated"
    assert "RegisterIntEnum" in runtime_content, "RegisterIntEnum should be imported"
    assert "IDLE" in runtime_content, "IDLE enum member should exist"
    assert "RUNNING" in runtime_content, "RUNNING enum member should exist"


def test_flag_and_enum_in_nodes(rdl_file):
    """Test that flag and enum registers are tracked in nodes"""
    # Compile RDL
    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_file))
    root = rdl.elaborate()
    
    # Create exporter and collect nodes
    exporter = Pybind11Exporter()
    exporter.top_node = root.top
    nodes = exporter._collect_nodes(root.top)
    
    # Check that we have the right number of registers
    assert len(nodes["regs"]) == 3, "Should have 3 registers total"
    assert len(nodes["flag_regs"]) == 1, "Should have 1 flag register"
    assert len(nodes["enum_regs"]) == 1, "Should have 1 enum register"
    
    # Check that the flag register is the right one
    flag_reg = nodes["flag_regs"][0]
    assert flag_reg.inst_name == "status_flags", "Flag register should be status_flags"
    
    # Check that the enum register is the right one
    enum_reg = nodes["enum_regs"][0]
    assert enum_reg.inst_name == "mode_enum", "Enum register should be mode_enum"


def test_normal_register_not_flagged(rdl_file):
    """Test that normal registers are not marked as flag or enum"""
    # Compile RDL
    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_file))
    root = rdl.elaborate()
    
    # Create exporter and collect nodes
    exporter = Pybind11Exporter()
    exporter.top_node = root.top
    nodes = exporter._collect_nodes(root.top)
    
    # Find the normal register
    normal_regs = [r for r in nodes["regs"] if r.inst_name == "normal"]
    assert len(normal_regs) == 1, "Should have exactly one normal register"
    
    normal_reg = normal_regs[0]
    
    # Check that it's not in flag_regs or enum_regs
    assert normal_reg not in nodes["flag_regs"], "Normal register should not be a flag register"
    assert normal_reg not in nodes["enum_regs"], "Normal register should not be an enum register"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
