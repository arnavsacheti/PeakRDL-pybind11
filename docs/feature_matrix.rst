SystemRDL Feature Matrix
========================

This page documents which SystemRDL constructs are supported by PeakRDL-pybind11
and how each is mapped to generated code. PeakRDL-pybind11 focuses on register map
structure and access for hardware testing; it does not implement the full SystemRDL
specification.

Structural Components
---------------------

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Construct
     - Status
     - Notes
   * - ``addrmap``
     - Supported
     - Top-level and nested address maps. Exposed as Python classes with child attributes.
   * - ``regfile``
     - Supported
     - Register file grouping. Exposed as Python classes with child register attributes.
   * - ``reg``
     - Supported
     - Individual registers with read/write/modify operations and context manager support.
   * - ``field``
     - Supported
     - Register fields with per-field read/write, bit position, and width metadata.
   * - ``mem`` (external memory)
     - Supported
     - External memory declarations. Exposed with a list-like Python interface (indexing, slicing, iteration).
   * - ``signal``
     - Not supported
     - Signal nodes are not collected or represented in the generated output.
   * - ``constraint``
     - Not supported
     - Constraint definitions are ignored during export.

Register & Field Properties
----------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Property
     - Status
     - Notes
   * - ``sw`` (software access)
     - Supported
     - Mapped to ``is_sw_readable`` / ``is_sw_writable`` flags on each field. Access control is informational (reported via the API) but not enforced on write operations at the register level.
   * - ``hw`` (hardware access)
     - Supported
     - Mapped to ``is_hw_readable`` / ``is_hw_writable`` flags on each field.
   * - ``name``
     - Supported
     - Used as the instance name for generated classes and attributes.
   * - ``desc``
     - Supported
     - Preserved as docstrings in the generated PyBind11 bindings (HTML-escaped).
   * - Field bit range (e.g. ``[7:0]``)
     - Supported
     - ``lsb``, ``width``, ``msb``, and ``mask`` are all exposed on field objects.
   * - ``reset`` / default value
     - Not supported
     - Reset values from the SystemRDL source are parsed by the compiler but not used in generated code. Registers are not initialized to their reset values.
   * - ``volatile``
     - Not supported
     - The volatile flag is not extracted or used.
   * - ``precedence``
     - Not supported
     - Not extracted.
   * - ``encode`` (field encoding)
     - Not supported
     - Field encoding enums defined in SystemRDL are not carried through to the generated bindings.
   * - ``onread`` / ``onwrite`` side effects
     - Not supported
     - Read/write side-effect actions (e.g. ``rclr``, ``wset``) are not modeled.
   * - ``swmod`` / ``swacc``
     - Not supported
     - Software modify/access notification properties are ignored.
   * - ``singlepulse``
     - Not supported
     - Not modeled in generated code.
   * - ``paritycheck``
     - Not supported
     - Not modeled in generated code.

Memory Properties
-----------------

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Property
     - Status
     - Notes
   * - ``mementries``
     - Supported
     - Used to size the generated memory array.
   * - ``memwidth``
     - Supported
     - Used to determine entry width.
   * - ``sw`` / ``hw`` access
     - Not supported
     - Memory-level access modes are not enforced.

Hierarchy & Addressing
----------------------

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Feature
     - Status
     - Notes
   * - Nested addrmaps
     - Supported
     - Arbitrary nesting depth is supported.
   * - Nested regfiles
     - Supported
     - Regfiles can contain registers and other regfiles.
   * - Register arrays
     - Not supported
     - Array instantiation syntax (e.g. ``reg my_reg[4]``) is not handled.
   * - Address stride / alignment
     - Not supported
     - Custom address strides are not applied; ``absolute_address`` from the compiler is used directly.
   * - ``bridge``
     - Not supported
     - Bridge address maps are not modeled.
   * - ``alias``
     - Not supported
     - Register aliases are not generated as separate Python objects.

User-Defined Properties (UDP)
-----------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Feature
     - Status
     - Notes
   * - ``is_flag`` (UDP)
     - Supported
     - Registers annotated with ``is_flag = true`` generate Python ``IntFlag`` classes with one member per field.
   * - ``is_enum`` (UDP)
     - Supported
     - Registers annotated with ``is_enum = true`` generate Python ``IntEnum`` classes with one member per field.
   * - Arbitrary UDPs
     - Not supported
     - Only ``is_flag`` and ``is_enum`` are recognized. Other user-defined properties are ignored.

Interrupts & Events
-------------------

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Feature
     - Status
     - Notes
   * - ``intr`` property
     - Not supported
     - Interrupt fields are treated as regular fields.
   * - ``enable`` / ``mask`` / ``haltmask`` / ``haltenable``
     - Not supported
     - Interrupt modifier properties are ignored.
   * - ``stickybit`` / ``sticky``
     - Not supported
     - Not modeled in generated code.

Signals & Ports
---------------

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Feature
     - Status
     - Notes
   * - ``signal``
     - Not supported
     - Signals are not collected or represented.
   * - Component port declarations
     - Not supported
     - Not applicable to the register-access use case.

Generated API Features
----------------------

These features are not part of the SystemRDL specification itself but are generated
capabilities of the PeakRDL-pybind11 output:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Feature
     - Description
   * - Register ``read()`` / ``write()`` / ``modify()``
     - Full register-level operations via an attached master backend.
   * - Field ``read()`` / ``write()``
     - Per-field read and read-modify-write operations.
   * - Context manager (``with reg:``)
     - Batch field writes into a single register write on context exit.
   * - ``RegisterInt`` / ``FieldInt``
     - Enhanced integer types that preserve register/field metadata through arithmetic.
   * - ``IntFlag`` / ``IntEnum`` generation
     - Python enum types for registers marked with ``is_flag`` or ``is_enum``.
   * - ``.pyi`` type stubs
     - Generated stub files for IDE autocompletion and type checking.
   * - Split bindings
     - Large designs can be split by register count or by hierarchy for parallel compilation.
   * - Master backends
     - MockMaster, OpenOCDMaster, SSHMaster, CallbackMaster, and custom implementations.

Limitations
-----------

- **Register width**: Generated C++ code uses ``uint64_t``, limiting registers to 64 bits.
- **No reset initialization**: Registers are not initialized to their SystemRDL default/reset values.
- **No behavioral modeling**: Side effects (``onread``, ``onwrite``), counters, and interrupt logic are not modeled. The generated code provides a structural register map, not a behavioral simulation.
- **No signal/port support**: Only structural components used for register access (addrmaps, regfiles, registers, fields, memories) are exported.
