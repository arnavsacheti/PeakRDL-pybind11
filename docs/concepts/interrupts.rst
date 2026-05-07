Interrupts
==========

Interrupts are the marquee feature of PeakRDL-pybind11's high-level API. They
turn the most painful part of bring-up — chasing pending bits, masking the
right line, writing the test bit, polling for completion — into a small, named
vocabulary that reads the same regardless of how the underlying RDL spelled
the trio.

Overview
--------

SystemRDL marks interrupt-bearing fields with the ``intr`` property. The
familiar ``INTR_STATE`` / ``INTR_ENABLE`` / ``INTR_TEST`` trio appears in
OpenTitan, ARM, RISC-V CLIC, and many vendor SoCs. The exporter detects this
trio (or a configurable variation) and synthesizes an ``InterruptGroup`` node
on the parent, hung off the attribute ``.interrupts``.

``InterruptGroup`` is a peer of ``Reg`` and ``RegFile`` in the node tree. Each
matched interrupt source becomes an attribute on the group whose value is an
``Interrupt`` object that knows how to talk to all three registers at once,
using the right primitive (write-1-to-clear, read-to-clear, write-0-to-clear)
for the underlying RDL hardware semantics.

Per-source operations
---------------------

A single interrupt source — e.g. the ``tx_done`` bit on a UART block — is
addressed as a named attribute of the parent's ``.interrupts`` group:

.. code-block:: python

   irq = soc.uart.interrupts.tx_done

   irq.is_pending()        # read state bit
   irq.is_enabled()        # read enable bit
   irq.enable()            # set enable bit (modify, not full write)
   irq.disable()
   irq.clear()             # do the right thing per RDL: woclr, rclr, or wzc
   irq.acknowledge()       # alias for .clear()
   irq.fire()              # write INTR_TEST bit (sw self-trigger)

   irq.wait(timeout=1.0)               # blocks until pending or timeout
   irq.wait_clear(timeout=1.0)         # block until *not* pending
   await irq.aiowait(timeout=1.0)      # asyncio variant
   irq.poll(period=0.001, timeout=1.0) # explicit period

   # Subscription (master-driven if backend supports interrupts; polling otherwise)
   unsubscribe = irq.on_fire(lambda: print("tx_done!"))

.. note::

   ``clear()`` is *not* a generic write. It dispatches on the RDL property of
   the state field: ``woclr`` writes a one to the matching bit, ``rclr``
   issues a read, and ``wzc`` writes a zero. Users do not have to know which
   one their hardware uses; ``acknowledge()`` is provided as a more
   ISR-flavored alias for the same operation.

``fire()`` writes a one into the matching bit of the test register, so the
hardware presents the interrupt as if it had fired naturally. This is the
canonical way to drive the ISR path from a unit test or a Jupyter notebook
without involving the rest of the device.

Group operations
----------------

``InterruptGroup`` itself supports the bulk operations a typical ISR will
want — listing pending sources, masking everything during a critical section,
or snapshotting the whole interrupt state at once:

.. code-block:: python

   soc.uart.interrupts.pending()     # frozenset of IRQ objects with state==1
   soc.uart.interrupts.enabled()     # frozenset
   soc.uart.interrupts.clear_all()
   soc.uart.interrupts.disable_all()
   soc.uart.interrupts.enable(set_={"tx_done", "rx_overflow"})
   soc.uart.interrupts.snapshot()    # dict[name, (state, enable)]

   # Iterate & ack the standard ISR pattern
   for irq in soc.uart.interrupts.pending():
       handle(irq)
       irq.clear()

The frozenset returned by ``pending()`` is hashable and stable across calls
that observe the same hardware state, so it can be used as a dict key in
event-table tests or memoized handlers.

Top-level interrupt tree
------------------------

Every block in the SoC contributes its ``InterruptGroup`` to a global view at
``soc.interrupts``. This is the right place to look when a user does not yet
know which peripheral fired, or wants a single point to wait on:

.. code-block:: python

   soc.interrupts                # global view across all blocks
   soc.interrupts.tree()         # print tree of all IRQs and their state
   soc.interrupts.pending()      # all pending across the SoC
   soc.interrupts.wait_any(timeout=1.0)   # → first pending IRQ object

Detection rules
---------------

The exporter applies a default heuristic to find interrupt trios:

- Default: a register named ``INTR_STATE`` / ``intr_status`` / ``*_INT_STATUS``
  whose fields all have the ``intr`` property triggers the trio search.
- Pair partners by suffix (``_ENABLE``, ``_MASK``, ``_TEST``, ``_RAW``).
- Fields are matched **by name** across the trio.
- An RDL ``--interrupt-pattern`` flag lets users override the matcher
  (regex or callable).

.. note::

   If detection fails, the exporter still emits per-field state — the
   attribute ``field.is_interrupt_source`` is ``True`` even when no
   ``InterruptGroup`` was synthesized. The user can then build a group
   manually (see below). No interrupt-bearing field is ever silently dropped.

Manual ``InterruptGroup``
-------------------------

When the heuristic fails — vendor naming conventions, split address maps,
unusual register layouts — the user can wire up an ``InterruptGroup``
explicitly from the bare register nodes:

.. code-block:: python

   my_irq = InterruptGroup.manual(
       state=soc.foo.IRQ_STAT,
       enable=soc.foo.IRQ_EN,
       test=soc.foo.IRQ_TEST,
   )

The manual group exposes the same per-source and group operations as a
detected one. Per-field, ``field.is_interrupt_source`` remains the source of
truth: it is ``True`` whenever the underlying RDL marks the field with
``intr``, regardless of whether the exporter could synthesize a group around
it.

See also
--------

- :doc:`/widgets` — interrupt matrix widget for Jupyter notebooks.
- :doc:`/wait_poll` — shared waiting and polling primitives.
- :doc:`/side_effects` — RDL clear-on-read, write-1-to-clear, and pulse
  semantics.
