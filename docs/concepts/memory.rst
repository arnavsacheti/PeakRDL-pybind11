Memory Regions
==============

SystemRDL ``mem`` declarations describe a region of *words* with no fields:
just storage, addressed linearly. The Python view models a memory region as a
sliceable, NumPy-aware buffer. Instead of forcing users to think in raw byte
offsets, the API exposes both a high-level numpy-idiomatic surface and the
lower-level byte and burst escape hatches that lab automation and bring-up
work demand.

This page is the conceptual reference for memory regions. See
:doc:`/bus_layer` for routing and barrier semantics, and :doc:`/snapshots`
for snapshotting and diffing memory state.

Overview
--------

A SystemRDL ``mem`` is a region with no fields - the unit of access is the
word. The Python wrapper for a ``mem`` node behaves like a typed buffer:

- It is **sliceable** with the usual Python idioms (``mem[i]``,
  ``mem[i:j]``, ``mem[:]``).
- It is **NumPy-aware** - it implements the buffer protocol and
  ``__array__``, so ``np.asarray(mem)`` returns a live ndarray view.
- It is **bus-bound** - the wrapper itself is a descriptor; values are
  produced by reads and accepted by writes.

The model is intentionally close to ``numpy.memmap``: indexing semantics are
familiar, and slices are *views*, not copies, so they stay live with the
underlying hardware.

Metadata
--------

Every memory node exposes a small set of geometry attributes derived from the
RDL declaration:

.. code-block:: python

   mem.size_bytes        # total region size in bytes, e.g. 0x4000
   mem.depth             # number of word entries, e.g. 0x1000
   mem.word_width        # word width in bits, e.g. 32
   mem.base_address      # absolute address of word 0

These are constants for a given build of the bindings; they do not trigger
bus traffic.

Element access
--------------

Indexing with an integer gets or sets a single word. The index is a *word
index*, not a byte address - this matches RDL semantics and avoids confusion
when ``word_width`` is not 32 bits.

.. code-block:: python

   mem[10]                       # one bus read
   mem[10] = 0xDEADBEEF           # one bus write

Slicing returns a live view
---------------------------

Slicing returns a ``MemView`` - an ndarray-like wrapper that stays bound to
the underlying memory region. Slicing does **not** snapshot the contents; it
hands back a live window onto the bus. This mirrors how ``numpy.memmap``
slices behave, and it makes large memories cheap to subscript.

To take a snapshot - one burst, one allocation - call ``.copy()`` or its
read-side alias ``.read()``. Bulk writes are always coalesced into a single
burst when the master supports it.

.. code-block:: python

   mem[10:20]            # MemView, ndarray-like, live
   mem[10:20].copy()      # one-burst snapshot
   mem[10:20].read()      # alias for .copy()
   mem[10:20] = [...]     # one-burst bulk write

.. warning::

   Writing tight per-element loops on a ``MemView`` is one bus transaction
   per access. A loop like ``for i in range(1024): total += view[i]`` will
   issue 1024 reads. For tight loops, take a snapshot with ``.copy()`` /
   ``.read()``, use a buffered ``mem.window(...)``, or assign in bulk with
   ``view[:] = arr`` so the API can coalesce the traffic.

Byte-level escape hatch
-----------------------

For protocols that genuinely think in bytes - firmware images, packet
buffers, opaque blobs - the byte-addressed accessors sidestep the word
abstraction:

.. code-block:: python

   mem.read_bytes(offset=0, n=64)
   mem.write_bytes(offset=0, data=b"\xde\xad\xbe\xef")

Both take a byte ``offset`` from the region's base. ``read_bytes`` returns a
``bytes`` object; ``write_bytes`` accepts any bytes-like object whose length
is a multiple of the underlying access width.

NumPy interop
-------------

NumPy is a hard runtime dependency of the generated bindings. Every memory
node implements the buffer protocol and ``__array__``, so the standard NumPy
entry points work directly:

.. code-block:: python

   import numpy as np

   arr = np.asarray(mem)                  # live ndarray view
   np.copyto(arr[0:256], pattern)          # bulk write, one burst
   checksum = np.bitwise_xor.reduce(arr[0:1024])

For explicit zero-copy bulk transfer into a caller-owned buffer, use
``read_into`` and ``write_from``:

.. code-block:: python

   buf = np.empty(1024, dtype=np.uint32)
   mem.read_into(buf, offset=0)            # one burst, one fill
   mem.write_from(buf, offset=0)           # one burst write

These are the lowest-overhead path for high-throughput dump and restore work
because they avoid the intermediate allocation that ``.copy()`` performs.

Mapped windows
--------------

For high-frequency access patterns where many small ops touch a bounded
region, ``mem.window(...)`` returns a context manager whose body operates on
a buffered, in-memory mirror. Reads see the buffered copy; writes are
batched and **flushed on exit**.

.. code-block:: python

   with mem.window(offset=0, length=256) as w:
       for i in range(256):
           w[i] = i
   # On exit: a single bulk write back to the bus.

This is the right tool when the access pattern is too irregular for a single
slice assignment but too hot to pay the per-element bus cost on a live view.

Streaming
---------

Large memories - frame buffers, on-chip RAM, traces - cannot always be
materialized into one ndarray. ``mem.iter_chunks(...)`` yields successive
chunks of a configurable size in words, suitable for piping through a
processing pipeline:

.. code-block:: python

   for chunk in mem.iter_chunks(size=4096):
       process(chunk)

Each ``chunk`` is an ndarray (a snapshot, not a live view), and the iterator
streams in burst-sized reads.

Bursts
------

Masters declare a *burst* capability at attach time. The API uses bursts
when the underlying master supports them and falls back to per-word loops
otherwise. The fallback is transparent to user code, but it is not
transparent to bus traffic.

To verify what actually went out, every read result carries a ``meta``
record:

.. code-block:: python

   result = mem.read(...)
   result.meta.transactions   # actual number of bus transactions

For a burst-capable master a 1024-word read should report a small number of
transactions, not 1024. This is the recommended way to confirm the burst
fast-path is engaged.

See also
--------

- :doc:`/bus_layer` - master attachment, routing, and barrier semantics that
  govern when memory writes drain to the device.
- :doc:`/snapshots` - capturing, diffing, and restoring memory state across
  test runs.
