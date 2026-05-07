Register and Field Values
=========================

PeakRDL-pybind11 returns typed value wrappers from ``read()`` calls — not bare
``int`` objects. ``RegisterValue`` carries the whole-register decode along with
field metadata. ``FieldValue`` is a single-field value, decoded as an
``IntEnum`` member or ``bool`` where the RDL says it should be.

Overview
--------

A node (``Reg`` or ``Field``) is a *descriptor*. It produces a *value* by being
read. Values are int-compatible wrappers that print well, compare to enums,
and round-trip cleanly back into writes:

* ``RegisterValue`` — returned by ``reg.read()``. Holds the whole register
  word plus per-field decode information. Field access by attribute or by
  ``__getitem__`` returns ``FieldValue`` instances.
* ``FieldValue`` — returned by ``field.read()``. A single field, narrowed to
  the field's bit range. When the field has ``encode = SomeEnum``, the value
  is the matching ``IntEnum`` member; for a 1-bit field, it is ``bool``-like.

Both behave like ``int`` for arithmetic and comparison, so existing code that
expects integers keeps working. The richer surface — formatting, decode,
field access, replacement — is layered on top.

.. code-block:: python

   v = soc.uart.control.read()
   print(v)
   # UartControl(0x00000022)
   #   enable[0]    = 1
   #   baudrate[3:1]= BaudRate.BAUD_19200  (1)
   #   parity[5:4]  = Parity.NONE          (0)

Immutability and hashability
----------------------------

``RegisterValue`` and ``FieldValue`` are **immutable and hashable**. Once a
read returns a value, that value cannot mutate — there is no attribute
assignment, no in-place op, no aliasing surprise. Both are safe to use as
``dict`` keys or ``set`` members, which makes them suitable for snapshots,
coverage maps, and golden-state comparisons.

Mutation is performed by *replacement*. ``v.replace(**fields)`` returns a new
``RegisterValue`` with the named fields overwritten and every other field
left untouched:

.. code-block:: python

   v  = soc.uart.control.read()       # RegisterValue(0x22)
   v2 = v.replace(enable=0)           # new RegisterValue(0x20); v unchanged
   v == v2                            # False; both still hashable

   seen = {}
   seen[v] = "before"                 # immutable & hashable → safe dict key
   seen[v2] = "after"

The cost is one allocation per read. The benefit is that no stale handle ever
silently changes underneath you.

Pickle and JSON
---------------

Both value types are picklable and JSON-serializable. They round-trip across
process boundaries — useful for distributed test harnesses, CI artefacts, and
``snapshot()`` blobs that need to survive a worker restart.

.. code-block:: python

   import pickle, json

   v = soc.uart.control.read()

   # Pickle round-trip
   blob = pickle.dumps(v)
   v2   = pickle.loads(blob)
   assert v == v2

   # JSON round-trip
   payload = json.dumps(v.to_json())
   v3      = RegisterValue.from_json(json.loads(payload))
   assert v == v3

The JSON form preserves the integer value and enough metadata to reconstruct
the decoded view (field names, widths, encodings).

Format helpers
--------------

Users always want to format a register value four different ways. The helpers
are short, predictable, and live on the value itself:

.. code-block:: python

   v.hex()                       # "0x00000022"
   v.hex(group=4)                # "0x0000_0022"
   v.bin()                       # "0b00000000_00000000_00000000_00100010"
   v.bin(group=8, fields=True)   # annotates groups with field boundaries
   print(reg, fmt="bin")          # alt-format on the live read
   soc.uart.control.read().table()   # ASCII table of fields, ready for logs

``v.table()`` is what you paste into a bug report — a compact, monospaced
field-by-field view that survives copy-paste.

Field access on RegisterValue
-----------------------------

A ``RegisterValue`` exposes its fields by attribute and by name:

.. code-block:: python

   v = soc.uart.control.read()

   v == 0x22                     # True — RegisterValue is int-compatible
   v.enable                      # 1 (FieldValue, 1-bit → bool-like)
   v["enable"]                   # same as above, by name
   v.baudrate                    # <BaudRate.BAUD_19200: 1>
   v.replace(enable=0)           # → new RegisterValue with enable=0

Comparisons against bare ints, against enum members, and against other
``RegisterValue`` instances all do the right thing. Field access never hits
the bus — the read already happened, and ``RegisterValue`` is just the
decoded snapshot.

Round-trip back to write
------------------------

Because ``RegisterValue`` is int-compatible and carries the decoded fields,
it round-trips into ``write()`` without ceremony:

.. code-block:: python

   v = soc.uart.control.read()
   # ... some logic ...
   soc.uart.control.write(v)              # raw write of the same word
   soc.uart.control.write(v.replace(enable=0))   # write the replaced value

Pair this with ``modify(**fields)`` (the canonical RMW) for cases where you
want a single read-modify-write rather than a separate read and write.

Legacy compatibility
--------------------

The names ``RegisterInt`` and ``FieldInt`` are the **legacy** spellings of
these types. ``RegisterValue`` and ``FieldValue`` are the canonical names
going forward. New code should use the canonical names; the legacy names
remain importable for backward compatibility but are not documented here —
their reference lives elsewhere in the API documentation.

Reference
---------

.. autoclass:: peakrdl_pybind11.RegisterValue
   :members:
   :inherited-members:
   :special-members: __new__

.. autoclass:: peakrdl_pybind11.FieldValue
   :members:
   :inherited-members:
   :special-members: __new__
