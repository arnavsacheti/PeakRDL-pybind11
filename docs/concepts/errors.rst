Error Model
===========

PeakRDL-pybind11 raises a small set of typed exceptions so that scripts and
tests can react to specific failure modes without parsing error text. Two
properties hold for every exception this library raises:

* The message includes the **path** of the offending node (``uart[0].status.tx_ready``)
  and its **absolute address** (``0x4000_1004``). You should never have to
  look up "which register was that?" from a stack trace alone.
* Stack traces are short. The API uses ``__tracebackhide__``-like patterns to
  skip its own frames, so the user-code frame that issued the bad call sits at
  the top of the traceback.

The error table
---------------

The full taxonomy is small enough to fit on one page. Every row below is a
typed exception, importable from :mod:`peakrdl_pybind11`.

.. list-table::
   :header-rows: 1
   :widths: 45 55

   * - Situation
     - Result
   * - Write to read-only field
     - ``AccessError("uart.status.tx_ready is sw=r")``
   * - Read from write-only field
     - ``AccessError``
   * - Out-of-range value for field
     - ``ValueError`` with bit width and valid range
   * - Unknown field name in ``modify(...)``
     - ``AttributeError`` with did-you-mean suggestion
   * - ``read()`` of an ``rclr`` field inside ``no_side_effects()``
     - ``SideEffectError``
   * - ``peek()`` of a field on a master that can't peek
     - ``NotSupportedError("master cannot peek rclr")``
   * - Bus error
     - ``BusError(addr, op, master, retries, underlying)``
   * - Address routing miss
     - ``RoutingError(addr, "no master attached for ...")``

``AccessError``, ``SideEffectError``, ``NotSupportedError``, ``BusError``, and
``RoutingError`` are all defined by ``peakrdl_pybind11``. ``ValueError`` and
``AttributeError`` are reused from the standard library because user code
already catches those for the same reasons (out-of-range argument, unknown
attribute), and consistency with built-in semantics beats inventing new types.

Catching errors
---------------

Most user code only needs to catch ``BusError`` (transient hardware/bus
trouble) and ``AttributeError`` (typos). The rest indicate programming errors
that should fail loudly.

.. code-block:: python

   try:
       soc.uart.control.modify(enbale=1)  # typo
   except AttributeError as e:
       print(e)
       # "no field 'enbale' on uart.control; did you mean 'enable'?"

The did-you-mean suggestion is built from the register's actual field list, so
it works even for fields whose names are not legal Python identifiers (where
they live under ``info.fields`` and are addressed by string).

For programmatic recovery — e.g., a flaky connection — catch the specific
type:

.. code-block:: python

   from peakrdl_pybind11 import BusError

   try:
       value = soc.uart.status.read()
   except BusError as e:
       log.warning("transient bus failure on %s after %d retries: %s",
                   e.path, e.retries, e.underlying)
       reconnect()

``BusError`` detail
-------------------

When the master gives up after exhausting its retry budget, the resulting
``BusError`` carries everything a CI run needs to triage the failure:

* The failed transaction (``addr``, ``op`` ∈ ``{"read", "write"}``).
* The master that issued it (so you know which backend died).
* The retry count actually attempted before giving up.
* The underlying exception raised by the master implementation
  (``socket.timeout``, ``OpenOCDError``, etc.) chained via ``__cause__``.

This means a top-level ``except BusError`` in a test runner can surface the
register path, the underlying transport error, and how aggressively the master
already retried — without re-wrapping the master. Retry tuning lives on the
master itself; see :doc:`/bus_layer` for the policy knobs that decide when a
transient error becomes a ``BusError``.

See also
--------

* :doc:`/bus_layer` — retry policy and how a transient bus error becomes a
  ``BusError``.
* :doc:`/side_effects` — what ``no_side_effects()`` blocks and why
  ``SideEffectError`` exists.
* :doc:`/values_and_io` — the ``AttributeError`` raised on bare attribute
  writes outside a context manager.
