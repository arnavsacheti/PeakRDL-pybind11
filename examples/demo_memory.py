#!/usr/bin/env python3
"""
Demonstration of memory type list-like interface in PeakRDL-pybind11

This script shows how memory types can be accessed like Python lists:
- Single element access: mem[0]
- Slicing: mem[0:10], mem[::2]
- Iteration: for entry in mem
- List comprehension: [entry.read() for entry in mem]
"""

import os
import tempfile

from systemrdl import RDLCompiler
from peakrdl_pybind11 import Pybind11Exporter



# Sample SystemRDL with memory
DEMO_RDL = """
addrmap demo_soc {
    name = "Demo SoC with Memory";
    desc = "Demonstrates memory array functionality";
    
    reg status_reg_t {
        name = "Status Register";
        desc = "Status register for each memory entry";
        
        field {
            name = "Valid";
            desc = "Entry is valid";
            sw = rw;
            hw = rw;
        } valid[0:0] = 0;
        
        field {
            name = "Ready";
            desc = "Entry is ready";
            sw = r;
            hw = w;
        } ready[1:1] = 0;
        
        field {
            name = "Error";
            desc = "Entry has error";
            sw = r;
            hw = w;
        } error[2:2] = 0;
        
        field {
            name = "Data";
            desc = "Status data";
            sw = rw;
            hw = rw;
        } data[31:8] = 0;
    };
    
    external mem {
        name = "Status Memory";
        desc = "Memory array of status registers";
        mementries = 256;
        memwidth = 32;
        
        status_reg_t entry;
    } status_mem @ 0x10000;
    
    reg control_t {
        name = "Control Register";
        field {
            name = "Enable";
            sw = rw;
            hw = r;
        } enable[0:0] = 1;
    };
    
    control_t ctrl @ 0x0000;
};
"""


def main():
    print("=" * 70)
    print("PeakRDL-pybind11 Memory Type Demonstration")
    print("=" * 70)
    print()
    
    # Create temporary directory for output
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write RDL to temporary file
        rdl_path = os.path.join(tmpdir, "demo.rdl")
        with open(rdl_path, 'w') as f:
            f.write(DEMO_RDL)
        
        print(f"1. Compiling SystemRDL...")
        rdl = RDLCompiler()
        rdl.compile_file(rdl_path)
        root = rdl.elaborate()
        print("   ✓ Compilation successful")
        print()
        
        # Export to PyBind11
        output_dir = os.path.join(tmpdir, "output")
        os.makedirs(output_dir)
        
        print(f"2. Exporting to PyBind11 C++...")
        exporter = Pybind11Exporter()
        exporter.export(root.top, output_dir, soc_name="demo_soc")
        print("   ✓ Export successful")
        print()
        
        # Show generated files
        print("3. Generated files:")
        files = sorted(os.listdir(output_dir))
        for filename in files:
            filepath = os.path.join(output_dir, filename)
            size = os.path.getsize(filepath)
            print(f"   - {filename:40s} ({size:6d} bytes)")
        print()
        
        # Analyze memory properties
        print("4. Memory properties:")
        nodes = exporter._collect_nodes(root.top)
        for mem in nodes['mems']:
            print(f"   Name: {mem.inst_name}")
            print(f"   Entries: {mem.get_property('mementries')}")
            print(f"   Entry width: {mem.get_property('memwidth')} bits")
            print(f"   Base address: 0x{mem.absolute_address:08x}")
            print(f"   Total size: {mem.total_size} bytes")
            entry_reg = list(mem.children())[0]
            print(f"   Entry register: {entry_reg.inst_name} ({entry_reg.size} bytes)")
        print()
        
        # Show generated C++ interface
        print("5. Generated C++ memory class interface:")
        with open(os.path.join(output_dir, "demo_soc_descriptors.hpp"), 'r') as f:
            content = f.read()
            # Find and display the MemoryBase class
            if 'class MemoryBase' in content:
                print("   ✓ MemoryBase template class generated")
            if 'operator[]' in content:
                print("   ✓ Array access operator (operator[]) generated")
            if 'size()' in content:
                print("   ✓ Size method generated")
            if 'begin()' in content and 'end()' in content:
                print("   ✓ Iterator support (begin/end) generated")
        print()
        
        # Show Python bindings
        print("6. Python list-like interface bindings:")
        with open(os.path.join(output_dir, "demo_soc_bindings.cpp"), 'r') as f:
            content = f.read()
            if '__len__' in content:
                print("   ✓ __len__: Get number of entries")
            if '__getitem__' in content:
                print("   ✓ __getitem__: Access by index")
            if 'py::slice' in content:
                print("   ✓ __getitem__ with slicing: Access by slice")
            if '__iter__' in content:
                print("   ✓ __iter__: Iteration support")
        print()
        
        # Show Python type hints
        print("7. Python type hints (.pyi stubs):")
        with open(os.path.join(output_dir, "__init__.pyi"), 'r') as f:
            content = f.read()
            # Find memory class stub
            if 'class status_mem_t' in content:
                print("   ✓ Memory class stub generated")
                # Extract and show the stub
                lines = content.split('\n')
                in_mem_class = False
                indent_count = 0
                for line in lines:
                    if 'class status_mem_t' in line:
                        in_mem_class = True
                        indent_count = 0
                    elif in_mem_class:
                        if line.strip() and not line.startswith(' ' * 4):
                            # End of class
                            break
                        if line.strip().startswith('def '):
                            method = line.strip().split('(')[0].replace('def ', '')
                            print(f"      - {method}()")
        print()
        
        # Example usage in Python (pseudo-code since we can't build without pybind11)
        print("8. Example Python usage (pseudo-code):")
        print("   ```python")
        print("   import demo_soc")
        print("   from peakrdl_pybind11.masters import MockMaster")
        print()
        print("   # Create SoC and attach master")
        print("   soc = demo_soc.create()")
        print("   soc.attach_master(MockMaster())")
        print()
        print("   # Access memory like a list")
        print("   print(f'Memory has {len(soc.status_mem)} entries')")
        print()
        print("   # Single element access")
        print("   entry_0 = soc.status_mem[0]")
        print("   entry_0.data.write(0x12345678)")
        print("   value = entry_0.data.read()")
        print()
        print("   # Slice access")
        print("   first_10 = soc.status_mem[0:10]")
        print("   even_entries = soc.status_mem[::2]")
        print()
        print("   # Iteration")
        print("   for entry in soc.status_mem:")
        print("       entry.valid.write(1)")
        print()
        print("   # List comprehension")
        print("   valid_entries = [e for e in soc.status_mem if e.valid.read()]")
        print("   ```")
        print()
        
        print("=" * 70)
        print("Demonstration complete!")
        print("=" * 70)


if __name__ == "__main__":
    main()
