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
typed exception, importable from :mod:`peakrdl_pybind11.errors` (with the
single exception of ``ReplayMismatchError``; see "Where to import from"
below).

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
   * - ``wait_for``/``poll`` exceeded its deadline
     - ``WaitTimeoutError(path, expected, last_seen, timeout, polls)``
   * - ``RegisterValue`` / ``Snapshot`` used after ``soc.reload()``
     - ``StaleHandleError(path, "handle for ... is stale; SoC was reloaded")``
   * - ``ReplayMaster`` (strict) saw a transaction the recording did not
     - ``ReplayMismatchError(expected, actual)``

``AccessError``, ``SideEffectError``, ``NotSupportedError``, ``BusError``,
``RoutingError``, ``WaitTimeoutError``, and ``StaleHandleError`` are all
defined by ``peakrdl_pybind11``. ``ReplayMismatchError`` lives with the
recording/replay master implementation in
``peakrdl_pybind11.masters.recording_replay``; it signals a **logical**
mismatch between requested and recorded traffic, not a transport failure,
which is why it is intentionally distinct from ``BusError``. ``ValueError``
and ``AttributeError`` are reused from the standard library because user
code already catches those for the same reasons (out-of-range argument,
unknown attribute), and consistency with built-in semantics beats inventing
new types.

Where to import from
--------------------

The canonical, top-level import path for the whole taxonomy is
``peakrdl_pybind11.errors``:

.. code-block:: python

   from peakrdl_pybind11.errors import (
       AccessError,
       BusError,
       NotSupportedError,
       PeakRDLError,
       RoutingError,
       SideEffectError,
       StaleHandleError,
       WaitTimeoutError,
   )

The runtime taxonomy at ``peakrdl_pybind11.runtime.errors`` is the older
internal home and stays around so generated runtime code keeps importing
from a single place; ``peakrdl_pybind11.errors`` re-exports from it. Prefer
the top-level path in user code — it is the surface this documentation
guarantees and the one that gets the ``PeakRDLError`` base class. The
``ReplayMismatchError`` is the only exception that does not live in the
``errors`` module: import it from
``peakrdl_pybind11.masters.recording_replay`` alongside the master itself.

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

   from peakrdl_pybind11.errors import BusError

   try:
       value = soc.uart.status.read()
   except BusError as e:
       log.warning("transient bus failure on %s after %d retries: %s",
                   e.path, e.retries, e.underlying)
       reconnect()

Two ``WaitTimeoutError`` classes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

There are intentionally **two** ``WaitTimeoutError`` classes in the source
tree, and they are not instance-compatible:

* ``peakrdl_pybind11.errors.WaitTimeoutError`` — top-level, subclasses
  both ``PeakRDLError`` and the builtin ``TimeoutError``. Carries
  ``path``, ``expected``, ``last_seen``, ``samples``, ``timeout``, and
  ``polls`` (all keyword-only after ``path``). This is the one user code
  should reference.
* ``peakrdl_pybind11.runtime.errors.WaitTimeoutError`` — the older runtime
  taxonomy class, subclasses ``TimeoutError`` only. Its constructor is
  positional (``node_path, expected, last_seen, samples=None``) and it
  does not subclass ``PeakRDLError``.

.. note::

   Because both classes inherit from the builtin ``TimeoutError``, the
   simplest catch — ``except TimeoutError`` — works regardless of which
   one was raised. Users who want a single PeakRDL-pybind11 catch should
   catch ``peakrdl_pybind11.errors.PeakRDLError`` (which the top-level
   class subclasses) rather than trying to ``isinstance``-check across
   the two classes; an instance of one is **not** an instance of the
   other.

   .. code-block:: python

      from peakrdl_pybind11.errors import PeakRDLError

      try:
          soc.uart.status.tx_ready.wait_for(True, timeout=1.0)
      except TimeoutError as e:
          # Catches either WaitTimeoutError class.
          ...
      except PeakRDLError as e:
          # Catches the top-level WaitTimeoutError plus every other typed
          # error this library raises.
          ...

``StaleHandleError`` and ``soc.reload()``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``soc.reload()`` re-imports the generated module after a recompile and
swaps the SoC tree in place. Any ``RegisterValue`` or ``Snapshot`` handle
captured before the reload is now backed by an obsolete tree, so the
runtime invalidates them: a stale handle raises ``StaleHandleError`` on
next use. Re-fetch the value from the live tree after a reload.

.. code-block:: python

   from peakrdl_pybind11.errors import StaleHandleError

   snapshot = soc.uart.read_all()
   soc.reload()
   try:
       print(snapshot["status.tx_ready"])
   except StaleHandleError:
       snapshot = soc.uart.read_all()  # re-capture against the new tree
       print(snapshot["status.tx_ready"])

See :doc:`/snapshots` for the handle lifecycle and the reload contract
that drives this invalidation.

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
* :doc:`/wait_poll` — ``wait_for``/``poll`` semantics and the
  ``WaitTimeoutError`` payload.
* :doc:`/snapshots` — handle invalidation on ``soc.reload()`` and the
  ``StaleHandleError`` it raises.
