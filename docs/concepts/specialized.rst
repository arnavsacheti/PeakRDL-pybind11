Specialized Register Kinds
==========================

Most registers in a SystemRDL design are plain RW words. A handful are not:
they tick on their own (counters), self-clear after a single cycle
(singlepulse), guard a reset sequence, or are locked by a key. Each kind
gets a small, opinionated wrapper on top of the four primitives in
:doc:`/concepts/values_and_io` so the typical operation is one obvious method
call, the bus cost is honest, and the side-effecting cases are surfaced
loudly rather than hidden behind a generic ``write``.

This page is the canonical reference for those wrappers. If a register has
none of these special properties, this page does not apply — use ``read()``,
``write()``, ``modify()`` from the values-and-I/O page directly.

Counters
--------

When a field carries the RDL ``counter`` property, PeakRDL-pybind11 emits a
``Counter`` wrapper rather than a bare field. A counter knows its own
saturation rules, its threshold (if any), and which of increment/decrement
the hardware actually supports.

.. code-block:: python

   c = soc.peripheral.event_counter
   c.value()             # current count (read)
   c.reset()             # clear (per RDL — could be wclr or hwreset)
   c.threshold()         # if `incrthreshold` set
   c.is_saturated()      # if `incrsaturate`
   c.increment(by=1)     # if sw-incr supported (writes the incrvalue field)
   c.decrement(by=1)     # if `decr` supported

``c.value()`` is one bus read. ``c.reset()`` is one bus write whose exact
semantics are determined by the RDL — typically a ``wclr`` write or a
hardware-driven reset path. ``c.increment(by=N)`` and ``c.decrement(by=N)``
exist only when the source RDL declares software-incrementable or
software-decrementable counters; ``c.threshold()`` and ``c.is_saturated()``
exist only when ``incrthreshold`` / ``incrsaturate`` are set. When the
underlying property is absent, the wrapper does not silently no-op — the
method is not generated in the first place, so a typo in user code is an
``AttributeError`` at the call site.

Singlepulse
-----------

A ``singlepulse`` field self-clears one cycle after a write of 1. The
register's ``write()`` would clobber other fields, so the wrapper exposes a
dedicated ``pulse()`` instead:

.. code-block:: python

   soc.dma.channel[0].control.start.pulse()    # writes 1; hardware clears

``pulse()`` is one bus write. The bit reads back as 0 immediately after
because the hardware has already cleared it; this is correct, not a bug, and
``pulse()`` is named to make that visible at the call site. Use ``pulse()``
for "kick this state machine" semantics rather than reaching for the
generic ``field.write(1)`` form — the dedicated name is the documented
surface for singlepulse fields.

Reset semantics
---------------

Every register knows its declared reset value, can compare its current value
against that reset value, and can write itself (or its parent subtree) back
to those reset values explicitly. This is the right tool for "give me a
clean baseline before this test" and for snapshot-driven golden-state checks.

.. code-block:: python

   soc.uart.control.reset_value       # 0x0
   soc.uart.control.is_at_reset()     # read & compare
   soc.uart.reset_all(rw_only=True)   # write reset values to writable regs
   soc.reset_all()                    # whole tree, with safety check on side-effecting fields

``reg.reset_value`` is a static property derived from the RDL — no bus
traffic. ``reg.is_at_reset()`` is one bus read followed by an in-process
comparison.

The ``reset_all`` calls are deliberately conservative:

- ``rw_only=True`` is the **default**. Read-only registers are skipped, since
  you cannot write back a hardware-driven status. Pass ``rw_only=False``
  only if you really mean "panic-write the whole subtree", and the RDL
  declares those registers writable in some access mode.
- The whole-tree ``soc.reset_all()`` runs a side-effect safety check before
  issuing any writes. Each register slated for restore is inspected for
  fields that combine RW with ``rclr`` semantics — those are flagged as
  ambiguous to "reset" because the next read will both compare against the
  reset value and clear the field as a read side effect.

.. warning::

   ``soc.reset_all()`` warns if any RW register also has an ``rclr`` field.
   Such fields make "reset" ambiguous: the act of verifying reset state
   itself mutates the field. Resolve by passing an explicit list of paths
   to skip, or by using the per-register ``reg.reset_value`` and
   ``reg.write(...)`` calls in a controlled order.

For a single register's reset, write the value directly:

.. code-block:: python

   soc.uart.control.write(soc.uart.control.reset_value)

This is one bus write and reads cleanly without dragging in the
whole-subtree machinery.

Lock
----

The RDL ``lock`` family covers fields whose writes are gated by a separate
key sequence — STM32-style ``LCKR`` registers are the canonical example. The
exporter emits ``lock`` / ``is_locked`` / ``unlock_sequence`` accessors on
the gating register so the user does not hand-roll the key dance:

.. code-block:: python

   soc.gpio_a.lckr.lock(["pin0", "pin5"])   # programs LCK + sets LCKK key per RDL `lock`
   soc.gpio_a.lckr.is_locked("pin0")        # True
   soc.gpio_a.lckr.unlock_sequence()        # explicit, vendor-specific UDP

``lock(paths)`` programs the lock-bits register with the named field set,
then drives the key write sequence the RDL ``lock`` property declares.
``is_locked(name)`` returns ``True``/``False`` for one named lock target.
``unlock_sequence()`` is generated when (and only when) the RDL or a
vendor-specific UDP declares an explicit unlock path; on parts that do not
support unlock — many do not, by design — the method is simply absent.

The exact bus traffic of ``lock`` and ``unlock_sequence`` is part-specific
and visible through the trace surface; expect more than one transaction
because the key sequence itself is multiple writes by definition.

External regs
-------------

The RDL ``external reg`` keyword declares a register whose backing storage
lives outside the generated regblock — typically a different master serves
its address. PeakRDL-pybind11 emits the **same** ``Reg`` node for an
``external reg`` as for an ordinary one, with the same primitive ops,
the same field accessors, and the same value types.

.. code-block:: python

   soc.peripherals.foo.external_reg.read()       # same as any other Reg
   soc.peripherals.foo.external_reg.modify(enable=1)

The only difference is bus dispatch: when masters are routed by region (see
:doc:`/concepts/bus_layer`), an ``external reg`` may end up on a different
master than its siblings. From the Python user's perspective, that
distinction is invisible by design — there is one API regardless of where
the bytes ultimately land.

If a routing decision is unexpectedly wrong, ``reg.info.address`` and the
master-routing tools described in :doc:`/concepts/bus_layer` are the
discoverability surface, not a separate API for external registers.

See also
--------

- :doc:`/concepts/side_effects` — singlepulse and counter saturation
  intersect with the broader rules around read-side and write-side effects;
  read that page for ``rclr``, ``woclr``, and the ``no_side_effects()``
  context.
- :doc:`/concepts/bus_layer` — external registers and per-region master
  routing; the place to look when an ``external reg`` lands on a different
  bus master than its siblings.
