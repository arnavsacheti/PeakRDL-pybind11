Usage Guide
===========

A tour of PeakRDL-pybind11 from the command line, through generation, into the
generated module and out to the concept pages where each topic is treated in
depth.

The canonical atomic update is ``modify(**fields)`` — a single read-modify-write
that takes named field values and is the form most user code should reach for.

CLI
---

The exporter ships as a ``peakrdl`` subcommand. Run it on a SystemRDL source
tree to produce a buildable Python module:

.. code-block:: bash

   peakrdl pybind11 input.rdl -o output_dir --soc-name MySoC --top top_addrmap --gen-pyi

Build-time options
^^^^^^^^^^^^^^^^^^

These flags shape the generated module before you ever ``pip install`` it.

``--soc-name``
   Name of the generated SoC module. Defaults to a name derived from the input
   file. The generated package will be importable as ``import <soc_name>``.

``--top``
   Top-level RDL ``addrmap`` node to export. Defaults to the top-level node of
   the elaborated tree. Use this to export a sub-tree of a larger design.

``--gen-pyi``
   Generate ``.pyi`` stub files. Stubs declare every node, every enum, every
   field as a typed surface — IDE autocomplete and ``mypy``/``pyright`` see
   field names, enum members, and ``modify(**fields)`` keyword types. On by
   default; turn off only if you're sure you don't want them.

``--split-bindings COUNT``
   Split bindings into multiple ``.cpp`` files for parallel compilation when
   the register count exceeds ``COUNT`` (default ``100``, ``0`` disables the
   split). Large designs benefit from this because pybind11 binding files are
   compile-time-heavy.

``--split-by-hierarchy``
   Split bindings along ``addrmap``/``regfile`` boundaries instead of by raw
   register count. Recommended for designs that have a clean structural
   hierarchy — files line up with the RDL tree, which makes incremental
   rebuilds cheaper.

``--strict-fields=false``
   Build-time opt-out that allows bare attribute assignment such as
   ``soc.uart.control.enable = 1`` to perform a single-field RMW. Default is
   strict, which forbids the assignment outside a ``with`` context and tells
   you to use ``modify(**fields)`` or the field's ``write()``. The opt-out
   exists for teams porting C drivers that depend on attribute-assign-as-RMW;
   it emits a ``DeprecationWarning`` on import and at every loose assignment.
   Silent RMW is the leading source of "I thought that wrote" test bugs, so
   the noise is intentional.

Runtime subcommands
^^^^^^^^^^^^^^^^^^^

Beyond generation, the CLI provides workflow helpers for bring-up, debugging,
and incremental development.

``--explore mychip``
   Spawn a REPL with ``soc`` already created and a default master attached.
   Inside IPython, ``?soc.uart.control`` shows full metadata and
   ``??soc.uart.control`` shows the originating RDL source. The fastest path
   from "module built" to "poking hardware".

``--diff snapA snapB``
   Render a text or HTML diff of two snapshots produced by ``soc.snapshot()``.
   Used in CI to assert that a test only touched the registers it was supposed
   to, and in lab work to compare "before vs. after a sequence" without
   eyeballing hex dumps.

``--replay session.json``
   Replay a recorded session against a target. Sessions are produced by
   ``RecordingMaster`` or ``soc.trace().save()``; replay is the basis for
   regression tests, post-mortem reproduction of a hardware bug, and
   simulator-vs-silicon delta runs.

``--watch input.rdl``
   Rebuild and reload the bound module when the RDL source changes. Hardware
   bus state is preserved across reloads — only the host-side bindings get
   replaced. The runtime warns on reload, invalidates outstanding
   ``RegisterValue`` and ``Snapshot`` handles, and refuses if a context
   manager is active. Set ``peakrdl.reload.policy = "fail"`` to abort instead
   of warning.

Generating bindings
-------------------

The Python API exposes the same functionality as the CLI for callers that want
to drive generation from a script (build hooks, test harnesses, CI):

.. code-block:: python

   from peakrdl_pybind11 import Pybind11Exporter
   from systemrdl import RDLCompiler

   rdl = RDLCompiler()
   rdl.compile_file("input.rdl")
   root = rdl.elaborate()

   exporter = Pybind11Exporter()
   exporter.export(
       root,
       "output_dir",
       soc_name="MySoC",
       split_by_hierarchy=True,   # cleaner builds for designs with structure
   )

The output directory is a Python package with a ``CMakeLists.txt``; install it
with ``pip install ./output_dir`` to get the importable ``MySoC`` module.

Using generated modules
-----------------------

The rest of this section is a quick tour of the generated surface. Each item
links to a concept page at the end if you want the full treatment.

Create and attach a master
^^^^^^^^^^^^^^^^^^^^^^^^^^

Every generated module exposes a ``create()`` factory. Without a master, reads
and writes will not have anywhere to go — most workflows attach a master at
construction:

.. code-block:: python

   import MySoC
   from peakrdl_pybind11.masters import MockMaster

   soc = MySoC.create(master=MockMaster())

   # Or attach later, optionally per-region:
   soc = MySoC.create()
   soc.attach_master(MockMaster(), where="peripherals.*")

Read a register
^^^^^^^^^^^^^^^

A register read is a single bus transaction. The return is a ``RegisterValue``
— an immutable, hashable wrapper around the integer with field-level
introspection:

.. code-block:: python

   v = soc.uart.control.read()    # → RegisterValue
   v.enable                       # int (1 for set)
   v.baudrate                     # → BaudRate.BAUD_115200 (enum)
   v.replace(enable=0)            # new RegisterValue, no bus traffic
   v.hex()                        # "0x00000022"

Write a whole register
^^^^^^^^^^^^^^^^^^^^^^

A register write is also a single bus transaction. ``write(value)`` clobbers
every field; reach for it when you have the full word, not when you want to
change one field of many:

.. code-block:: python

   soc.uart.control.write(0x1234)      # raw 32-bit write
   soc.uart.control.write(v)            # round-trip a previously read RegisterValue
   soc.uart.control.poke(0x1234)        # alias of write — the "I know what I'm doing" name

Atomic multi-field update — the canonical form
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The most common pattern in real driver code is "set these fields, leave
everything else alone". That is ``modify(**fields)`` — one read, one write,
named keyword arguments type-checked against the generated stubs:

.. code-block:: python

   from MySoC.uart import BaudRate, Parity

   soc.uart.control.modify(
       enable=1,
       baudrate=BaudRate.BAUD_115200,
       parity=Parity.NONE,
   )

This is the API that should appear in tutorials, examples, and review
suggestions. Bare attribute assignment such as ``soc.uart.control.enable = 1``
raises outside a context manager — it's the most common footgun in C-derived
register code, and the strict default exists to surface that.

Field-level reads
^^^^^^^^^^^^^^^^^

Field reads return decoded values. A 1-bit field reads as a ``bool``-compatible
integer; an enum-encoded field reads as an enum member:

.. code-block:: python

   soc.uart.intr.tx_done.read()              # → bool (True if pending)
   soc.uart.control.baudrate.read()          # → BaudRate.BAUD_19200
   soc.uart.control.baudrate.write(BaudRate.BAUD_115200)   # single-field RMW

Context manager — staged writes (secondary)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

For sequences that build up several field changes around intermediate reads,
the register itself can serve as a context manager. Inside the ``with`` block,
``r.field = value`` is sugar for staging — nothing hits the bus until the
block exits, at which point exactly one read and one write occur:

.. code-block:: python

   with soc.uart.control as r:
       r.enable    = 1
       r.baudrate  = BaudRate.BAUD_115200
       if r.parity.read() == Parity.NONE:
           r.parity = Parity.EVEN
   # 1 read + 1 write hit the bus on exit.

This is a secondary surface. Reach for ``modify(**fields)`` first; reach for
the context manager only when you need an intermediate ``read()`` to decide
the next staged value. Outside the context, the same ``r.field = value``
syntax raises, by design.

Interrupts
^^^^^^^^^^

Interrupt-bearing fields synthesize an ``InterruptGroup`` node from the
``intr_state``/``intr_enable``/``intr_test`` trio. The group exposes the
familiar wait/clear/enable surface:

.. code-block:: python

   soc.uart.interrupts.tx_done.wait(timeout=1.0)    # block until pending
   soc.uart.interrupts.tx_done.clear()              # do the right thing per RDL
   soc.uart.interrupts.tx_done.enable()             # set the enable bit
   for irq in soc.uart.interrupts.pending():        # iterate-and-ack pattern
       handle(irq)
       irq.clear()

Snapshots and diff
^^^^^^^^^^^^^^^^^^

A snapshot captures the readable state of the SoC (or a subtree) into a
picklable, JSON-serializable object. Diffing two snapshots is the building
block for golden-state checks, regression assertions, and "what did this
sequence touch" inspection:

.. code-block:: python

   snap = soc.snapshot()                # whole tree
   sub  = soc.uart.snapshot()           # just the uart subtree
   run_test()
   delta = soc.snapshot().diff(snap)
   delta.assert_only_changed("uart.intr_state.*", "uart.data")

Snapshots ``peek()`` by default and abort if a required read would be
destructive; pass ``allow_destructive=True`` only when you've decided you're
fine with the side effect.

Where to go next
----------------

Each topic above has a dedicated concept page with the full treatment —
edge cases, error model, and worked examples.

Core hierarchy and values
^^^^^^^^^^^^^^^^^^^^^^^^^

* :doc:`concepts/hierarchy` — the ``soc.peripherals.uart[0].control`` tree, navigation, ``info`` namespace.
* :doc:`concepts/values_and_io` — ``RegisterValue`` and ``FieldValue`` semantics, formatting, immutability.
* :doc:`concepts/widgets` — Jupyter rich repr, ``watch()``, live monitors, the notebook surface.

Storage and structure
^^^^^^^^^^^^^^^^^^^^^

* :doc:`concepts/memory` — ``Mem`` regions, ``MemView``, NumPy interop, bursts.
* :doc:`concepts/arrays` — single- and multi-dim register arrays, bulk reads, field arrays.
* :doc:`concepts/enums_flags` — ``IntEnum`` per encoded field, ``IntFlag`` per flag register, ``set()``/``clear()``/``toggle()``.

Behavior and side effects
^^^^^^^^^^^^^^^^^^^^^^^^^

* :doc:`concepts/interrupts` — ``InterruptGroup`` detection, per-source ops, wait and async, group ops.
* :doc:`concepts/aliases` — RDL ``alias`` registers and the canonical/view relationship.
* :doc:`concepts/side_effects` — ``rclr``/``woclr``/``singlepulse``, ``peek()``/``clear()``/``set()``/``pulse()``/``acknowledge()``.
* :doc:`concepts/specialized` — counters, lock, reset semantics, external regs.

Bus and execution
^^^^^^^^^^^^^^^^^

* :doc:`concepts/bus_layer` — masters, transactions, barriers, retries, tracing, mocks.
* :doc:`concepts/wait_poll` — ``wait_for``, ``wait_until``, sample/histogram, async equivalents.
* :doc:`concepts/snapshots` — full snapshot/restore, JSON, diff, partial trees.
* :doc:`concepts/observers` — read/write hook chain, coverage, audit, scoped ``observe()``.

Operational
^^^^^^^^^^^

* :doc:`concepts/errors` — the ``AccessError``, ``BusError``, ``RoutingError``, ``SideEffectError``, ``NotSupportedError`` family.
* :doc:`concepts/cli_repl` — ``--explore``/``--diff``/``--replay``/``--watch`` workflows in detail.
