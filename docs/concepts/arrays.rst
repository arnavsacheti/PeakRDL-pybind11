Arrays
======

.. note::

   This page describes the **target API** for register, regfile, and field
   arrays. It is aspirational and tracks the design sketch; not every behavior
   is wired up in the current release.

Overview
--------

SystemRDL arrays apply to ``reg``, ``regfile``, ``field``, and ``addrmap``
nodes. The Python model treats every array as a *typed* ``Sequence``: it has
a length, supports integer and slice indexing, iterates lazily, and
participates in ``in`` membership tests. Arrays are bus-bound — every element
access remains a real handle that issues bus transactions when read or
written.

Multi-dimensional RDL arrays (``reg my_reg[4][16]``) are exposed both as
chained subscripts (``my_reg[2][5]``) and as native tuple index
(``my_reg[2, 5]``), with a NumPy-style ``.shape`` attribute reporting the
dimensions.

Single dimension
----------------

A 1-D array is the common case: ``soc.dma.channel`` below is a
``ChannelArray`` of length 8.

.. code-block:: python

   soc.dma.channel               # ChannelArray, len=8
   soc.dma.channel[3]            # one channel
   soc.dma.channel[-1]           # last
   soc.dma.channel[2:6]          # ChannelSlice (still bus-bound)
   list(soc.dma.channel)         # iterate
   3 in soc.dma.channel          # by index

Negative indices count from the end, slicing yields a ``ChannelSlice`` that
remains bus-bound (no values are read until you ask), and iteration produces
each element in order.

Multi-dimension
---------------

For multi-dimensional RDL arrays, both tuple index and chained subscripts
are supported. The ``.shape`` attribute reports the array's dimensions as a
tuple, mirroring ``numpy.ndarray.shape``.

.. code-block:: python

   # rdl: reg my_reg[4][16];
   soc.regblock.my_reg[2, 5]              # tuple index — natural for users
   soc.regblock.my_reg[2][5]              # also supported
   soc.regblock.my_reg.shape              # (4, 16)

Bulk reads and writes
---------------------

Indexing a register array with a slice returns an ``ArrayView``. An
``ArrayView``:

- Coalesces reads into bursts when the master supports it.
- Returns ``ndarray[uint{regwidth}]`` for a single field or single register
  array.
- Returns a structured ndarray when multiple fields are projected.

NumPy is a hard runtime dependency — bulk array reads, memory bursts, and
the buffer protocol are first-class.

.. code-block:: python

   # Read all 64 entries of a lookup table
   vals = soc.lut.entry[:].read()                # ndarray[uint32], shape (64,)

   # Read one field across the array
   enables = soc.dma.channel[:].config.enable.read()   # ndarray[bool], shape (8,)

   # Write same value to all
   soc.lut.entry[:] = 0

   # Write per-element
   soc.lut.entry[:] = np.arange(64)

   # Modify one field across the array
   soc.dma.channel[:].config.modify(enable=0)    # 8 RMWs (or 1 burst-RMW if supported)

   # Filter
   [c for c in soc.dma.channel if c.config.enable.read()]

The ``.modify(**fields)`` form on an ``ArrayView`` runs one RMW per element
by default and collapses to a single burst-RMW when the master advertises
that capability — see :doc:`/bus_layer` for details on burst negotiation
and the ``soc.batch()`` context that lets you queue many such operations.

Field arrays
------------

Field arrays are conceptually rare but supported — for instance, a register
that packs sixteen single-bit mode flags as ``mode[16]``. They expose as a
``FieldArray`` with the same indexing, slicing, and iteration semantics as
register arrays:

.. code-block:: python

   soc.gpio.mode               # FieldArray, len=16
   soc.gpio.mode[5].read()     # one bit
   soc.gpio.mode[:].read()     # ndarray[bool], shape (16,)
   soc.gpio.mode[0:8] = 0xAA   # bitmask write across the slice

See also
--------

- :doc:`/memory` — memory regions, bursts, and the NumPy buffer protocol.
- :doc:`/bus_layer` — how ``soc.batch()``, masters, and burst capability
  back the array fast paths described above.
