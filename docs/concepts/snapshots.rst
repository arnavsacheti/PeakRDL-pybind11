Snapshots, Diff, Save & Restore
===============================

.. note::

   This page describes the *aspirational* snapshot surface for capturing,
   comparing, and restoring SoC state. The behavior here mirrors the
   source-of-truth sketch (§15) and is the target shape of the API; current
   releases may implement only a subset of these features.

Overview
--------

A snapshot is a captured image of every readable register, field, and memory
region under a node, taken at a single instant. ``soc.snapshot()`` returns a
``SocSnapshot`` that exposes the captured state two ways:

- a flat dict keyed by dotted path (``"uart.control"``, ``"uart.status.tx_ready"``,
  ``"ram[0x40..0x60]"``) — the right shape for grep-style assertions in tests
  and for stable JSON output, and
- a **structured view** that mirrors the SoC hierarchy, so ``snap.uart`` is a
  subtree snapshot you can inspect, diff, and restore on its own.

Snapshots are immutable, hashable, picklable, and JSON-serializable, which
makes them safe to pass between processes, attach to bug reports, and compare
without worrying about aliasing or accidental mutation.

Attaching to a hand-built SoC
-----------------------------

The generated ``create()`` factory wires ``soc.snapshot()`` and
``soc.restore()`` onto the returned soc instance automatically (it calls
``register_post_create`` under the hood). If you build your own SoC harness
— a mock, a unit-test stub, a hand-rolled adapter — you can attach the same
methods explicitly with one call:

.. code-block:: python

   from peakrdl_pybind11.runtime.snapshot import attach_snapshot

   my_soc = MyHandBuiltSoc(...)        # not produced by create()
   attach_snapshot(my_soc)             # binds .snapshot() and .restore()

   snap = my_soc.snapshot()
   my_soc.restore(snap)

``attach_snapshot`` is idempotent (calling it twice rebinds harmlessly) and
returns the same ``soc`` so it composes with other ``attach_*`` helpers.
Test mocks only need to expose ``walk()`` (or ``iter_readable()``) and the
per-node ``peek()`` / ``read()`` / ``write()`` surface — anything that walks
like a soc snapshots like a soc.

Capturing
---------

The headline call captures the entire SoC. The structured view lets you focus
on a peripheral after the fact, and individual subtrees can be snapshotted
directly when capturing the whole device is overkill.

.. code-block:: python

   snap1 = soc.snapshot()                # SocSnapshot — flat dict + structured view
   snap1.uart                            # subtree view of the uart peripheral

   # Capture only a subtree from the start (cheaper if that's all you need)
   uart_snap = soc.uart.snapshot()       # only the uart subtree

Both forms produce ``SocSnapshot`` objects. A subtree snapshot diffs and
restores against the same node it was captured from; passing it to a different
peripheral raises rather than silently writing the wrong addresses.

Diff
----

``snap2.diff(snap1)`` returns a diff object that lists every register, field,
or memory region whose value changed between the two captures. Pretty-printed,
it looks like:

.. code-block:: python

   snap1 = soc.snapshot()
   do_thing()
   snap2 = soc.snapshot()
   print(snap2.diff(snap1))

.. code-block:: text

   3 differences
     uart.control            0x00000000 → 0x00000022
     uart.status.tx_ready    0          → 1
     ram[0x40..0x60]         <changed>

Diffs are sorted by path so the output is deterministic and copy-paste
friendly in CI logs. Memory regions show ``<changed>`` for ranges large enough
that an inline byte-by-byte rendering would dominate the log; expand the diff
in a notebook to see the highlighted bytes (see `Notebook rendering`_).

Restore
-------

A snapshot can be written back to the device. ``dry_run=True`` is the
preview: it walks the snapshot, computes which registers and memory ranges
would be touched, and reports them without issuing any bus writes.

.. code-block:: python

   soc.restore(snap1, dry_run=True)      # show what would change, no writes
   soc.restore(snap1)                    # write back

Restore respects access modes. Read-only fields and registers are skipped
silently; write-only fields are written from the captured "intended" value
recorded at capture time. A subtree snapshot restored against the matching
subtree (``soc.uart.restore(uart_snap)``) only writes that peripheral.

A subtree view of a whole-soc snapshot also restores cleanly against the
parent soc — paths from the subtree view are re-absolutized at restore time,
so the writes still land at the right addresses:

.. code-block:: python

   snap = soc.snapshot()                 # whole-device capture
   soc.restore(snap.uart)                # restores only the uart paths

   # Equivalent, captured directly as a subtree:
   uart_snap = soc.uart.snapshot()
   soc.restore(uart_snap)                # also routes correctly

This means you can keep one big snapshot around and replay just one
peripheral from it without re-capturing.

Serialization
-------------

Snapshots round-trip through JSON for human-readable artefacts and through
``pickle`` for distributed and multi-process tests.

.. code-block:: python

   # JSON — human-readable, stable, easy to attach to a bug or commit
   snap = soc.uart.snapshot()
   snap.to_json("uart-state.json")
   snap2 = SocSnapshot.from_json("uart-state.json")

   # Pickle — round-trips for distributed / multi-process tests
   import pickle
   data = pickle.dumps(snap)
   restored = pickle.loads(data)

JSON output is keyed by dotted path with explicit ``access`` and ``reset``
metadata; ``SocSnapshot.from_json`` reattaches the snapshot to the matching
SoC tree at load time so ``restore`` knows which addresses to write.

JSON file format
~~~~~~~~~~~~~~~~

``Snapshot.to_json(path)`` writes a stable, hand-authorable file. The
top-level object is a flat dict with three keys: ``"version"`` (currently
``1``), ``"values"``, and ``"metadata"``. ``values`` is a flat
``{absolute-dotted-path: integer}`` mapping; integers are written as decimal
JSON numbers (not hex strings), so the file diffs cleanly under git and
``json.load`` round-trips without custom parsing. ``metadata`` mirrors the
same key set and carries a JSON-friendly subset of each node's
``Info`` — ``name``, ``path``, ``address``, ``offset``, ``regwidth``,
``access``, ``reset``, ``on_read``, ``on_write`` — limited to fields that
``json.dumps`` accepts; missing or non-serializable attributes are dropped
silently. ``metadata`` is purely descriptive: ``Snapshot.from_json`` and
``restore`` work even if ``metadata`` is empty (``{}``), which means you
can build a snapshot file by hand or from a script with no SoC introspection.

The schema is small enough to write directly:

.. code-block:: json

   {
     "version": 1,
     "values": {
       "uart.control": 34,
       "uart.status.tx_ready": 1
     },
     "metadata": {
       "uart.control": {
         "path": "uart.control",
         "address": 1024,
         "regwidth": 32,
         "access": "rw",
         "reset": 0
       },
       "uart.status.tx_ready": {
         "path": "uart.status.tx_ready",
         "access": "r",
         "reset": 0
       }
     }
   }

Keys in ``values`` must be absolute paths (``"uart.control"``, not
``"control"``); ``restore`` resolves them through the soc's path index.
Paths that don't exist in the target soc are skipped silently — an
authored file can target a subset of the device, and a captured file
loaded against an older firmware ignores paths the device no longer
exposes. Snapshots authored without ``metadata`` round-trip through
``from_json`` and restore correctly; the diff/notebook renderers degrade
gracefully when access info is missing.

Hashing & dict-key use
----------------------

``Snapshot`` is hashable: any snapshot can be used as a dict key or a set
member, which is the right shape for golden-state checks where the test
fixture is "the device must end in *one of these* known good states":

.. code-block:: python

   golden_idle  = soc.snapshot()
   reset(soc); arm(soc)
   golden_armed = soc.snapshot()

   GOLDEN = {golden_idle: "idle", golden_armed: "armed"}

   actual = soc.snapshot()
   assert actual in GOLDEN, f"unexpected state: {actual.diff(golden_idle)}"

Equality and hashing key off ``values`` only — captured metadata is
descriptive and is deliberately ignored, so two snapshots with identical
paths and integer values compare equal and hash to the same bucket even if
they were captured from different soc revisions. That makes
``set(snaps)`` and ``Counter(snaps)`` cheap deduplication primitives in
soak tests.

Side-effect safety
------------------

By default ``snapshot()`` uses ``peek()`` to capture each readable node, which
means a register tagged ``rclr`` (read-clear) or otherwise destructive on read
is **not** silently consumed. If any required read would be destructive, the
call aborts with a clear error before touching the bus.

.. code-block:: python

   soc.snapshot()                         # safe: peek() throughout, aborts on rclr

   # Opt-in override when you really do want a destructive capture
   soc.snapshot(allow_destructive=True)

The override is opt-in for the same reason ``watch()`` requires it: a snapshot
that quietly clears the very state you are trying to record is worse than a
loud failure.

Notebook rendering
------------------

A ``SocSnapshot`` is a renderable node. ``snap2.diff(snap1)`` renders as a
side-by-side HTML table in Jupyter, with changed cells highlighted, added or
removed paths shown explicitly, and a filter row for restricting the view by
node kind or access mode.

.. code-block:: python

   snap1 = soc.snapshot()
   do_thing()
   snap2 = soc.snapshot()

   snap2.diff(snap1)                     # → side-by-side HTML table in a notebook

The same diff prints as the deterministic text table shown above when the
result is sent to a plain terminal or a CI log. See :doc:`/concepts/widgets`
for the rich-display surface and the ``watch()`` integration with snapshots.

See also
--------

- :doc:`/concepts/widgets` — rich-display rendering for snapshots, diffs,
  and live monitors.
- :doc:`/concepts/bus_layer` — the bus / master layer that backs snapshot
  reads and supports record-and-replay.
- :doc:`/concepts/observers` — observation hooks and audit logs for tracking
  every read and write that built a snapshot.
