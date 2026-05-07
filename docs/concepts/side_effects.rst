Read/Write Side Effects
=======================

.. note::

   **Aspirational documentation.** This page describes the target API
   defined in ``docs/IDEAL_API_SKETCH.md`` (§11). Some of the surfaces
   below may not be implemented yet — this page is the contract we
   build toward, not a snapshot of the current shipping module.

Why this matters
----------------

A core design principle of PeakRDL-pybind11 is that **side effects are
loud, not silent.** A read that clears the field, a write that pulses
hardware, a sticky bit that hardware will only update while it is zero
— these are the bugs that turn into multi-day debugging sessions when
they hide behind innocent-looking ``reg.read()`` calls.

The API surfaces every read-side and write-side effect in three places:

- ``repr()`` and ``_repr_html_()`` carry side-effect badges (⚠ rclr,
  ↻ singlepulse, ✱ sticky, ⚡ volatile).
- The metadata on ``info.on_read`` / ``info.on_write`` /
  ``info.is_volatile`` is one attribute away from any node.
- Verbs are named after the action: ``clear()``, ``set()``,
  ``pulse()``, ``acknowledge()``, ``peek()``. RMW reads that *would*
  destroy state are explicit; ``peek()`` is the safe alternative.

The classes
-----------

Every read-side and write-side effect declared by SystemRDL maps to a
known Python surface:

.. list-table::
   :header-rows: 1
   :widths: 22 38 40

   * - RDL property
     - Meaning
     - Surface
   * - ``onread = rclr``
     - Read clears the field
     - ``read()`` warns or ``peek()`` required
   * - ``onread = rset``
     - Read sets the field
     - same
   * - ``onwrite = woclr``
     - Write 1 clears (W1C)
     - ``clear()`` writes 1
   * - ``onwrite = woset``
     - Write 1 sets (W1S)
     - ``set()`` writes 1
   * - ``onwrite = wzc/wzs``
     - Write 0 clears/sets
     - inverted
   * - ``onwrite = wclr``
     - Any write clears
     - ``.clear()`` writes anything
   * - ``singlepulse``
     - Field self-clears after 1 cycle
     - ``pulse()``
   * - ``hwclr``/``hwset``
     - Hardware can change without sw write
     - ``.is_volatile``
   * - ``sticky``/``stickybit``
     - Hardware writes only update if currently zero
     - metadata only
   * - ``paritycheck``
     - Parity bit appended for RAS
     - ``info.paritycheck = True``

Surface methods
---------------

The metadata namespace and the verbs are the same on every field:

.. code-block:: python

   f = soc.intr_status.tx_done

   f.info.on_read     # ReadEffect.NONE | RCLR | RSET | RUSER
   f.info.on_write    # WriteEffect.NONE | W1C | W1S | WZC | WZS | WCLR | WSET | WUSER
   f.info.is_volatile # True if hwclr/hwset/sticky/counter — value can change without sw

   f.read()           # standard read; if rclr, this CLEARS the bit. Logged at INFO.
   f.peek()           # read without clearing IF the master supports it; else raises
                       # (some buses literally cannot peek; we don't pretend)
   f.clear()          # W1C → writes 1; rclr → does a read; wclr → writes 0; raises if no clear path
   f.acknowledge()    # alias of clear() — reads better in ISR code
   f.set()            # symmetric: W1S → writes 1; raises if not settable
   f.pulse()          # singlepulse → writes 1, hardware clears

A note on ``peek()``: some buses *literally cannot read* without
clearing the field (the bus protocol bakes the clear into the read
cycle). On those masters, ``peek()`` of an ``rclr`` field raises
``NotSupportedError`` rather than silently doing a destructive read.
The API does not pretend a non-destructive read exists when it
doesn't.

Repr surfaces side effects
--------------------------

The ``repr`` of a register or field exposes its side effects directly,
so that a user landing in a REPL sees the danger before they try a
``read()``:

.. code-block:: text

   >>> soc.system.reset_status
   <Reg system.reset_status @ 0x40000018  ro  reset=0x00>  ⚠ side-effecting reads (rclr)
     [0] por_flag        ro  rclr  "Power-on-reset latched flag"
     ...

The same badges (⚠ rclr, ↻ singlepulse, ✱ sticky, ⚡ volatile) appear
in ``_repr_html_()`` for notebook surfaces and in autocompletion
hints. Once a user has seen the ⚠ glyph next to ``reset_status``,
they know to reach for ``peek()`` instead of ``read()``.

Guard against accidental clears
-------------------------------

Debug dumps, assertion frameworks, and read-only inspectors must not
mutate hardware state. The ``soc.no_side_effects()`` context manager
upgrades any side-effecting read into an exception:

.. code-block:: python

   with soc.no_side_effects():
       soc.system.reset_status.por_flag.read()
       # SideEffectError: read() of por_flag would clear (rclr).
       # Use .peek() instead, or remove the no_side_effects() guard.

Inside the block, ``peek()`` on the same field still works (assuming
the master can peek) — the guard only forbids reads that *would*
change state. Snapshots (``soc.snapshot()``) use the same machinery
internally: by default they ``peek()`` and abort on any required
destructive read; pass ``allow_destructive=True`` to override.

See also
--------

- :doc:`/values_and_io` — how typed values round-trip through reads
  and writes, and how RMW is named.
- :doc:`/interrupts` — interrupts use W1C heavily; ``acknowledge()``
  is the spelling you want in ISR-shaped code.
- :doc:`/snapshots` — ``peek()`` is the default read for snapshots,
  precisely because of the rules on this page.
