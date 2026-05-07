PeakRDL-pybind11 Documentation
==============================

.. image:: https://img.shields.io/badge/license-GPL--3.0-blue
   :target: https://github.com/arnavsacheti/PeakRDL-pybind11/blob/main/LICENSE
   :alt: License

PeakRDL-pybind11 generates Python bindings from SystemRDL register
descriptions so a Python-fluent person who is *not* a hardware engineer can
stay productive on hardware.

**One-line goal:** make ``dir(soc)`` and a docstring enough to drive the chip
from the REPL or a notebook.

Who this is for
---------------

Five user roles drive the design. Anything that helps them is a feature.

.. list-table::
   :header-rows: 1
   :widths: 18 42 40

   * - Role
     - What they do most
     - What hurts most today
   * - Bring-up engineer
     - Poke registers from a REPL, verify hardware came up.
     - "Does this register need RMW or write?"
   * - Test author
     - Write directed/random tests, wait for events.
     - Polling boilerplate, magic constants.
   * - FW dev
     - Mirror the C driver's behavior in Python for co-simulation.
     - Mismatched semantics with ``mmio_*`` helpers.
   * - Lab automator
     - Long-running scripts in Jupyter, log + replay sessions.
     - No transcript, no diffing, weak notebook UX.
   * - Debugger
     - Attached over JTAG/SWD, want a state dump and snapshot.
     - Each tool has its own register browser.

Mental model
------------

A generated module is a typed tree of nodes that mirrors the RDL hierarchy.
Nodes are descriptors; values come from reads. Reads return typed wrappers
(``RegisterValue``, ``FieldValue``) that behave like ints but carry decode
info.

Four primitive ops cover the read/write surface:

.. code-block:: python

   reg.read()              # one bus read  -> RegisterValue
   reg.write(value)        # one bus write, no read
   reg.modify(**fields)    # one read + one write (RMW)
   reg.poke(value)         # explicit "I know what I'm doing", same as write

See :doc:`concepts/values_and_io` and :doc:`concepts/side_effects` for the
full surface, including ``peek()``, ``clear()``, ``set()``, ``pulse()``, and
``acknowledge()`` for side-effecting registers.

.. note::
   These docs describe the **target API**. The sketch is the source of truth;
   the implementation catches up to it. Where the current code differs, treat
   the sketch as authoritative.

Design Sketch
-------------

The full design sketch -- audience, principles, and every concept page below
in one place -- is available as a single document.

* :download:`IDEAL_API_SKETCH.md <IDEAL_API_SKETCH.md>` -- the design sketch in full.

.. toctree::
   :maxdepth: 2
   :caption: Getting started

   installation
   usage
   feature_matrix

.. toctree::
   :maxdepth: 1
   :caption: Concepts

   concepts/hierarchy
   concepts/values_and_io
   concepts/widgets
   concepts/memory
   concepts/arrays
   concepts/enums_flags
   concepts/interrupts
   concepts/aliases
   concepts/side_effects
   concepts/specialized
   concepts/bus_layer
   concepts/wait_poll
   concepts/snapshots
   concepts/observers
   concepts/errors
   concepts/cli_repl

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api/int_types
   api/exporter
   api/masters

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
