Values and I/O
==============

This page is the canonical reference for how Python code reads from and writes to
hardware registers in PeakRDL-pybind11, and for what those reads return. Every
register and field has the same small set of primitive operations, with explicit
bus costs and predictable typed return values.

The design principles that drive the rules on this page:

- **One transaction per primitive op.** ``reg.write(v)`` is exactly one bus
  write. ``reg.read()`` is exactly one bus read. RMW (read-modify-write) only
  appears where the abstraction makes it unavoidable, and those names —
  ``field.write`` and ``reg.modify`` — are chosen to read clearly without lying
  about cost.
- **Returns are typed.** A field with ``encode = BaudRate`` reads back as
  ``BaudRate.BAUD_115200``, not ``2``.
- **Side effects are loud.** Attribute assignment outside a context manager
  raises rather than silently issuing an RMW.

The four primitive ops
----------------------

Every register exposes the same four methods. The bus cost is fixed and
documented:

.. list-table::
   :header-rows: 1
   :widths: 25 50 25

   * - Operation
     - Semantics
     - Bus cost
   * - ``reg.read()``
     - Read the register; return a ``RegisterValue``.
     - 1 read
   * - ``reg.write(value)``
     - Raw write, no read first. Bits not represented in ``value`` are written
       as zero.
     - 1 write
   * - ``reg.modify(**fields)``
     - Read-modify-write: read the register, splice in the named fields, write
       it back.
     - 1 read + 1 write
   * - ``reg.poke(value)``
     - Same as ``write(value)`` but explicit; reads as "I know what I'm
       doing" at call sites.
     - 1 write — same as ``write`` but explicit

``poke`` is provided so that a code review can tell at a glance that a raw
write (rather than an RMW) was intentional. It does not bypass any safety
check that ``write`` performs.

Field reads and writes
----------------------

Field operations sit on top of the same four primitives. ``field.read()``
issues one bus read and slices out the field's bits. ``field.write(v)`` is a
**single-field RMW** — it cannot be a single bus write on a multi-field
register, because that would clobber the other fields.

.. code-block:: python

   soc.uart.control.baudrate.read()    # 1 bus read    → BaudRate.BAUD_19200
   soc.uart.control.baudrate.write(BaudRate.BAUD_115200)
   # 1 read + 1 write (RMW)

The name ``write`` is intentional on a field: it is named to read clearly
without lying about the cost. If you want to update several fields without
paying for one RMW per field, use ``reg.modify(**fields)``.

Multi-field atomic update
-------------------------

When you need to change more than one field at once, ``reg.modify(**fields)``
is the canonical form. It is **a single RMW** regardless of how many fields
are passed:

.. code-block:: python

   # One bus read + one bus write, no matter how many fields.
   soc.uart.control.modify(
       enable=1,
       baudrate=BaudRate.BAUD_115200,
       parity=Parity.NONE,
   )

Keyword arguments map to fields by name and are type-checked against the
generated ``.pyi`` stubs, so unknown names and wrong enum types are caught
before you hit hardware.

If you already know the full register value (no host-side read required),
build a value directly and write it:

.. code-block:: python

   # Compose-then-write — no bus read needed.
   soc.uart.control.write(
       UartControl.build(enable=1, baudrate=2)
   )
   # Fields not passed to .build() take their reset value.

``UartControl.build(...)`` is a class method emitted by the exporter for every
register; it returns a ``RegisterValue`` (see below) that round-trips through
``write()``.

Context manager (secondary)
---------------------------

The context manager is the right tool when a single hardware transaction
should bundle multiple staged reads and writes — for example, reading a field
to decide what to write to another field, all on one register, with a single
bus read and a single bus write at exit.

.. code-block:: python

   with soc.uart.control as r:
       r.enable    = 1
       r.baudrate  = BaudRate.BAUD_115200
       if r.parity.read() == Parity.NONE:
           r.parity = Parity.EVEN
   # 1 read + 1 write hits the bus on exit.

Inside the ``with`` block, ``r.enable = 1`` is sugar for
``r.enable.stage(1)``. Reads (``r.parity.read()``) come from the staged
register value, not the bus, so a context with mixed reads and writes is
still one round trip.

For the common single-RMW case, prefer ``reg.modify(**fields)``: the kwargs
form is shorter, type-checked end to end, and reads as a single transaction
at the call site. The context manager earns its keep when the staged writes
depend on intermediate reads.

Attribute writes outside contexts
---------------------------------

To kill the most common footgun (``reg.enable = 1`` while expecting a bus
transaction), bare attribute assignment on a register **outside a context
manager** raises:

.. code-block:: python

   soc.uart.control.enable = 1

Produces:

.. code-block:: text

   AttributeError: assigning to a field outside a context. Use:
     soc.uart.control.enable.write(1)         # RMW
     soc.uart.control.modify(enable=1)        # RMW
     with soc.uart.control as r: r.enable = 1 # batched

The error message lists the three correct alternatives so the fix is always
one copy-paste away.

Strict-fields opt-out (``--strict-fields=false``)
-------------------------------------------------

A build-time toggle, ``--strict-fields=false``, exists for teams porting C
drivers that depend on attribute-assign-as-RMW semantics. The default is
strict.

.. warning::

   ``--strict-fields=false`` makes ``reg.field = value`` silently issue an RMW
   on every assignment. **It is a footgun by design.** Silent RMW on bare
   attribute assignment is the single most common source of "I thought that
   wrote" test bugs.

When the opt-out is enabled, the generated module emits a
``DeprecationWarning`` at import **and** on every loose assignment. The
warning is annoying on purpose. Per-instance toggling intentionally does not
exist — the policy is a single bit, set at build time.

Use ``--strict-fields=false`` only as a porting bridge. New code should use
``modify(**fields)`` or the context manager.

Typed return values
-------------------

Reads do not return bare ``int``. They return ``RegisterValue`` (for a
register read) or ``FieldValue`` (for a field read). Both are **immutable and
hashable** — safe as dict keys for snapshots, coverage maps, and
golden-state checks. Both are picklable and JSON-serializable for
distributed test harnesses and CI artefacts.

Mutation goes through ``.replace(**fields)`` (returns a new value) and never
through assignment.

.. code-block:: python

   v = soc.uart.control.read()
   print(v)
   # UartControl(0x00000022)
   #   enable[0]    = 1
   #   baudrate[3:1]= BaudRate.BAUD_19200  (1)
   #   parity[5:4]  = Parity.NONE          (0)

   v == 0x22                     # True, RegisterValue is int-compatible
   v.enable                      # 1 (also v["enable"])
   v.baudrate                    # <BaudRate.BAUD_19200: 1>
   v.replace(enable=0)           # → new RegisterValue with field swapped
   soc.uart.control.write(v)     # round-trips

Format helpers
~~~~~~~~~~~~~~

``RegisterValue`` and ``FieldValue`` provide the formatting helpers users
always end up wanting:

.. code-block:: python

   v.hex()                       # "0x00000022"
   v.hex(group=4)                # "0x0000_0022"
   v.bin()                       # "0b00000000_00000000_00000000_00100010"
   v.bin(group=8, fields=True)   # annotates groups with field boundaries
   print(reg, fmt="bin")         # alt-format on the live read
   soc.uart.control.read().table()   # ASCII table of fields, ready for logs

The ``.table()`` form is especially useful in CI logs and bug reports: it
prints field-by-field rows that survive copy-paste.

Why immutable values?
~~~~~~~~~~~~~~~~~~~~~

There is one allocation per read. The alternative — mutable shared state
pretending to be a value — leaks bugs (a stale read silently mutating, a
snapshot dict whose keys all alias the same int). The cost is paid on
purpose. ``Snapshot`` follows the same rule.

Field reads return enums when the field has ``encode``, and ``bool`` for
1-bit fields:

.. code-block:: python

   soc.uart.control.baudrate.read()   # → BaudRate.BAUD_19200
   soc.uart.intr.tx_done.read()       # → bool (1-bit field)

Bit-level access in multi-bit fields
------------------------------------

Multi-bit fields often pack N independent flags (e.g. a 16-bit ``direction``
where each bit is one GPIO line). The ``.bits`` accessor gives single-bit and
slice-level access without breaking the field abstraction:

.. code-block:: python

   soc.gpio.direction.bits[5].read()        # bool
   soc.gpio.direction.bits[5].write(1)      # RMW that touches one bit only
   soc.gpio.direction.bits[0:8].read()      # ndarray[bool], length 8
   soc.gpio.direction.bits[:].write(0xFF00) # bitmask to bool array

``bits[i].write(...)`` is a single-bit RMW, with the same bus cost as a
single-field RMW: 1 read + 1 write. Slicing (``bits[0:8]``) returns a
NumPy array of ``bool`` and is the right way to dump or apply a bitmask.
