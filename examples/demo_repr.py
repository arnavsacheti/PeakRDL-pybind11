#!/usr/bin/env python3
"""
Demonstration of __repr__ and __str__ functionality in PeakRDL-pybind11

This script shows what the generated __repr__ methods look like for various classes.
Note: This is a demonstration of the generated C++ code structure, not a working runtime example.
"""

import os
import tempfile

from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

# Sample RDL
RDL_CONTENT = """
addrmap demo_soc {
    name = "Demo SoC";
    desc = "Demonstration SoC with various register types";
    
    reg control_reg {
        name = "Control Register";
        desc = "Main control register";
        
        field {
            name = "Enable";
            desc = "Global enable bit";
            sw = rw;
            hw = r;
        } enable[0:0];
        
        field {
            name = "Mode";
            desc = "Operating mode selection";
            sw = rw;
            hw = r;
        } mode[3:1];
        
        field {
            name = "Priority";
            desc = "Priority level";
            sw = rw;
            hw = r;
        } priority[7:4];
    } control @ 0x0000;
    
    reg status_reg {
        name = "Status Register";
        desc = "Status and flags";
        
        field {
            name = "Ready";
            desc = "Device ready flag";
            sw = r;
            hw = w;
        } ready[0:0];
        
        field {
            name = "Error";
            desc = "Error flag";
            sw = r;
            hw = w;
        } error[1:1];
        
        field {
            name = "Count";
            desc = "Event counter";
            sw = r;
            hw = w;
        } count[15:8];
    } status @ 0x0004;
    
    regfile uart {
        name = "UART Peripheral";
        desc = "UART register file";
        
        reg {
            name = "Data Register";
            field {
                name = "Data";
                sw = rw;
                hw = rw;
            } data[7:0];
        } data @ 0x00;
        
        reg {
            name = "Baud Rate";
            field {
                name = "Divisor";
                sw = rw;
                hw = r;
            } divisor[15:0];
        } baud @ 0x04;
    } uart @ 0x1000;
    
    external mem {
        name = "Data Buffer";
        desc = "Data buffer memory";
        mementries = 32;
        memwidth = 32;
        
        reg {
            field {
                name = "Value";
                sw = rw;
                hw = rw;
            } value[31:0];
        } entry;
    } buffer @ 0x2000;
};
"""

def main() -> None:
    print("=" * 80)
    print("PeakRDL-pybind11 __repr__ and __str__ Demonstration")
    print("=" * 80)
    print()
    
    # Compile RDL
    rdl = RDLCompiler()
    rdl_file = _write_rdl(RDL_CONTENT)
    
    try:
        rdl.compile_file(rdl_file)
        root = rdl.elaborate()
        
        # Export to temporary directory
        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = Pybind11Exporter()
            exporter.export(root.top, tmpdir, soc_name="demo_soc")
            
            print("Generated files in:", tmpdir)
            print()
            
            # Show the __repr__ implementations
            with open(os.path.join(tmpdir, 'demo_soc_descriptors.hpp')) as f:
                content = f.read()
            
            print("__repr__ Methods in Generated C++ Code")
            print("-" * 80)
            print()
            
            # Extract and display each __repr__ method
            classes = [
                ("Master", "Abstract bus master interface"),
                ("FieldBase", "Register field base class"),
                ("RegisterBase", "Register base class"),
                ("NodeBase", "Addrmap/Regfile base class"),
                ("MemoryBase", "Memory array base class")
            ]
            
            lines = content.split('\n')
            for class_name, description in classes:
                print(f"{class_name} - {description}")
                print("─" * 80)
                
                # Find the __repr__ method for this class
                for i, line in enumerate(lines):
                    if f'class {class_name}' in line or f'class {class_name}<' in line:
                        # Look for __repr__ in the next 100 lines
                        for j in range(i, min(i + 100, len(lines))):
                            if '__repr__() const' in lines[j]:
                                # Print the __repr__ method
                                k = j
                                while k < len(lines) and 'return' not in lines[k]:
                                    print(f"  {lines[k]}")
                                    k += 1
                                # Print the return line and closing brace
                                if k < len(lines):
                                    print(f"  {lines[k]}")
                                    k += 1
                                if k < len(lines) and '}' in lines[k]:
                                    print(f"  {lines[k]}")
                                break
                        break
                print()
            
            print()
            print("Example Output (what Python users would see)")
            print("─" * 80)
            print()
            
            # Show example outputs
            examples = [
                ("Field 'enable'", "<Field 'enable' @ 0x0 [0:0] RW>"),
                ("Field 'mode'", "<Field 'mode' @ 0x0 [3:1] RW>"),
                ("Field 'priority'", "<Field 'priority' @ 0x0 [7:4] RW>"),
                ("Field 'ready' (read-only)", "<Field 'ready' @ 0x4 [0:0] R>"),
                ("Field 'count' (read-only)", "<Field 'count' @ 0x4 [15:8] R>"),
                ("", ""),
                ("Register 'control'", "<Register 'control' @ 0x0000 (32 bits)>"),
                ("Register 'status'", "<Register 'status' @ 0x0004 (32 bits)>"),
                ("", ""),
                ("Node 'uart' (regfile)", "<Node 'uart' @ 0x1000>"),
                ("", ""),
                ("Memory 'buffer'", "<Memory 'buffer' @ 0x2000 [32 entries]>"),
                ("", ""),
                ("Master interface", "<Master>"),
            ]
            
            for label, output in examples:
                if label:
                    print(f"{label:.<40} {output}")
                else:
                    print()
            
            print()
            print("=" * 80)
            print("Usage in Python")
            print("=" * 80)
            print()
            print("After building and importing the generated module:")
            print()
            print("  import demo_soc")
            print("  from peakrdl_pybind11.masters import MockMaster")
            print()
            print("  soc = demo_soc.create()")
            print("  master = MockMaster()")
            print("  soc.attach_master(master)")
            print()
            print("  # Print register representation")
            print("  print(repr(soc.control))        # <Register 'control' @ 0x0000 (32 bits)>")
            print("  print(str(soc.status))          # <Register 'status' @ 0x0004 (32 bits)>")
            print()
            print("  # Print field representation")
            print("  print(repr(soc.control.enable)) # <Field 'enable' @ 0x0 [0:0] RW>")
            print("  print(repr(soc.status.ready))   # <Field 'ready' @ 0x4 [0:0] R>")
            print()
            print("  # Print node representation")
            print("  print(repr(soc.uart))           # <Node 'uart' @ 0x1000>")
            print()
            print("  # Print memory representation")
            print("  print(repr(soc.buffer))         # <Memory 'buffer' @ 0x2000 [32 entries]>")
            print()
            print("=" * 80)
            
    finally:
        if os.path.exists(rdl_file):
            os.unlink(rdl_file)


def _write_rdl(content: str) -> str:
    """Write RDL content to a temporary file"""
    import tempfile
    fd, path = tempfile.mkstemp(suffix='.rdl')
    os.write(fd, content.encode('utf-8'))
    os.close(fd)
    return path


if __name__ == "__main__":
    main()
