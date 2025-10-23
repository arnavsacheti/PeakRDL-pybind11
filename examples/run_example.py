#!/usr/bin/env python3
"""
Example usage of PeakRDL-pybind11 exporter

This script demonstrates how to:
1. Export a SystemRDL file to PyBind11 modules
2. Build the generated C++ extension
3. Use the generated Python API with different Master backends
"""

import os
import sys
import subprocess
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from systemrdl import RDLCompiler
from peakrdl_pybind11 import Pybind11Exporter


def export_example():
    """Export the example SystemRDL file"""
    print("=" * 70)
    print("Step 1: Exporting SystemRDL to PyBind11 modules")
    print("=" * 70)

    # Compile SystemRDL
    rdl_file = Path(__file__).parent / "example.rdl"
    output_dir = Path(__file__).parent / "output"

    print(f"Input file: {rdl_file}")
    print(f"Output directory: {output_dir}")

    rdl = RDLCompiler()
    rdl.compile_file(str(rdl_file))
    root = rdl.elaborate()

    # Export to PyBind11
    exporter = Pybind11Exporter()
    exporter.export(root.top, str(output_dir), soc_name="example_soc", gen_pyi=True)

    print("\nGenerated files:")
    for f in output_dir.iterdir():
        if f.is_file():
            print(f"  - {f.name} ({f.stat().st_size} bytes)")

    print("\n✓ Export completed successfully!")
    return output_dir


def build_extension(output_dir):
    """Build the C++ extension"""
    print("\n" + "=" * 70)
    print("Step 2: Building C++ extension (optional)")
    print("=" * 70)
    print("\nTo build the extension, run:")
    print(f"  cd {output_dir}")
    print("  pip install .")
    print("\nOr for development:")
    print("  pip install -e .")
    print("\nNote: This requires pybind11, CMake 3.15+, and a C++11 compiler.")


def demonstrate_usage():
    """Show example usage"""
    print("\n" + "=" * 70)
    print("Step 3: Example Python usage")
    print("=" * 70)

    example_code = """
# Import the generated module
import sys
sys.path.insert(0, 'examples/output')
import example_soc
from peakrdl_pybind11.masters import MockMaster

# Create SoC instance
soc = example_soc.create()

# Attach a mock master (for testing without hardware)
master = MockMaster()
soc.attach_master(master)

# Configure UART
soc.uart.control.write(0b00_010_1)  # Enable, 115200 baud, no parity

# Or configure individual fields:
# soc.uart.control.enable.write(1)
# soc.uart.control.baudrate.write(2)  # 115200
# soc.uart.control.parity.write(0)    # No parity

# Read status
status = soc.uart.status.read()
print(f"UART status: 0x{status:02x}")

# Check individual status bits
if soc.uart.status.tx_ready.read():
    print("TX buffer ready")

# Write data
soc.uart.data.write(0x42)

# Configure GPIO
soc.gpio.direction.write(0xFF00)  # Upper 8 pins as outputs
soc.gpio.output.write(0x5500)     # Set output values

# Read GPIO input
input_val = soc.gpio.input.read()
print(f"GPIO input: 0x{input_val:04x}")
"""

    print("\nExample code:")
    print(example_code)


def main():
    """Main entry point"""
    print("\n" + "=" * 70)
    print("PeakRDL-pybind11 Example")
    print("=" * 70)

    try:
        # Export
        output_dir = export_example()

        # Show build instructions
        build_extension(output_dir)

        # Show usage example
        demonstrate_usage()

        print("\n" + "=" * 70)
        print("Next steps:")
        print("=" * 70)
        print("1. Install dependencies: pip install pybind11")
        print("2. Build the extension (see Step 2 above)")
        print("3. Try the example code (see Step 3 above)")
        print("\nFor more information, see the README.md file.")
        print("=" * 70)

    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
