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
receives an ``Event`` object describing the transaction (see
`The Event dataclass`_ below):

.. code-block:: python

   soc.observers.add_read(lambda evt: cov.record(evt.path, evt.value))
   soc.observers.add_write(lambda evt: audit.log(evt))

Observers run in registration order, after the master returns.

The ``Event`` dataclass
-----------------------

Every observer callback receives an ``Event``. It is a frozen, slotted
dataclass (``@dataclass(frozen=True, slots=True)``) so handlers can rely on
identity, hashability, and zero accidental mutation when an event is shared
across the chain.

.. code-block:: python

   from peakrdl_pybind11.runtime.observers import Event

   def my_handler(evt: Event) -> None:
       if evt.op == "write":
           audit.log(evt.path, evt.address, evt.value)

.. list-table:: ``Event`` fields
   :header-rows: 1
   :widths: 22 22 56

   * - Field
     - Type
     - Description
   * - ``path``
     - ``str``
     - Dotted RDL path of the node touched (e.g. ``"uart.control"``).
   * - ``address``
     - ``int``
     - Byte address of the underlying register.
   * - ``value``
     - ``int``
     - The value read or written.
   * - ``op``
     - ``Literal["read", "write"]``
     - Either ``"read"`` or ``"write"``; useful for narrowing in handlers
       that subscribe to both streams.
   * - ``timestamp``
     - ``float``
     - ``time.monotonic()`` value captured when the transaction completed.

Because ``Event`` is frozen, handlers that need to forward a derived event
should construct a new instance (``dataclasses.replace(evt, value=...)``)
rather than mutating in place.

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

The ``CoverageReport`` object
-----------------------------

``obs.coverage_report()`` returns a ``CoverageReport`` -- a structured
summary of every node touched inside the observed block, suitable both
for human inspection and for assertions in CI.

.. code-block:: python

   from peakrdl_pybind11.runtime.observers import CoverageReport

   with soc.observe() as obs:
       run_test()

   report: CoverageReport = obs.coverage_report()

   assert "uart.control" in report.nodes_written
   assert report.total_writes >= 1
   for path, count in report.paths_by_frequency[:5]:
       print(f"{path:32} {count}")

.. list-table:: ``CoverageReport`` attributes
   :header-rows: 1
   :widths: 28 28 44

   * - Attribute
     - Type
     - Description
   * - ``nodes_read``
     - ``set[str]``
     - Dotted paths of every node that was read at least once.
   * - ``nodes_written``
     - ``set[str]``
     - Dotted paths of every node that was written at least once.
   * - ``total_reads``
     - ``int``
     - Total number of read transactions observed.
   * - ``total_writes``
     - ``int``
     - Total number of write transactions observed.
   * - ``paths_by_frequency``
     - ``list[tuple[str, int]]``
     - Per-path access counts, sorted by descending count then by path
       for deterministic output. Each tuple is ``(path, count)`` and
       counts both reads and writes against that path.

The set and counter attributes make it cheap to write coverage gates
(``assert report.nodes_written >= required_paths``) without parsing
text. ``paths_by_frequency`` is the right entry point for a "hot
register" report or for spotting unexpected polling loops in a long
test.

Filtering by path
-----------------

To limit a hook to a subtree, pass ``where=`` with a glob-style pattern.
The predicate is the same one used by :doc:`/bus_layer` for
``attach_master(where=...)``:

.. code-block:: python

   soc.observers.add_read(my_handler, where="uart.*")

Patterns match against ``evt.path``. ``"uart.*"`` selects every direct
child of the ``uart`` block; ``"uart.**"`` selects the whole subtree.

Attaching to a hand-built SoC
-----------------------------

The generated runtime auto-attaches the observer chain on the SoC node it
builds: ``soc.observers`` and ``soc.observe()`` are present immediately
after import. Users who assemble their own SoC harness -- mocking the
top, splicing in stubs, composing peripherals across modules -- need to
opt in once with ``attach_observers``.

.. code-block:: python

   from peakrdl_pybind11.runtime.observers import attach_observers

   my_soc = build_my_custom_soc(...)   # hand-built top
   attach_observers(my_soc)            # adds .observers and .observe()

   my_soc.observers.add_write(lambda evt: audit.log(evt))
   with my_soc.observe() as obs:
       run_test()

After ``attach_observers(soc)`` returns, the SoC behaves identically to a
generated one: every read and every write routes through the same chain,
``soc.observe()`` is a working context manager, and existing tools (the
notebook ``watch()`` widget, ``RecordingMaster``, ``--coverage``) plug in
without further setup.

The call is idempotent. Re-attaching the default chain is a no-op; the
existing subscribers are preserved.

Sharing a chain across SoCs
---------------------------

Pass an explicit ``ObserverChain`` when you want hooks pre-configured
before any SoC is wired up, or when several SoCs in the same process
should funnel into the same audit log:

.. code-block:: python

   from peakrdl_pybind11.runtime.observers import ObserverChain, attach_observers

   shared = ObserverChain()
   shared.add_write(lambda evt: audit.log(evt))   # configure once

   attach_observers(soc_a, chain=shared)
   attach_observers(soc_b, chain=shared)          # both feed `audit`

   # `shared` is the same object now exposed as soc_a.observers and
   # soc_b.observers, so adding/removing hooks at runtime affects both.
   shared.add_read(coverage.record, where="uart.*")

``ObserverChain`` exposes the same surface as ``soc.observers`` --
``add_read``, ``add_write``, ``remove``, and the ``where=`` predicate --
so a chain configured ahead of time is interchangeable with one built
incrementally on the SoC.

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
