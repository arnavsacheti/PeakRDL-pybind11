Usage Guide
===========

Command Line Interface
----------------------

Basic Usage
~~~~~~~~~~~

The basic command to export SystemRDL to PyBind11:

.. code-block:: bash

   peakrdl pybind11 input.rdl -o output_dir --soc-name MySoC --top top_addrmap --gen-pyi

CLI Options
~~~~~~~~~~~

- ``--soc-name``: Name of the generated SoC module (default: derived from input file)
- ``--top``: Top-level address map node to export (default: top-level node)
- ``--gen-pyi``: Generate ``.pyi`` stub files for type hints (enabled by default)
- ``--split-bindings COUNT``: Split bindings into multiple files for parallel compilation when register count exceeds COUNT (default: 100, set to 0 to disable)
- ``--split-by-hierarchy``: Split bindings by addrmap/regfile hierarchy instead of by register count

Compilation Performance Optimization
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For large register maps, compilation can be very slow. PeakRDL-pybind11 includes several optimizations:

1. **Hierarchical binding splitting** (recommended): Use ``--split-by-hierarchy`` to split bindings by addrmap/regfile boundaries
2. **Register count binding splitting**: When register count exceeds ``--split-bindings`` threshold, bindings are split into multiple ``.cpp`` files
3. **Optimized compiler flags**: The generated CMakeLists.txt uses ``-O1`` optimization for faster compilation
4. **Parallel compilation**: CMake will automatically compile split files in parallel

Examples for large register maps:

.. code-block:: bash

   # Split by hierarchy (recommended for well-structured designs)
   peakrdl pybind11 large_design.rdl -o output --split-by-hierarchy

   # Split bindings every 50 registers
   peakrdl pybind11 large_design.rdl -o output --split-bindings 50

   # Build with parallel compilation (4 cores)
   cd output
   pip install . -- -DCMAKE_BUILD_PARALLEL_LEVEL=4

Python API
----------

Basic Usage
~~~~~~~~~~~

.. code-block:: python

   from peakrdl_pybind11 import Pybind11Exporter
   from systemrdl import RDLCompiler

   # Compile SystemRDL
   rdl = RDLCompiler()
   rdl.compile_file("input.rdl")
   root = rdl.elaborate()

   # Export to PyBind11
   exporter = Pybind11Exporter()
   exporter.export(root, "output_dir", soc_name="MySoC")

Advanced Options
~~~~~~~~~~~~~~~~

.. code-block:: python

   # For large designs, enable binding splitting by hierarchy
   exporter.export(root, "output_dir", soc_name="MySoC", split_by_hierarchy=True)

   # Or split by register count
   exporter.export(root, "output_dir", soc_name="MySoC", split_bindings=50)

Using Generated Modules
------------------------

Basic Operations
~~~~~~~~~~~~~~~~

.. code-block:: python

   import MySoC
   from peakrdl_pybind11.masters import MockMaster

   # Create and attach a master
   soc = MySoC.create()
   master = MockMaster()
   soc.attach_master(master)

   # Read/write registers
   value = soc.peripherals.uart.control.read()
   soc.peripherals.uart.control.write(0x1234)

   # Modify specific fields
   soc.peripherals.uart.control.modify(enable=1, mode=2)

Available Master Backends
~~~~~~~~~~~~~~~~~~~~~~~~~~

Mock Master
^^^^^^^^^^^

For testing without hardware:

.. code-block:: python

   from peakrdl_pybind11.masters import MockMaster

   master = MockMaster()
   soc.attach_master(master)

OpenOCD Master
^^^^^^^^^^^^^^

For JTAG/SWD debugging:

.. code-block:: python

   from peakrdl_pybind11.masters import OpenOCDMaster

   master = OpenOCDMaster(host="localhost", port=4444)
   soc.attach_master(master)

SSH Master
^^^^^^^^^^

For remote access:

.. code-block:: python

   from peakrdl_pybind11.masters import SSHMaster

   master = SSHMaster(host="192.168.1.100", username="user", password="pass")
   soc.attach_master(master)

Custom Master Backend
~~~~~~~~~~~~~~~~~~~~~

You can implement custom master backends by inheriting from the base Master class:

.. code-block:: python

   from peakrdl_pybind11.masters import Master

   class MyCustomMaster(Master):
       def read(self, address, width):
           # Implement your read logic
           pass

       def write(self, address, width, value):
           # Implement your write logic
           pass
