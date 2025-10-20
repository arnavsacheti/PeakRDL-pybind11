"""
Integration test demonstrating the PeakRDL-pybind11 workflow

This test demonstrates the complete workflow of:
1. Exporting SystemRDL to PyBind11 modules
2. Building the C++ extension  
3. Using the generated Python API

Note: This test requires systemrdl-compiler to be installed
"""

import os
import sys
import tempfile
from pathlib import Path

def test_workflow_demonstration():
    """
    Demonstrate the complete workflow
    
    This is a documentation test that shows how the exporter would be used
    """
    
    # Example SystemRDL content
    rdl_content = """
    addrmap demo {
        name = "Demo SoC";
        
        reg {
            field {
                sw = rw;
                hw = r;
            } enable[0:0];
        } control @ 0x0000;
        
        reg {
            field {
                sw = r;
                hw = w;
            } ready[0:0];
        } status @ 0x0004;
    };
    """
    
    print("=" * 70)
    print("PeakRDL-pybind11 Workflow Demonstration")
    print("=" * 70)
    print()
    
    print("Step 1: Compile SystemRDL")
    print("-" * 70)
    print("Input RDL:")
    print(rdl_content)
    print()
    
    print("Code:")
    print("""
    from systemrdl import RDLCompiler
    from peakrdl_pybind11 import Pybind11Exporter
    
    # Compile SystemRDL
    rdl = RDLCompiler()
    rdl.compile_file("demo.rdl")
    root = rdl.elaborate()
    
    # Export to PyBind11
    exporter = Pybind11Exporter()
    exporter.export(root.top, "output", soc_name="demo")
    """)
    print()
    
    print("Step 2: Generated Files")
    print("-" * 70)
    print("The exporter generates:")
    print("  ✓ demo_descriptors.hpp  - C++ register descriptors")
    print("  ✓ demo_bindings.cpp     - PyBind11 bindings")
    print("  ✓ __init__.py          - Python runtime module")
    print("  ✓ setup.py             - Build script for C++ extension")
    print("  ✓ __init__.pyi         - Type stub file")
    print()
    
    print("Step 3: Build C++ Extension")
    print("-" * 70)
    print("Command:")
    print("  $ cd output")
    print("  $ python setup.py build_ext --inplace")
    print()
    
    print("Step 4: Use Generated Python API")
    print("-" * 70)
    print("Code:")
    print("""
    import demo
    from peakrdl_pybind11.masters import MockMaster
    
    # Create SoC instance
    soc = demo.create()
    
    # Attach a master for register access
    master = MockMaster()
    soc.attach_master(master)
    
    # Access registers
    soc.control.write(0x1)          # Write register
    value = soc.control.read()       # Read register
    
    # Access fields
    soc.control.enable.write(1)      # Write field
    enabled = soc.control.enable.read()  # Read field
    
    # Check status
    if soc.status.ready.read():
        print("Device ready!")
    """)
    print()
    
    print("Step 5: Alternative Master Backends")
    print("-" * 70)
    print("Mock Master (testing):")
    print("""
    from peakrdl_pybind11.masters import MockMaster
    master = MockMaster()
    soc.attach_master(master)
    """)
    print()
    
    print("OpenOCD Master (JTAG/SWD debugging):")
    print("""
    from peakrdl_pybind11.masters import OpenOCDMaster
    master = OpenOCDMaster(host="localhost", port=6666)
    soc.attach_master(master)
    """)
    print()
    
    print("SSH Master (remote access):")
    print("""
    from peakrdl_pybind11.masters import SSHMaster
    master = SSHMaster(host="target.local", username="root")
    soc.attach_master(master)
    """)
    print()
    
    print("Callback Master (custom backend):")
    print("""
    from peakrdl_pybind11.masters import CallbackMaster
    
    def my_read(address, width):
        # Custom read implementation
        return read_from_hardware(address)
    
    def my_write(address, value, width):
        # Custom write implementation
        write_to_hardware(address, value)
    
    master = CallbackMaster(read_callback=my_read, write_callback=my_write)
    soc.attach_master(master)
    """)
    print()
    
    print("=" * 70)
    print("Workflow demonstration complete!")
    print("=" * 70)
    print()
    print("For a working example, see: examples/run_example.py")
    print()

if __name__ == "__main__":
    test_workflow_demonstration()
