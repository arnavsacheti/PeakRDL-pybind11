Integer Types
=============

PeakRDL-pybind11 provides enhanced integer types that preserve metadata about register fields and positions.

Overview
--------

When reading from generated register maps, ``read()`` methods return ``RegisterInt`` or ``FieldInt`` objects
instead of plain Python integers. These enhanced types:

* Inherit from Python's ``int``, so they work anywhere an integer does
* Preserve position and width metadata
* Support all integer operations (comparison, arithmetic, etc.)
* Enable smart read-modify-write operations

RegisterInt
-----------

.. autoclass:: peakrdl_pybind11.RegisterInt
   :members:
   :inherited-members:
   :special-members: __new__

A ``RegisterInt`` represents a complete register value with metadata about its fields.

**Example:**

.. code-block:: python

   from peakrdl_pybind11 import RegisterInt

   # Create a RegisterInt with fields
   reg = RegisterInt(
       0xABCD,
       offset=0x1000,
       width=4,
       fields={
           'enable': (0, 1),      # bit 0, width 1
           'mode': (1, 3),        # bits 3:1, width 3
           'data': (8, 8),        # bits 15:8, width 8
       }
   )

   # Use as a regular integer
   print(f"Register value: {int(reg):#x}")  # 0xabcd

   # Access metadata
   print(f"Offset: {reg.offset:#x}")  # 0x1000
   print(f"Width: {reg.width} bytes")  # 4

   # Access fields (returns FieldInt objects)
   print(f"Enable: {reg.enable}")      # 1
   print(f"Mode: {reg.mode}")          # 6
   print(f"Data: {reg.data:#x}")       # 0xab

**With Generated Code:**

.. code-block:: python

   # Read returns RegisterInt
   reg_value = soc.control.read()
   
   # Access fields directly from the returned value
   if reg_value.enable == 1:
       print(f"Mode is {reg_value.mode}")


FieldInt
--------

.. autoclass:: peakrdl_pybind11.FieldInt
   :members:
   :inherited-members:
   :special-members: __new__

A ``FieldInt`` represents a field value with metadata about its position within a register.

**Example:**

.. code-block:: python

   from peakrdl_pybind11 import FieldInt

   # Create a FieldInt for a 3-bit field at bits 3:1
   field = FieldInt(5, lsb=1, width=3, offset=0x1000)

   # Use as a regular integer
   print(f"Field value: {int(field)}")  # 5

   # Access metadata
   print(f"LSB: {field.lsb}")          # 1
   print(f"MSB: {field.msb}")          # 3
   print(f"Width: {field.width} bits") # 3
   print(f"Mask: {field.mask:#x}")     # 0xe

**With Generated Code:**

.. code-block:: python

   # Read field returns FieldInt
   enable_val = soc.control.enable.read()
   
   # Check field properties
   print(f"Enable at bit {enable_val.lsb}")


Read-Modify-Write Operations
----------------------------

One of the key features of ``FieldInt`` is automatic read-modify-write when passed to ``write()``:

.. code-block:: python

   # Set initial register value
   soc.control.write(0x5B)  # enable=1, mode=5, priority=5

   # Create a FieldInt to change only the mode field
   new_mode = FieldInt(7, lsb=1, width=3, offset=0x0)

   # Write the FieldInt - automatically does RMW!
   # Only bits 3:1 are modified, enable and priority unchanged
   soc.control.write(new_mode)

   # Result: enable=1, mode=7, priority=5 (0x5F)
   result = soc.control.read()
   assert result.enable == 1      # unchanged
   assert result.mode == 7        # changed
   assert result.priority == 5    # unchanged

This is much safer than manually doing read-modify-write:

.. code-block:: python

   # Manual RMW (error-prone):
   current = soc.control.read()
   new_value = (current & ~0xE) | ((7 << 1) & 0xE)
   soc.control.write(new_value)

   # With FieldInt (safe and clear):
   new_mode = FieldInt(7, lsb=1, width=3, offset=0x0)
   soc.control.write(new_mode)


Type Compatibility
------------------

Both ``RegisterInt`` and ``FieldInt`` are fully compatible with Python's ``int`` type:

.. code-block:: python

   field = FieldInt(5, lsb=0, width=4, offset=0)

   # Comparison
   assert field == 5
   assert field > 3
   assert field < 10

   # Arithmetic (returns plain int)
   result = field + 2      # 7 (plain int)
   result = field * 2      # 10 (plain int)

   # Use anywhere an int is expected
   value = int(field)      # 5
   hex_str = f"{field:#x}" # "0x5"


RegisterIntFlag
---------------

.. autoclass:: peakrdl_pybind11.RegisterIntFlag
   :members:
   :show-inheritance:

When a register in SystemRDL has the ``flag`` UDP property set to true, PeakRDL-pybind11 generates
a Python ``IntFlag`` class where each field becomes a flag member. This is ideal for status and
control registers where each bit or field has independent meaning.

**Example SystemRDL:**

.. code-block:: systemrdl

   property flag {
       component = reg;
       type = boolean;
   };

   addrmap example {
       reg {
           name = "Status Register";
           flag = true;
           
           field { sw = r; hw = w; } ready[0:0];
           field { sw = r; hw = w; } error[1:1];
           field { sw = r; hw = w; } busy[2:2];
       } status @ 0x00;
   };

**Generated Python Usage:**

.. code-block:: python

   import example
   from peakrdl_pybind11.masters import MockMaster

   # Setup
   soc = example.create()
   master = example.wrap_master(MockMaster())
   soc.attach_master(master)

   # Access the generated IntFlag class
   Flags = example.status_Flags
   
   # Flag members are named in UPPERCASE based on field names
   ready_flag = Flags.READY    # Value: 1
   error_flag = Flags.ERROR    # Value: 2
   busy_flag = Flags.BUSY      # Value: 4

   # Combine flags with bitwise operations
   ready_and_error = Flags.READY | Flags.ERROR  # Value: 3
   
   # Check membership
   status = soc.status.read()  # Returns an IntFlag value
   if Flags.READY in status:
       print("System is ready")
   if Flags.ERROR in status:
       print("Error occurred")

   # Write flags
   soc.status.write(Flags.READY | Flags.BUSY)

**Note:** When the ``flag`` UDP property is set, fields are NOT accessible as attributes on the 
register. Instead, use the generated IntFlag class and its members.


RegisterIntEnum
---------------

.. autoclass:: peakrdl_pybind11.RegisterIntEnum
   :members:
   :show-inheritance:

When a register in SystemRDL has the ``is_enum`` UDP property set to true (note: ``enum`` is a
reserved keyword, so use ``is_enum``), PeakRDL-pybind11 generates a Python ``IntEnum`` class where 
each field becomes an enum member. This is ideal for mode or state registers where values represent 
discrete states.

**Example SystemRDL:**

.. code-block:: systemrdl

   property is_enum {
       component = reg;
       type = boolean;
   };

   addrmap example {
       reg {
           name = "Mode Register";
           is_enum = true;
           
           field { sw = rw; hw = r; } idle[0:0];
           field { sw = rw; hw = r; } running[1:1];
           field { sw = rw; hw = r; } paused[2:2];
       } mode @ 0x04;
   };

**Generated Python Usage:**

.. code-block:: python

   import example
   from peakrdl_pybind11.masters import MockMaster

   # Setup
   soc = example.create()
   master = example.wrap_master(MockMaster())
   soc.attach_master(master)

   # Access the generated IntEnum class
   Mode = example.mode_Enum
   
   # Enum members are named in UPPERCASE based on field names
   idle_mode = Mode.IDLE       # Value: 1
   running_mode = Mode.RUNNING # Value: 2
   paused_mode = Mode.PAUSED   # Value: 4

   # Compare states
   current_mode = soc.mode.read()  # Returns an IntEnum value
   if current_mode == Mode.RUNNING:
       print("System is running")

   # Write states
   soc.mode.write(Mode.IDLE)

**Note:** When the ``is_enum`` UDP property is set, fields are NOT accessible as attributes on the
register. Instead, use the generated IntEnum class and its members.


Complete Example
---------------

.. code-block:: python

   import simple_soc
   from peakrdl_pybind11.masters import MockMaster
   from peakrdl_pybind11 import RegisterInt, FieldInt

   # Setup
   soc = simple_soc.create()
   master = simple_soc.wrap_master(MockMaster())
   soc.attach_master(master)

   # Read register (returns RegisterInt)
   control = soc.control.read()
   print(f"Control: {control:#x}")
   print(f"  Enable: {control.enable}")
   print(f"  Mode: {control.mode}")

   # Read field (returns FieldInt)
   enable = soc.control.enable.read()
   print(f"Enable bit at position {enable.lsb}")

   # Write with automatic RMW
   new_mode = FieldInt(7, lsb=1, width=3, offset=0x0)
   soc.control.write(new_mode)  # Only changes mode field

   # Write full register
   soc.control.write(0xAB)
   soc.control.write(RegisterInt(0xCD, offset=0, width=4))
