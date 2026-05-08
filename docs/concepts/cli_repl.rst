CLI & REPL Niceties
===================

.. note::

   **Aspirational documentation.** This page describes the target API
   defined in ``docs/IDEAL_API_SKETCH.md`` (§21 and §22.6). Some of the
   subcommands and behaviors below — ``--explore``, ``--diff``,
   ``--replay``, ``--watch``, and the ``soc.reload()`` hot-reload path —
   may not be implemented yet. The sketch is the contract we build
   toward; this page is the shape of the user experience as it should
   feel from the command line and a notebook.

Overview
--------

PeakRDL-pybind11 is more than a code generator. Once a chip is
exported, the ``peakrdl pybind11`` CLI becomes the front door for
interactive workflows — exploring the SoC at the REPL, diffing two
snapshots from a CI job, replaying a recorded bring-up session, and
hot-reloading bindings as the source RDL changes. The interactive
subcommands sit on top of the same primitives covered in
:doc:`/concepts/values_and_io` and :doc:`/concepts/snapshots`, and they
compose with the bus layer described in :doc:`/concepts/bus_layer`.

The goal is the same as the rest of the API: a Python-fluent person
who is *not* a hardware engineer should be able to drive the chip from
a REPL or a notebook with nothing more than ``dir(soc)``, a docstring,
and a one-line CLI invocation.

CLI subcommands
---------------

Beyond the core ``peakrdl pybind11 input.rdl`` exporter invocation, the
CLI exposes four interactive subcommands:

.. code-block:: bash

   peakrdl pybind11 --explore mychip       # spawn a REPL with `soc` already created
   peakrdl pybind11 --diff snapA snapB     # text/HTML diff of two snapshots
   peakrdl pybind11 --replay session.json  # replay a recorded session
   peakrdl pybind11 --watch input.rdl      # rebuild & reload bindings on RDL changes

Each one is a thin wrapper over a piece of the runtime that is
already available programmatically:

- ``--explore`` imports the generated module, calls
  ``MyChip.create()``, attaches a sensible default master, and drops
  into IPython with ``soc`` bound in the namespace. No boilerplate.
- ``--diff`` deserializes two snapshots (see
  :doc:`/concepts/snapshots`) and prints (or writes to HTML) the same
  diff a notebook would render.
- ``--replay`` constructs a ``ReplayMaster.from_file(...)`` and runs
  the recorded transaction stream against the freshly built tree. See
  :doc:`/concepts/bus_layer` for how recording and replay are wired
  through the master layer.
- ``--watch`` rebuilds the C++ extension and re-imports the bound
  module whenever the source RDL changes on disk. It is the
  command-line counterpart to ``soc.reload()``.

REPL niceties
-------------

Inside ``--explore`` (or any IPython session that has imported a
generated module), the standard IPython introspection operators do
the right thing:

.. code-block:: text

   ?soc.uart.control       # full metadata: address, fields, access, on_read/write
   ??soc.uart.control      # the underlying RDL source for this register

``?`` summons the same metadata the rich repr exposes (see
:doc:`/concepts/widgets` and :doc:`/concepts/hierarchy`). ``??`` goes
one step further and prints the RDL source location and surrounding
text — useful when a field's behavior is documented in a comment in
the RDL file rather than in its description string.

Tab-completion is exhaustive: every node, every field, and every
generated enum member appears in completion lists, with the type
information drawn from the generated ``.pyi`` stubs.

Async session
-------------

Generated SoCs expose an async dual surface via
``soc.async_session()``. Use it as an async context manager whenever
the calling code already lives on an event loop (a notebook with
``%autoawait``, a ``pytest-asyncio`` test, an ``aiohttp`` handler):

.. code-block:: python

   async with soc.async_session() as s:
       v = await s.uart.control.aread()
       await s.uart.control.awrite(0x42)

Every node mirrored under ``s`` exposes ``aread`` / ``awrite`` /
``amodify`` / ``aiowait`` async methods that forward to their sync
counterparts. The mirror is lazy — only the path you touch becomes a
proxy — so a 10k-node design pays nothing for unused subtrees.

.. note::

   **There is no native async transport yet.** The dual surface is a
   thin wrapper that dispatches each call to a
   ``concurrent.futures.ThreadPoolExecutor`` (default: a single
   worker) via ``loop.run_in_executor``. Concurrency is therefore
   bounded by the executor's worker count, and the underlying bus op
   still blocks the worker thread for its full duration. Callers who
   need to issue real concurrent transactions pass their own
   wider-pool executor:

   .. code-block:: python

      from concurrent.futures import ThreadPoolExecutor

      pool = ThreadPoolExecutor(max_workers=4)
      async with soc.async_session(executor=pool) as s:
          ...
      # caller owns `pool`; AsyncSession does not shut it down

The session **owns** its default executor: on ``__aexit__`` the
shutdown is dispatched off the event loop (so an in-flight bus op
finishes cleanly without stalling the loop) and the executor is
disposed. A caller-supplied executor is left untouched — the lifecycle
belongs to whoever passed it in.

Async forwarder closures are cached per ``(node, async-name)``. A
tight ``await`` loop such as::

   async with soc.async_session() as s:
       reg = s.uart.status
       while not (await reg.aread()).ready:
           await asyncio.sleep(0)

re-uses the same forwarder closure on every iteration rather than
rebuilding it on each attribute access. This matters in poll-heavy
tests where the cost of allocating a fresh closure per cycle would
otherwise dominate.

Hot reload semantics
--------------------

Both ``--watch`` and ``soc.reload()`` (callable from inside a
notebook) are **opt-in**. Hot reload is one of those features that is
useful exactly when you trust it, and dangerous when you don't, so
the runtime is loud about every step.

On reload, the runtime:

- **Emits a warning** identifying the new source revision and the
  previous one.
- **Invalidates outstanding** ``RegisterValue`` and ``Snapshot``
  instances so stale handles cannot silently be compared against
  values from a different tree.
- **Refuses to swap** if any context manager is active — both
  per-register staging contexts (``with soc.uart.control as r: ...``)
  and bus-layer batches (``with soc.batch() as b: ...``).
- **Reattaches the existing master** to the freshly built tree, so
  routing and retry policy survive the reload.

.. note::

   **Hardware bus state is NOT affected by hot reload.** Only the
   host-side Python and C++ bindings get replaced; the chip on the
   other end of the bus does not see any reset, write, or barrier as
   a consequence of reloading the module. Live registers stay
   exactly where they were before the reload.

To re-emphasize: ``soc.reload()`` re-imports the generated module and
rebinds the master to the freshly built tree. The hardware bus is not
touched. Outstanding ``RegisterValue`` and ``Snapshot`` instances raise
``StaleHandleError`` on use after a reload — see
:doc:`/concepts/errors` for the exception's place in the hierarchy and
the recommended catch points.

For users who would rather crash than warn, the reload policy is a
single configuration knob:

.. code-block:: python

   import peakrdl
   peakrdl.reload.policy = "fail"      # abort instead of warning on reload

The default policy is ``"warn"`` (emit a warning, invalidate, swap).
``"fail"`` raises a ``ReloadAbortedError`` and leaves the existing
tree in place. Either way, the bus is untouched.

The ``peakrdl.reload.policy`` value is read **at the moment**
``soc.reload()`` runs, not at module import time. Flipping it from
``"warn"`` to ``"fail"`` (or back) mid-session takes effect on the
next call — a notebook can therefore tighten the policy before a
risky reload and relax it afterwards without restarting the kernel.

Detecting a reload from user code
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Long-running test fixtures and notebook helpers occasionally need to
notice that a reload happened so they can re-fetch handles. Two public
helpers in :mod:`peakrdl_pybind11.runtime.hot_reload` make this
explicit rather than implicit:

.. code-block:: python

   from peakrdl_pybind11.runtime.hot_reload import (
       current_generation,
       check_generation,
   )

   gen = current_generation()
   ...
   # Later, before touching cached handles:
   check_generation(gen)        # raises StaleHandleError if reloaded

``current_generation()`` returns the monotonic counter that
``soc.reload()`` bumps on every successful swap. ``check_generation()``
accepts a previously captured value and raises ``StaleHandleError``
when it no longer matches — the same exception ``RegisterValue`` and
``Snapshot`` raise internally. Fixtures that build up cached handles
across multiple test invocations capture the generation at setup and
verify it before every use.

The ``--watch`` subcommand additionally requires the optional
``watchdog`` package to drive the filesystem-change observer. Install
it via the documented extras (see :doc:`/installation`) — without it,
``--watch`` errors out on startup with a clear message, rather than
silently no-oping.

Diff & replay use cases
-----------------------

The CLI's interactive subcommands are aimed at two pain points the
sketch flags as common across user roles:

**CI regression check via** ``--diff``. A nightly job snapshots the
SoC before and after a known-good test run, archives both, and
compares them against the next run's pair. Drift surfaces as a small
HTML diff that reviewers can inspect at a glance:

.. code-block:: bash

   peakrdl pybind11 --diff baseline_after.json after.json --html > diff.html

The diff respects the same rules as the in-process
``snap2.diff(snap1)`` covered in :doc:`/concepts/snapshots`: changed
cells highlighted, added or removed rows shown, sorted by path,
filterable by access mode or node kind.

**Reproducing a flaky bring-up issue via** ``--replay``. When a lab
engineer hits a transient bug at the REPL, they enable a
``RecordingMaster`` (see :doc:`/concepts/bus_layer`), capture the
session, and ship the JSON to a colleague — who replays it locally:

.. code-block:: bash

   peakrdl pybind11 --replay flaky_bringup_2026-05-06.json

``ReplayMaster`` carries the original transaction widths and
endianness, so the replay reproduces the exact byte-for-byte bus
traffic the recording captured. Combined with ``--diff``, replay
makes "I cannot reproduce" a much rarer failure mode.

See also
--------

- :doc:`/concepts/snapshots` — the format ``--diff`` operates on, and
  the canonical use case for ``--replay``.
- :doc:`/concepts/bus_layer` — ``RecordingMaster`` and
  ``ReplayMaster`` for the recording/replay surface.
- :doc:`/concepts/widgets` — ``watch()`` is the in-notebook
  counterpart to ``--explore``: a live monitor on a single register
  or snapshot.
- :doc:`/concepts/errors` — ``StaleHandleError`` and its place in the
  exception hierarchy, raised after ``soc.reload()``.
- :doc:`/installation` — the optional ``watchdog`` extras required by
  ``--watch``.
