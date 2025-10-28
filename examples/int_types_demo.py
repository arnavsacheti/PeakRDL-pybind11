#!/usr/bin/env python3
"""
Example demonstrating RegisterInt and FieldInt usage.

This script shows how to use the new RegisterInt and FieldInt classes
for enhanced register manipulation with metadata preservation.
"""

from peakrdl_pybind11 import FieldInt, RegisterInt


def main() -> None:
    print("=" * 70)
    print("RegisterInt and FieldInt Example")
    print("=" * 70)
    print()

    # Example 1: Creating a FieldInt
    print("1. Creating a FieldInt")
    print("-" * 70)
    field = FieldInt(0x5, lsb=2, width=3, offset=0x1000)
    print("   field = FieldInt(0x5, lsb=2, width=3, offset=0x1000)")
    print(f"   Value: {int(field)}")
    print(f"   LSB: {field.lsb}")
    print(f"   MSB: {field.msb}")
    print(f"   Width: {field.width} bits")
    print(f"   Offset: {field.offset:#x}")
    print(f"   Mask: {field.mask:#010b} ({field.mask:#x})")
    print()

    # Example 2: Creating a RegisterInt with fields
    print("2. Creating a RegisterInt with fields")
    print("-" * 70)
    reg = RegisterInt(
        0xABCD,
        offset=0x2000,
        width=4,
        fields={
            "enable": (0, 1),
            "mode": (1, 3),
            "status": (4, 4),
            "data": (8, 8),
        },
    )
    print("   reg = RegisterInt(0xABCD, offset=0x2000, width=4, fields={...})")
    print(f"   Value: {int(reg):#06x}")
    print(f"   Offset: {reg.offset:#x}")
    print(f"   Width: {reg.width} bytes")
    print()
    print("   Accessing fields:")
    print(f"     reg.enable = {int(reg.enable)} (bit 0)")
    print(f"     reg.mode   = {int(reg.mode)} (bits 3:1)")
    print(f"     reg.status = {int(reg.status):#x} (bits 7:4)")
    print(f"     reg.data   = {int(reg.data):#x} (bits 15:8)")
    print()

    # Example 3: Comparison operations
    print("3. Comparison operations")
    print("-" * 70)
    field1 = FieldInt(5, lsb=0, width=4, offset=0)
    field2 = FieldInt(3, lsb=0, width=4, offset=0)

    print("   field1 = FieldInt(5, ...)")
    print("   field2 = FieldInt(3, ...)")
    print(f"   field1 > field2: {field1 > field2}")
    print(f"   field1 == 5: {field1 == 5}")
    print(f"   field2 < 5: {field2 < 5}")
    print()

    # Example 4: Use case - Read-Modify-Write
    print("4. Use case: Read-Modify-Write simulation")
    print("-" * 70)
    print("   Imagine a control register with:")
    print("     - enable (bit 0)")
    print("     - mode (bits 3:1)")
    print("     - priority (bits 7:4)")
    print()

    # Initial register value
    initial_value = RegisterInt(
        0x5B,  # 0b0101_1011
        offset=0x3000,
        width=4,
        fields={
            "enable": (0, 1),
            "mode": (1, 3),
            "priority": (4, 4),
        },
    )

    print(f"   Initial register value: {int(initial_value):#04x}")
    print(f"     enable   = {int(initial_value.enable)} (bit 0)")
    print(f"     mode     = {int(initial_value.mode)} (bits 3:1)")
    print(f"     priority = {int(initial_value.priority)} (bits 7:4)")
    print()

    # Create a FieldInt to modify only the mode field
    new_mode = FieldInt(7, lsb=1, width=3, offset=0x3000)
    print("   Want to change mode to 7 (leaving other fields unchanged)")
    print("   new_mode = FieldInt(7, lsb=1, width=3, offset=0x3000)")
    print(f"   new_mode.mask = {new_mode.mask:#010b}")
    print()

    # Simulate RMW operation
    current_val = int(initial_value)
    new_val = (current_val & ~new_mode.mask) | ((int(new_mode) << new_mode.lsb) & new_mode.mask)

    result = RegisterInt(
        new_val,
        offset=0x3000,
        width=4,
        fields={
            "enable": (0, 1),
            "mode": (1, 3),
            "priority": (4, 4),
        },
    )

    print(f"   After RMW: {int(result):#04x}")
    print(f"     enable   = {int(result.enable)} (unchanged)")
    print(f"     mode     = {int(result.mode)} (changed to 7)")
    print(f"     priority = {int(result.priority)} (unchanged)")
    print()

    # Example 5: With generated code
    print("5. With generated SoC code")
    print("-" * 70)
    print("   When using PeakRDL-pybind11 generated modules:")
    print()
    print("   # Read returns RegisterInt")
    print("   reg_value = soc.control.read()")
    print("   # Can access fields directly")
    print("   enable_val = reg_value.enable  # Returns FieldInt")
    print()
    print("   # Read field returns FieldInt")
    print("   field_val = soc.control.enable.read()")
    print()
    print("   # Write with FieldInt does automatic RMW")
    print("   new_mode = FieldInt(7, lsb=1, width=3, offset=0x0)")
    print("   soc.control.write(new_mode)  # Only modifies mode field!")
    print()
    print("   # Write with RegisterInt or int works normally")
    print("   soc.control.write(0xAB)")
    print("   soc.control.write(RegisterInt(0xAB, ...))")
    print()

    print("=" * 70)
    print("Example complete!")
    print("=" * 70)


if __name__ == "__main__":
    main()
