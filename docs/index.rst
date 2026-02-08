PeakRDL-pybind11 Documentation
==============================

.. image:: https://img.shields.io/badge/license-GPL--3.0-blue
   :target: https://github.com/arnavsacheti/PeakRDL-pybind11/blob/main/LICENSE
   :alt: License

PeakRDL-pybind11 is an exporter for the PeakRDL toolchain that generates PyBind11 modules from SystemRDL register descriptions. This enables Python-based hardware testing and interaction with register maps through a clean, type-safe Python API.

Features
--------

- **PyBind11 Module Generation**: Automatically generates C++ descriptors and Python bindings from SystemRDL
- **SoC Hierarchy Exposure**: Import generated modules to access the complete SoC register hierarchy
- **Pluggable Master Backends**: Support for multiple communication backends:

  - Mock Master (for testing without hardware)
  - OpenOCD Master (for JTAG/SWD debugging)
  - SSH Master (for remote access)
  - Custom Master backends (extensible interface)

- **Comprehensive API**:

  - ``attach_master()``: Connect to hardware or simulator
  - ``read()``: Read register values
  - ``write()``: Write register values
  - ``modify()``: Read-modify-write operations

- **Type Safety**: Generated ``.pyi`` stub files for full IDE support and type checking
- **Python-Based Testing**: Enable hardware testing with callbacks and custom logic

Installation
------------

.. code-block:: bash

   pip install peakrdl-pybind11

Quick Start
-----------

Command Line Usage
~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   peakrdl pybind11 input.rdl -o output_dir --soc-name MySoC --top top_addrmap --gen-pyi

Python API Usage
~~~~~~~~~~~~~~~~

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

Using Generated Modules
~~~~~~~~~~~~~~~~~~~~~~~~

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

Contents
--------

.. toctree::
   :maxdepth: 2
   :caption: User Guide

   installation
   usage
   feature_matrix

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api
   api/exporter
   api/masters

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
