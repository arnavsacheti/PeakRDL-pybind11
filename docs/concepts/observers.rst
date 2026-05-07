Observers and Observation Hooks
===============================

.. note::

   This page describes the **aspirational API** sketched in
   :doc:`/IDEAL_API_SKETCH` §16.2. The current implementation may differ;
   the unified observer chain is the design target.

PeakRDL-pybind11 routes every read and every write through a single
**observer chain**. Coverage tools, audit logs, assertion frameworks, and
live notebook widgets all subscribe to the same hook stream rather than
each one wrapping the master with its own decorator.

Why this matters
----------------

A SoC under test is rarely poked by a single agent. A typical session
mixes:

* a coverage collector recording which fields were exercised,
* an audit log capturing every write for post-mortem reproduction,
* an assertion framework checking invariants on every transaction,
* a live notebook widget that re-renders when watched registers change.

Without a unified mechanism, each of these wraps the master, the wrappers
stack in arbitrary order, and adding a fifth tool means another decorator
layer. With observers, every consumer **subscribes** to the same chain.
The master stays plain; the chain stays composable.

Adding observers
----------------

Register a callback for read events, write events, or both. Each callback
receives an event object describing the transaction:

.. code-block:: python

   soc.observers.add_read(lambda evt: cov.record(evt.path, evt.value))
   soc.observers.add_write(lambda evt: audit.log(evt))

The ``evt`` object exposes:

* ``evt.path`` -- dotted RDL path of the node touched (e.g. ``"uart.control"``)
* ``evt.address`` -- byte address of the underlying register
* ``evt.value`` -- the value read or written
* ``evt.op`` -- ``"read"`` or ``"write"``
* ``evt.timestamp`` -- monotonic time of the transaction

Observers run in registration order, after the master returns.

Scoped observation
------------------

For test cases that only need observation during a specific block, use
``soc.observe()`` as a context manager. On exit, ``obs`` reports what was
exercised inside the block:

.. code-block:: python

   with soc.observe() as obs:
       run_test()

   print(obs.coverage_report())
   # -> which regs/fields were read or written inside the block

Scoped observers do not survive past the ``with`` block; they self-detach
on exit, even if ``run_test()`` raises.

Filtering by path
-----------------

To limit a hook to a subtree, pass ``where=`` with a glob-style pattern.
The predicate is the same one used by :doc:`/bus_layer` for
``attach_master(where=...)``:

.. code-block:: python

   soc.observers.add_read(my_handler, where="uart.*")

Patterns match against ``evt.path``. ``"uart.*"`` selects every direct
child of the ``uart`` block; ``"uart.**"`` selects the whole subtree.

Unified mechanism
-----------------

The observer chain is the **single mechanism** behind several visible
features:

* ``RecordingMaster`` -- subscribes for replay and golden-trace capture.
* ``--coverage`` (CLI flag) -- attaches a coverage collector for the
  duration of a run.
* The live notebook ``watch()`` widget -- subscribes to keep its
  rendered view in sync with hardware state.
* User-written audit, assertion, and instrumentation tools -- the same
  API, no privileged path.

One mechanism, four users. Adding a fifth user means writing a
subscriber, not patching the bus layer.

Performance note
----------------

Observers add per-transaction Python overhead: each registered callback
runs on every matching event. For tight inner loops -- bulk memory
sweeps, large register arrays, performance benchmarks -- prefer:

* :doc:`/snapshots` (``Snapshot.diff()``) to capture before/after state
  in two transactions instead of one-per-access.
* Burst reads (``mem.read(offset, count=...)``) to amortize the per-call
  cost across many words.

Observers are **off by default**. They cost nothing until you call
``add_read``, ``add_write``, or enter an ``observe()`` block.

See also
--------

* :doc:`/bus_layer` -- how the master and ``attach_master(where=...)``
  predicates work.
* :doc:`/snapshots` -- low-overhead state capture for tight loops.
* :doc:`/widgets` -- the live notebook widget, a built-in observer
  consumer.
