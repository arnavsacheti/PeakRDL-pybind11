Hierarchy and Discovery
=======================

.. note::

   This page is **aspirational**. It describes the API surface defined by the
   ideal-API sketch (sections §2 and §4). Some of the surfaces below are not
   yet implemented in the shipped exporter — the sketch is the source of truth,
   and the code is catching up.

A generated SoC module is, first and last, a tree you can navigate. This page
describes the mental model behind that tree, the ways to walk it, and the
metadata each node exposes for inspection from a REPL or notebook.

Mental model
------------

A generated module is a **typed tree of nodes** that mirrors the SystemRDL
hierarchy. Every node knows:

- Its **path** and **absolute address**.
- Its **kind**: ``AddrMap``, ``RegFile``, ``Reg``, ``Field``, ``Mem``,
  ``InterruptGroup``, ``Alias``, ``Signal``.
- Its **metadata**: name, description, RDL properties, source location.
- Its **bus binding**: which master serves which address ranges.

Nodes are *descriptors*, not values. They produce values by reading. Values
produced by reads are typed wrappers (``RegisterValue``, ``FieldValue``) that
behave like ints but carry decode info — they print well, compare to enums,
and round-trip cleanly back into writes.

.. code-block:: text

   SoC
   ├── AddrMap            (peripherals)
   │   ├── RegFile        (uart[0..3])
   │   │   ├── Reg        (control)
   │   │   │   ├── Field  (enable, baudrate, parity)
   │   │   │   └── ...
   │   │   ├── Reg        (status)            ← side-effect: rclr
   │   │   └── InterruptGroup    (auto-detected from intr_state/enable/test trio)
   │   └── Mem            (sram)              ← buffer-protocol, ndarray, slice
   └── Master             (the bus)

Navigation
----------

The canonical form is attribute access along the RDL hierarchy. Several
escape hatches exist for indexed lookup, programmatic search, and address-based
discovery:

.. code-block:: python

   soc.peripherals.uart[0].control            # by index
   soc.peripherals.uart["uart0"]              # by name
   soc.peripherals.uart.uart0                 # if names are valid identifiers
   soc["peripherals.uart[0].control"]         # path string (escape hatch)

   soc.find(0x4000_1004)                      # → soc.peripherals.uart[0].status
   soc.find_by_name("control", glob=True)     # → list of all matches

   list(soc.walk())                           # breadth-first iterator over leaves
   list(soc.walk(kind=Reg))                   # filtered

``walk()`` yields every leaf in the subtree rooted at the receiver. Pass
``kind=`` to filter by node kind (e.g. ``Reg``, ``Field``, ``Mem``).

Metadata via ``.info``
----------------------

Every node exposes a uniform ``.info`` namespace so attribute autocompletion
isn't polluted by metadata accessors. The same shape works for any node kind:

.. code-block:: python

   soc.uart.control.info.address           # 0x4000_1000
   soc.uart.control.info.path              # "peripherals.uart[0].control"
   soc.uart.control.info.fields            # OrderedDict[str, FieldInfo]

The full attribute set:

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Attribute
     - Applies to
     - Description
   * - ``name``
     - all nodes
     - Human-readable name (from RDL ``name`` property), e.g. ``"Control Register"``.
   * - ``desc``
     - all nodes
     - Long-form description (from RDL ``desc`` property).
   * - ``address``
     - registers, regfiles, memories
     - Absolute byte address on the bus, e.g. ``0x4000_1000``.
   * - ``offset``
     - registers, fields
     - Offset within the parent container, e.g. ``0x000``.
   * - ``regwidth``
     - registers
     - Register width in bits, e.g. ``32``.
   * - ``access``
     - registers, fields
     - Access mode (``AccessMode.RW``, ``RO``, ``WO``, ``NA``).
   * - ``reset``
     - registers, fields
     - Reset value.
   * - ``fields``
     - registers
     - ``OrderedDict[str, FieldInfo]`` of child fields.
   * - ``path``
     - all nodes
     - Dotted/indexed RDL path, e.g. ``"peripherals.uart[0].control"``.
   * - ``rdl_node``
     - all nodes
     - Underlying ``systemrdl`` AST node (``None`` if stripped).
   * - ``source``
     - all nodes
     - ``(filename, line)`` tuple pointing at the RDL source.
   * - ``tags``
     - all nodes
     - Custom user-defined properties (UDPs).

Field-specific extras live alongside the common attributes:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Attribute
     - Description
   * - ``precedence``
     - ``Precedence.SW`` or ``Precedence.HW`` — who wins on collision.
   * - ``paritycheck``
     - ``bool`` — RDL parity protection enabled.
   * - ``is_volatile``
     - ``True`` if ``hwclr``/``hwset``/``sticky``/``counter`` — value can change without software.
   * - ``is_interrupt_source``
     - ``True`` if the field carries the ``intr`` property.

Example field probe:

.. code-block:: python

   f = soc.uart.control.enable
   f.info.precedence              # Precedence.SW
   f.info.paritycheck             # False
   f.info.is_volatile             # False
   f.info.is_interrupt_source     # False

``repr``, ``print``, ``help``
-----------------------------

Three rendering surfaces, three audiences. ``repr`` is for the REPL prompt and
is dense; ``print`` is for log output and shows current values; ``help`` is for
``?`` discovery in IPython and includes the RDL description in full.

Compact ``repr`` — what you see at the REPL prompt:

.. code-block:: text

   >>> soc.uart.control
   <Reg uart[0].control @ 0x40001000  rw  reset=0x00000000>
     [0]    enable    rw  "Enable UART"
     [3:1]  baudrate  rw  encode=BaudRate  "Baudrate selection"
     [5:4]  parity    rw  encode=Parity    "Parity mode"

``print`` — performs the read and shows decoded values:

.. code-block:: text

   >>> print(soc.uart.control)
   peripherals.uart[0].control = 0x00000022  @ 0x40001000
     [0]    enable    = 1                       "Enable UART"
     [3:1]  baudrate  = BaudRate.BAUD_19200 (1) "Baudrate selection"
     [5:4]  parity    = Parity.NONE         (0) "Parity mode"

``help`` — full datasheet entry for a single field:

.. code-block:: text

   >>> help(soc.uart.control.baudrate)
   Field uart[0].control.baudrate, bits [3:1], rw
   "Baudrate selection (0=9600, 1=19200, 2=115200)"
   encode = BaudRate {BAUD_9600=0, BAUD_19200=1, BAUD_115200=2}
   on_read  = none      on_write = none

Tree dumps
----------

When you want the whole subtree at once, three top-level helpers cover the
common cases:

.. code-block:: python

   soc.dump()             # walk the entire tree, with current values if a master is attached
   soc.uart.dump()        # same, scoped to a subtree
   soc.tree()             # structural-only — no reads

``dump()`` performs reads (subject to the side-effect rules covered in the
ideal-API sketch §11) and pretty-prints both metadata and live values.
``tree()`` is read-free: it only emits structure, so it's safe to call against
side-effecting registers.

See also
--------

- :doc:`widgets` — notebook rendering of the same tree (rich HTML, click-to-expand).
- :doc:`values_and_io` — what reads return (``RegisterValue``/``FieldValue``)
  and how writes consume them.
