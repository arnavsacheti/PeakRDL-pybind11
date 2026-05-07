The Bus Layer
=============

.. note::

   This page is **aspirational**. It describes the API surface defined by the
   ideal-API sketch §13. Several of the surfaces below — composable masters
   with ``where=`` routing, transaction objects, barrier policies, read
   coalescing, retry policies, and tracing/replay — are not yet shipped in the
   exporter. The sketch is the source of truth, and the code is catching up.

Every read and write a generated SoC issues eventually crosses a bus. The
**master** is the object that owns that crossing: it serializes transactions,
handles errors, decides whether a barrier is needed, and (optionally) records
what flowed through it. PeakRDL-pybind11 makes the master a first-class,
replaceable, observable boundary of the SoC API.

This page is the canonical reference for how masters compose, route, fence,
cache, retry, trace, mock, and lock. The :doc:`/api/masters` page is the API
reference for the concrete master classes named here.

Overview
--------

The master layer is the bus binding for a generated SoC. It is:

- **Replaceable.** Any master that satisfies the ``Master`` protocol slots in.
  Production code targets ``OpenOCDMaster`` or ``SSHMaster``; tests target
  ``MockMaster``; regression replays target ``ReplayMaster``.
- **Observable.** Every transaction can be intercepted, recorded, and replayed
  byte-for-byte. ``with soc.trace() as t`` and ``RecordingMaster`` are the
  primitives.
- **Explicit.** The master is what runs the bus. Cost lives on the master:
  retries, barriers, caches, locks. Nothing in the rest of the API quietly
  amplifies bus traffic without the master being the place that decides.

Cross-references for the design pieces this page composes:

- :doc:`/api/masters` — concrete master classes (``MockMaster``,
  ``OpenOCDMaster``, ``SSHMaster``, ``SimMaster``, ``ReplayMaster``,
  ``RecordingMaster``).
- :doc:`/observers` — the hook chain that surrounds every master transaction.
- :doc:`/snapshots` — the canonical use case for trace/replay.

Composing masters
-----------------

The simplest configuration is one master for the whole address map:

.. code-block:: python

   from peakrdl_pybind11.masters import (
       MockMaster, OpenOCDMaster, SSHMaster, SimMaster,
       ReplayMaster, RecordingMaster,
   )

   soc = MySoC.create(master=OpenOCDMaster("localhost:6666"))

Real SoCs almost never have one bus, though. ``soc.attach_master`` with a
``where=`` argument registers a master to serve a subset of the tree:

.. code-block:: python

   soc = MySoC.create()
   soc.attach_master(jtag, where="peripherals.*")
   soc.attach_master(mem_master, where="ram")
   soc.attach_master(MockMaster(), where=lambda node: node.info.is_external)

The ``where=`` argument accepts three forms:

- A **glob** against the dotted RDL path (``"peripherals.*"``,
  ``"ram"``, ``"chip.cluster[*].l2"``).
- A **callable on a node**, returning ``True`` if the master should serve
  that node — useful for routing on metadata such as
  ``node.info.is_external`` or ``node.info.is_volatile``.
- An **address-range** tuple ``(lo, hi)`` for routing by physical address.

Multiple masters serve disjoint regions; the routing layer picks the right
one for every transaction. Overlap is an error: ``soc.attach_master`` raises
``RoutingError`` when two ``where=`` clauses claim the same node.

Transactions as objects
-----------------------

Reads and writes are normally implicit: ``reg.read()`` produces one read,
``reg.write(v)`` produces one write. For users who want to script the bus
directly, transactions are reified as data classes:

.. code-block:: python

   from peakrdl_pybind11 import Read, Write, Burst

   txns = [
       Read(0x4000_1000),
       Write(0x4000_1004, 0x42),
       Burst(0x4000_2000, count=128, op="read"),
   ]
   results = soc.master.execute(txns)

``execute`` returns a list of results aligned with the transaction list, with
``Write`` slots holding ``None`` and ``Read`` / ``Burst`` slots holding the
read-back values.

For staged operations that should land on the wire as one batch, use the
``soc.batch()`` context manager:

.. code-block:: python

   with soc.batch() as b:
       b.uart.control.write(1)
       b.uart.data.write(0x55)
   # All sent at exit; if the master supports queuing, this is one command.

Inside a ``batch`` block, every read and write is staged on the batch builder
rather than issued. At exit, the master receives the whole list at once and
can coalesce or pipeline as it sees fit.

Barriers and fences
-------------------

Many masters queue or coalesce writes; some buses post writes asynchronously.
A barrier forces all in-flight writes to drain before the next read.

.. code-block:: python

   soc.uart.barrier()                     # default: master(s) serving uart subtree
   soc.master.barrier()                   # explicit single-master
   soc.barrier()                          # current master(s)
   soc.barrier(scope="all")               # SoC-wide
   soc.global_barrier()                   # alias
   soc.set_barrier_policy("auto")         # default: same-master only
   soc.set_barrier_policy("none")
   soc.set_barrier_policy("strict")
   soc.set_barrier_policy("auto-global")  # paranoid

The four named policies map to:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Policy
     - Semantics
   * - ``auto`` (default)
     - Barrier before any read-after-write — same master only.
   * - ``none``
     - Opt out. Faster, but you must barrier yourself.
   * - ``strict``
     - Barrier before every read **and** every write.
   * - ``auto-global``
     - Auto-barrier extends across all masters. Slow, paranoid.

.. note::

   **Per-master is the default for a reason.** Flushing every master on
   every read-after-write is expensive when masters serve disjoint regions:
   a write to ``peripherals.uart`` does not need to drain a queued burst on
   the ``ram`` master. The ``auto-global`` policy is opt-in for the rare
   case where a read on master B genuinely depends on a write that went out
   via master A — at which point the explicit ``soc.barrier(scope="all")``
   call site usually reads better than turning the global policy on.

Read coalescing and cache policy
--------------------------------

Tight polling loops often re-read the same status register thousands of
times. A ``read_within`` policy lets the master return a cached value
without re-issuing the bus transaction:

.. code-block:: python

   soc.uart.status.cache_for(50e-3)       # 50 ms TTL
   soc.uart.status.invalidate_cache()
   with soc.cached(window=10e-3): ...

The block-scoped form is the most useful: every read inside the ``with`` block
is allowed to return a value already seen within the last 10 ms.

Cache is **refused for side-effecting reads.** The exporter declines to
attach a cache when ``info.is_volatile`` is set or ``info.on_read`` is
present, and the master ignores the cache for those registers even if it
was somehow attached. Read-clear and read-pulse semantics are not allowed
to lie. See :doc:`values_and_io` for the side-effect rules that drive this.

Bus error recovery
------------------

The master is the single place where transient bus errors are handled.
Retries, backoff, and the give-up policy all live on the master:

.. code-block:: python

   soc.master.set_retry_policy(
       retries=3,
       backoff=0.05,
       on=("timeout", "nack"),
       on_giveup="raise",
   )

   # Per-call override
   soc.uart.control.read(retries=10)

   # Global panic handler — e.g. reconnect JTAG and replay last N txns
   soc.master.on_disconnect(lambda m: m.reconnect())

When a transaction exhausts its retries, the master raises ``BusError``.
``BusError`` carries the failed transaction, the retry count, and the
underlying exception — enough for a CI run to triage why it died without
having to re-instrument.

The ``on_disconnect`` hook fires when the master loses its connection to the
target (e.g. the JTAG probe drops). Common patterns are reconnect-and-replay
the last N transactions, or escalate to a hardware reset.

Tracing and replay
------------------

Every transaction the master issues can be captured. The ``soc.trace()``
context manager builds a trace object you can inspect, save, and feed back
into a ``ReplayMaster`` for regression:

.. code-block:: python

   with soc.trace() as t:
       soc.uart.control.write(0x42)
       soc.uart.status.read()
   print(t)
   # 2 transactions, 8 bytes
   #   wr  @0x40001000  0x00000042   (uart.control)
   #   rd  @0x40001004  → 0x00000001 (uart.status)

   t.save("session.json")
   soc2 = MySoC.create(master=ReplayMaster.from_file("session.json"))

For long-running sessions, wrap the production master in a ``RecordingMaster``
to capture transactions as they happen, with no per-call instrumentation:

.. code-block:: python

   soc.attach_master(RecordingMaster(jtag, file="run.log"))

A failing CI run can then be re-run offline against the captured log via
``ReplayMaster.from_file("run.log")``. See :doc:`/snapshots` for the
record-and-replay use case in full.

Mock with hooks
---------------

The mock master is the test-driven dual of the real bus. It supports
arbitrary read/write side effects through hooks:

.. code-block:: python

   mock = MockMaster()
   mock.on_read(soc.uart.intr_status, lambda addr: 0b101)
   mock.on_write(soc.uart.data, lambda addr, val: stdout.append(val))
   mock.preload(soc.ram, np.arange(1024, dtype=np.uint32))

Hooks compose with the master's own state: ``on_read`` decides what value
this read returns; ``on_write`` runs as a side effect of the write;
``preload`` seeds memory regions in bulk.

The mock supports the same volatile / clear semantics as the real bus
(``rclr``, ``w1c``, sticky bits, hwclr counters), so test code written
against the mock is the same shape as production code. The :doc:`/observers`
hook chain stacks on top of this.

Concurrency
-----------

Masters hold a re-entrant lock by default; multi-threaded callers can issue
reads and writes without tearing up shared state. For sequences that must
not be interleaved with other threads, use the explicit lock:

.. code-block:: python

   with soc.lock():
       soc.uart.control.write(1)
       soc.uart.data.write(0x55)

For ``asyncio`` callers, the master exposes an async dual:

.. code-block:: python

   async with soc.async_session():
       await soc.uart.control.awrite(1)
       v = await soc.uart.status.aread()

``aread`` / ``awrite`` / ``amodify`` mirror the synchronous primitives on
every node and are issued through the same retry / barrier / cache machinery
described above.

Errors raised by the bus layer
------------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Exception
     - Raised when
   * - ``BusError``
     - A transaction failed after the configured retry policy gave up.
       Carries the failed transaction, retry count, and underlying exception.
   * - ``RoutingError``
     - Two ``attach_master(..., where=...)`` clauses claim the same node, or
       a transaction's address has no master configured to serve it.
   * - ``NotSupportedError``
     - The selected master cannot honour an operation (e.g. ``Burst`` on a
       master that only does single-word transactions, or ``cache_for`` on a
       side-effecting register).

See also
--------

- :doc:`/api/masters` — API reference for the concrete master classes
  (``MockMaster``, ``OpenOCDMaster``, ``SSHMaster``, ``SimMaster``,
  ``ReplayMaster``, ``RecordingMaster``).
- :doc:`/observers` — the hook chain that surrounds every master
  transaction (read pre/post, write pre/post, error).
- :doc:`/snapshots` — the canonical record-and-replay use case for
  ``RecordingMaster`` and ``ReplayMaster``.
