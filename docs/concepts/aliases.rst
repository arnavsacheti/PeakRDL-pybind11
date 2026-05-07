Linked and Aliased Registers
============================

.. note::

   This page is **aspirational**. It describes the API surface defined by the
   ideal-API sketch (§10). Some of the surfaces below are not yet implemented
   in the shipped exporter — the sketch is the source of truth, and the code
   is catching up.

Overview
--------

SystemRDL ``alias`` lets multiple register definitions point at the same
address. The Python API treats one definition as **canonical** and the rest
as **views** onto it. Reads and writes through any view land at the same
underlying address; what differs is the field set and access policy each
view exposes.

This pattern shows up most often around scrambled or secured registers
(read-only mirrors, partial masks, hardware-only views), and around
firmware-friendly aliases that re-expose a hardware-controlled register
under a software-only name.

Surface
-------

Aliases relate to their target through three attributes — ``.target`` on
the alias, ``.aliases`` on the primary, and the boolean ``.is_alias`` on
either side:

.. code-block:: python

   soc.uart.control                 # primary
   soc.uart.control_alt             # alias
   soc.uart.control_alt.target      # → soc.uart.control
   soc.uart.control.aliases         # (soc.uart.control_alt,)
   soc.uart.control_alt.is_alias    # True

``.target`` is the canonical node. ``.aliases`` is the tuple of all
aliases pointing at this node (empty on leaf primaries). ``.is_alias``
returns ``True`` for any node that is *not* the canonical definition;
the primary always reports ``False``.

``repr`` surfaces the relationship
----------------------------------

The compact ``repr`` for an alias names its target inline so users
landing in a REPL can see the link without consulting the RDL source:

.. code-block:: text

   >>> soc.uart.control_alt
   <Reg uart.control_alt @ 0x40001000  alias-of=uart.control  rw>

Reads and writes through ``soc.uart.control_alt`` reach the same address
as ``soc.uart.control``, but the field set and access policy may differ —
that's the whole point of having a separate view.

Alias kinds
-----------

The exporter classifies each alias relationship and exposes it on
``info.alias_kind``. The kind comes from the RDL ``alias`` declaration
or from a UDP that refines it:

.. code-block:: python

   soc.uart.control_alt.info.alias_kind   # AliasKind.FULL

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - Kind
     - Meaning
   * - ``full``
     - Both views see every field. The alias is a pure rename — a
       firmware-friendly handle for the same register.
   * - ``sw_view``
     - Software-side projection. The alias exposes only the fields a
       driver should touch; hardware-only fields are hidden.
   * - ``hw_view``
     - Hardware-side projection. The alias exposes only the fields a
       hardware block updates; the software-only mirror lives elsewhere.
   * - ``scrambled``
     - Read-only mirror of a secured register, often with a partial mask
       so sensitive fields read as zero. Writes through the scrambled
       view typically raise.

Branching on ``alias_kind`` lets snapshot, replay, and verification code
react to the relationship without hard-coding register names.

See also
--------

- :doc:`hierarchy` — how nodes (including aliases) are reached and
  inspected via the canonical attribute tree.
- :doc:`side_effects` — read-clear, write-one-clear, and other rules
  the alias view inherits from its target.
