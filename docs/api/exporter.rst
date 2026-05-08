Exporter Module
===============

The :class:`~peakrdl_pybind11.exporter.Pybind11Exporter` turns a SystemRDL
compilation into a buildable PyBind11 module. It can be driven directly from
Python or via the ``peakrdl pybind11`` subcommand registered by the
``peakrdl.exporters`` entry point.

Programmatic API
----------------

The exporter is a plain class. Compile your RDL with the upstream
``systemrdl-compiler``, then hand the root node to :meth:`Pybind11Exporter.export`:

.. code-block:: python

   from systemrdl import RDLCompiler
   from peakrdl_pybind11 import Pybind11Exporter

   rdlc = RDLCompiler()
   # Optional: pre-register the exporter's UDPs so users do not have to
   # declare ``property is_flag {...}`` etc. in their RDL.
   Pybind11Exporter.register_udps(rdlc)

   rdlc.compile_file("mychip.rdl")
   root = rdlc.elaborate()

   Pybind11Exporter().export(
       root,
       output_dir="build/mychip",
       soc_name="mychip",
       split_by_hierarchy=True,
   )

CLI options reference
---------------------

The exporter is exposed through ``peakrdl pybind11``. ``peakrdl-cli`` itself
contributes the input file argument and ``--top``; the flags below are the
ones added (or aspired to) by this exporter.

``--soc-name NAME``
   Name of the generated SoC module. Defaults to the top-level addrmap's
   instance name. The chosen name shapes the import path your test code uses
   (``import NAME`` -> ``NAME.create()``).

``--top NODE``
   *(provided by peakrdl-cli.)* Pick a non-root addrmap to export. Useful when
   one ``.rdl`` defines several SoCs and you want bindings for just one.

``--gen-pyi`` / ``--no-gen-pyi``
   Emit ``.pyi`` stub files alongside the compiled extension so editors and
   type checkers see the full hierarchy. Default: enabled.

``--split-bindings COUNT``
   Split the generated PyBind11 sources across multiple translation units
   when the register count exceeds *COUNT*. Speeds up compilation for large
   designs by enabling parallel ``make``/``ninja`` jobs. Set to ``0`` to
   force a single TU. Default: ``100``. Ignored when
   ``--split-by-hierarchy`` is used.

``--split-by-hierarchy``
   Split bindings by addrmap/regfile boundary instead of by register count.
   Keeps related registers in the same translation unit, which is friendlier
   to incremental rebuilds and matches the way most large SoCs are organized.

``--explore``
   Spawn an IPython REPL with ``soc`` already created and ready to use.
   Inside the REPL, ``?soc.uart.control`` shows full metadata and
   ``??soc.uart.control`` shows the originating RDL source (sketch §21).

``--diff snapA snapB``
   Render a text or HTML diff of two saved snapshots. Pairs with
   :meth:`soc.snapshot` and :meth:`soc.save`/:func:`soc.load` for
   golden-state regression workflows (sketch §21).

``--replay session.json``
   Replay a recorded master session (the trace ``Master.record()``
   produces) against a target. Useful for reproducing a bug captured on
   silicon against a mock or a cosim model (sketch §21).

``--watch input.rdl``
   Re-build and re-load the bound module whenever the input ``.rdl``
   changes. Backed by ``watchdog``; emits a warning on every reload so
   you cannot miss it (sketch §21). See `Hot reload semantics`_ below.

``--strict-fields=false``
   Build-time opt-out from the strict-fields default. Restores
   attribute-assign-as-read-modify-write so teams porting C drivers can
   keep their existing call sites (sketch §22.8).

   .. warning::

      ``--strict-fields=false`` is intentionally noisy. It emits a
      ``DeprecationWarning`` once at module import *and* once per loose
      attribute assignment. Silent RMW is the leading source of
      "I thought that wrote" bugs, and the warning stream is the price
      of the escape hatch. The strict default is preferred — use
      :meth:`Register.write_fields` or :meth:`RegisterValue.replace` for
      multi-field updates.

Hot reload semantics
--------------------

``--watch`` (and its in-process twin :meth:`Soc.reload`) is opt-in. On
reload, the runtime:

* emits a warning so the event is never silent,
* invalidates outstanding :class:`RegisterValue` and :class:`Snapshot`
  handles — they raise on next access rather than returning stale data,
* refuses to swap if a context manager (transaction, write-only block,
  etc.) is currently active, and
* re-attaches the existing master to the freshly built tree.

**Hardware bus state is not affected** — only the host-side bindings are
replaced. Users who would rather crash than warn can set
``peakrdl.reload.policy = "fail"`` and the runtime aborts the reload
instead of continuing with new bindings.

API reference
-------------

.. automodule:: peakrdl_pybind11.exporter
   :members:
   :undoc-members:
   :show-inheritance:
   :special-members: __init__
