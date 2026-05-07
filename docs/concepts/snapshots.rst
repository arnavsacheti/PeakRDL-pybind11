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

JSON output is keyed by dotted path with hex string values and explicit
``access`` and ``reset`` metadata; ``SocSnapshot.from_json`` reattaches the
snapshot to the matching SoC tree at load time so ``restore`` knows which
addresses to write.

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
