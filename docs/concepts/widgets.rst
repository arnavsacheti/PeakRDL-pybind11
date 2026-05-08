Jupyter & Rich Display
======================

.. note::

   This page describes the *aspirational* rich-display surface for notebooks
   and IPython terminals. The behavior here mirrors the source-of-truth
   sketch (§5) and is the target shape of the API; current releases may
   implement only a subset of these widgets.

Why this matters
----------------

Lab automators and bring-up engineers spend most of their day in Jupyter.
The notebook surface is a primary output, not a courtesy. Every node
implements both ``_repr_pretty_`` (IPython terminal) and ``_repr_html_``
(notebook) — the difference between "thorough" and "this would actually
win users" lives here.

Rich repr per node
------------------

Every node — registers, fields, mems, arrays, interrupt groups — exposes
``_repr_html_()`` for notebooks and ``_repr_pretty_()`` for IPython
terminals. Evaluating a node at the cell prompt renders a self-describing
table without the user having to call anything explicitly.

.. code-block:: python

   soc.uart.control            # → renders as an HTML table in a notebook

The rendered table looks like:

.. code-block:: text

   ┌──────────────────────────── uart[0].control @ 0x40001000  rw  reset=0x00000000 ──────────────────────────┐
   │ Bits   │ Field    │ Value (decoded)              │ Access │ On-read │ On-write │ Description           │
   ├────────┼──────────┼──────────────────────────────┼────────┼─────────┼──────────┼───────────────────────┤
   │ [0]    │ enable   │ 1                            │ rw     │ —       │ —        │ Enable UART           │
   │ [3:1]  │ baudrate │ BaudRate.BAUD_19200 (1)      │ rw     │ —       │ —        │ Baudrate selection    │
   │ [5:4]  │ parity   │ Parity.NONE (0)              │ rw     │ —       │ —        │ Parity mode           │
   └────────┴──────────┴──────────────────────────────┴────────┴─────────┴──────────┴───────────────────────┘

Highlights:

- Access modes are color-coded: ``rw`` blue, ``ro`` grey, ``wo`` orange,
  ``na`` red strikethrough.
- Side-effect badges appear inline with each field:

  - ⚠ ``rclr`` — read-clear (reading the field has a destructive side effect)
  - ↻ ``singlepulse`` — single-pulse (auto-clears after one cycle)
  - ✱ ``sticky`` — sticky (latches until explicitly cleared)
  - ⚡ ``volatile`` — volatile (hardware may change the value at any time)

  The badge mapping itself is exposed as a public dict
  ``SIDE_EFFECT_BADGES`` in :mod:`peakrdl_pybind11.runtime._registry`
  for callers who want to render their own representations:

  .. code-block:: python

     from peakrdl_pybind11.runtime._registry import SIDE_EFFECT_BADGES

     SIDE_EFFECT_BADGES
     # {"rclr": "⚠", "singlepulse": "↻", "sticky": "✱", "volatile": "⚡"}

- Click a row to expand it to show the full RDL source location and any
  user-defined properties (UDPs).
- ``soc.uart.dump()`` in a notebook renders as a nested collapsible tree,
  letting you drill into a peripheral without flooding the cell output.

Memory regions in notebooks
---------------------------

A ``mem`` region renders as a hex/ASCII dump table with click-to-edit
cells. The default widget shows 16-byte rows: hex bytes on the left,
ASCII on the right, addresses on the side, and supports range-select for
copy-and-paste back into Python.

.. code-block:: python

   soc.ram                      # → hex/ASCII dump table with click-to-edit cells
   soc.ram[0:0x100].render()    # explicit render with byte-grouping options

Use ``.render(...)`` when you want to control the row width, byte grouping,
or byte order. For an interactive view see also
:doc:`/concepts/values_and_io`.

Diff & snapshot rendering
-------------------------

A snapshot (see :doc:`/concepts/snapshots`) is itself a renderable node.
Subtracting two snapshots produces a diff that renders as a side-by-side
table with changed cells highlighted, added or removed rows shown
explicitly, sorted by path, and filterable by access mode or by node
kind.

.. code-block:: python

   snap1 = soc.snapshot()
   # ... do something ...
   snap2 = soc.snapshot()

   snap2.diff(snap1)            # → side-by-side table, changes highlighted

Interrupt group widget
----------------------

Interrupt groups render as a matrix view. Rows are sources; columns are
``State``, ``Enable``, ``Test``, and ``Pending`` (where
``Pending = State & Enable``). Pending sources are highlighted; clicking
a cell offers actions to clear, enable, or fire the corresponding source.

.. code-block:: python

   soc.uart.interrupts          # → matrix view of interrupt sources

Live monitors with ``watch()``
------------------------------

The most-asked-for pattern in tester forums: a refreshing widget for a
register or set of registers, powered by `ipywidgets`. Calling
``watch(node, period=0.1)`` on any node returns a ``Watcher`` object: a
live widget that polls on the given period and updates the rendered HTML
in place.

.. code-block:: python

   w = soc.uart.control.watch(period=0.1)            # polls every 100 ms
   w = soc.snapshot(["uart.*", "gpio.*"]).watch()    # multi-register dashboard

``Watcher`` lifecycle
~~~~~~~~~~~~~~~~~~~~~

A ``Watcher`` owns a background polling thread, so it must be torn down
explicitly. Call ``.stop()`` when you're done — it joins the polling
thread cleanly so the kernel doesn't keep ticking after the cell
finishes:

.. code-block:: python

   w = soc.uart.control.watch(period=0.1)
   try:
       do_stuff()
   finally:
       w.stop()

``try``/``finally`` guarantees teardown even if ``do_stuff()`` raises.
Where the implementation also exposes a context-manager protocol,
``with soc.uart.control.watch() as w:`` is equivalent.

Destructive-read registers
~~~~~~~~~~~~~~~~~~~~~~~~~~

``watch()`` respects the side-effect rules described in
:doc:`/concepts/values_and_io`: periodic polling of an ``rclr`` register
would silently destroy the very state you are trying to observe, so
``watch()`` refuses without an explicit opt-in.

.. warning::

   ``watch()`` on a register with ``info.on_read = "rclr"`` (or any
   other destructive-read semantics) requires
   ``allow_destructive=True``. Without it, the call raises
   ``NotSupportedError`` rather than quietly clearing the state on
   every poll.

.. code-block:: python

   # rclr register: must explicitly acknowledge the destructive read
   w = soc.system.reset_status.watch(
       period=0.5,
       allow_destructive=True,
   )

Each tick of a watched register returns an immutable ``RegisterValue``,
so prior frames remain valid even after the widget updates.

.. note::

   ``watch()`` is the only rich-display surface that requires the
   optional ``ipywidgets`` dependency. It is a *soft import* — when
   ``ipywidgets`` is missing, calling ``watch()`` raises::

       NotSupportedError("install peakrdl-pybind11[notebook] for watch()")

   All other rich-display features (HTML repr, pretty repr, diff, mem
   dump) work without ``ipywidgets``. Install the extra with
   ``pip install peakrdl-pybind11[notebook]`` to enable live monitors.

Plain-terminal IPython
----------------------

Outside the notebook, ``_repr_pretty_`` produces the same content as the
notebook tables but with terminal color, aligned columns, and
side-effect markers in a way that survives copy-paste into a bug
report. Engineers who live in ``ipython`` rather than Jupyter get the
same information density without losing readability when the output is
pasted back into a chat or issue.

See also
--------

- :doc:`/concepts/values_and_io` — what reads and writes return, and how
  side-effects interact with rich display.
- :doc:`/concepts/snapshots` — how snapshot objects are produced and
  diffed.
- :doc:`/concepts/interrupts` — the model behind the interrupt group
  matrix widget.
