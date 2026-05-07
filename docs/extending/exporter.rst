Exporter Plugin Seam
====================

The exporter is a pipeline (descriptors -> bindings -> runtime -> stubs) and
that pipeline is intentionally finite. Codegen passes that aren't part of the
core contract -- interrupt-trio detection, schema emission, ``.pyi``
enrichment, project-specific output formats -- belong outside ``exporter.py``
so the core stays small and the extra passes stay swappable.

The seam for that extra work is the ``peakrdl_pybind11.exporter_plugins``
package. Drop a module into the package, expose ``register(exporter)``, and
the exporter picks it up at construction time. Optionally implement
``post_export(ctx)`` and the same plugin gets a chance to write extra files
once the main pipeline has finished.

This page documents that contract for library extenders.

Overview
--------

``peakrdl_pybind11.exporter_plugins`` is a regular Python package. At
``Pybind11Exporter.__init__`` time, the exporter walks the package, imports
every module that does not start with ``_``, and calls each module's
``register(exporter)`` function. A module that returns a non-``None`` plugin
instance from ``register`` is also added to the post-export list -- the
exporter will fire ``post_export(ctx)`` on it after the four built-in
codegen stages complete.

Two phases, two responsibilities:

* ``register(exporter)`` runs **early**, before any codegen. It can install
  Jinja filters, stash references on the exporter for templates to read, or
  swap in a different template loader. This is the right place to influence
  the rendered output of the built-in pipeline.
* ``post_export(ctx)`` runs **late**, after every built-in stage has
  finished. It can read the collected node tree, the resolved options, and
  the output directory, and write entirely new artifacts alongside the
  generated module.

Plugins never modify ``exporter.py``. Adding a feature is adding a file to
``exporter_plugins/``.

Two-phase model
---------------

A plugin module has at most two entry points. Both are optional only in the
sense that a plugin that does nothing in either phase is degenerate -- a
plugin that contributes anything at all implements at least one.

``register(exporter) -> plugin_or_None``
    Called once during ``Pybind11Exporter.__init__``, before the exporter
    has a top node, an output directory, or a soc name. Use this phase to:

    * install Jinja filters or globals on ``exporter.env``
    * stash references on the exporter (e.g. an interrupt-pattern matcher)
    * extend ``exporter._KNOWN_UDPS`` or register additional UDPs

    The return value matters. If ``register`` returns ``None``, the plugin
    is treated as a one-shot configuration hook and never seen again. If it
    returns *anything else*, that object is appended to the post-export
    plugin list, and ``run_post_export`` will look for ``post_export`` on
    it later.

    .. code-block:: python

       # peakrdl_pybind11/exporter_plugins/my_plugin.py

       def register(exporter):
           exporter.env.filters["upper_snake"] = lambda s: s.upper()
           return MyPlugin()  # opt in to post_export

``post_export(ctx) -> None``
    An optional method on the plugin instance returned from ``register``.
    Called by ``Pybind11Exporter.export()`` after the main pipeline
    (descriptors, bindings, runtime, stubs) has finished writing files.
    Use this phase for **late codegen**:

    * emit a ``schema.json`` describing the register layout
    * walk the collected nodes and write a detected-interrupt-group manifest
    * append synthesized declarations to the generated ``.pyi``
    * produce documentation, lint files, or vendor-specific output

    By the time ``post_export`` runs, every artifact the core promised is on
    disk. A plugin that crashes here cannot corrupt the main module -- the
    exporter has already declared success on its part of the contract.

PluginContext dataclass
-----------------------

``run_post_export`` builds a ``PluginContext`` and passes it to every
post-export plugin. Plugins should treat the context as **read-only** -- it
is the same instance for every plugin in the registration list.

.. list-table::
   :header-rows: 1
   :widths: 18 22 60

   * - Field
     - Type
     - Meaning
   * - ``exporter``
     - ``Pybind11Exporter``
     - The exporter instance. Useful for reading
       ``exporter.env`` (Jinja environment), ``exporter.soc_version``, and
       any state a sibling plugin parked there during ``register``.
   * - ``top_node``
     - ``AddrmapNode``
     - The resolved top-of-design node. Already
       unwrapped from ``RootNode`` -- plugins do not need to re-check.
   * - ``output_dir``
     - ``pathlib.Path``
     - The directory the main pipeline wrote into.
       New artifacts should land here or under ``output_dir / soc_name /``.
   * - ``soc_name``
     - ``str``
     - The sanitized module name (also the name of the
       sub-package directory created for the runtime). Safe to use as a
       Python and C++ identifier.
   * - ``nodes``
     - ``Nodes`` (TypedDict)
     - The node lists collected by ``_collect_nodes``: ``addrmaps``,
       ``regfiles``, ``regs``, ``fields``, ``mems``. Same lists the built-in
       templates render against -- plugins see exactly what the core saw.
   * - ``options``
     - ``dict[str, Any]``
     - CLI-derived options. Stable keys today include ``interrupt_pattern``
       (the regex/glob a plugin can use to match a state/enable/test trio)
       and any extra kwargs the CLI forwarded under
       ``--plugin-option key=value``. Unknown keys are passed through
       unchanged.

The dataclass is frozen by construction; plugins should not mutate it. To
share state between two plugins, park the state on ``ctx.exporter``
(plugins are loaded in deterministic order, so a producer plugin that
registers earlier in alphabetical order can stash data the consumer reads).

Programmatic registration
-------------------------

For tests and downstream tooling that build plugins outside the package
directory, ``exporter_plugins`` exposes two helpers.

``register_plugin(plugin) -> None``
    Append ``plugin`` to the registry as if it had been auto-discovered.
    Useful in pytest fixtures that want to run a one-off plugin against a
    real exporter without dropping a file under ``exporter_plugins/``.

``registered_plugins() -> list``
    Return the current list of registered plugins, in registration order.
    Test code uses this to assert "did my plugin actually get picked up?"
    or to take a snapshot, run an export, and roll the registry back.

.. code-block:: python

   from peakrdl_pybind11.exporter_plugins import (
       register_plugin,
       registered_plugins,
   )

   class _CountingPlugin:
       def __init__(self) -> None:
           self.calls = 0
       def post_export(self, ctx) -> None:
           self.calls += 1

   plugin = _CountingPlugin()
   register_plugin(plugin)
   assert plugin in registered_plugins()

run_post_export
---------------

``run_post_export(ctx)`` is the entry point ``Pybind11Exporter.export()``
calls after the main pipeline. It iterates the registered plugins **in
registration order** and invokes ``post_export(ctx)`` on each one that
defines it. Plugins without ``post_export`` are skipped silently.

.. note::

   ``run_post_export`` swallows plugin exceptions and logs them. The main
   exporter has already written every artifact it promised by the time
   post-export runs, so a misbehaving plugin must not be allowed to mark
   the export as failed. Failures are logged with the plugin's module name
   so downstream tooling can detect and report them, but the exporter
   itself returns normally.

Plugin authors who want a hard failure should raise inside their own
``post_export`` and check the log -- the exporter will not propagate the
exception.

Worked example: ``feature_detection.py``
----------------------------------------

The in-tree plugin under ``src/peakrdl_pybind11/exporter_plugins/feature_detection.py``
detects the ``intr_state`` / ``intr_enable`` / ``intr_test`` trio that
appears across OpenTitan, ARM, and many vendor SoCs, and emits a
``schema.json`` describing the discovered interrupt groups. It exists for
two reasons:

1. The detection logic is *interesting* (regex matching, partial-trio
   handling, alias resolution) and would clutter ``exporter.py``.
2. The ``schema.json`` artifact is consumed by external tooling (the CLI's
   ``peakrdl explore`` subcommand and downstream lint passes), so it has
   to land in the output directory but is not part of the generated Python
   module itself.

It uses both phases:

.. code-block:: python

   # peakrdl_pybind11/exporter_plugins/feature_detection.py

   class FeatureDetectionPlugin:
       def __init__(self) -> None:
           self.interrupt_groups: list[dict] = []

       def post_export(self, ctx) -> None:
           pattern = ctx.options.get("interrupt_pattern")
           self.interrupt_groups = _detect_interrupt_trios(
               ctx.nodes["regs"], pattern=pattern,
           )
           _emit_schema_json(
               ctx.output_dir,
               ctx.soc_name,
               ctx.top_node,
               self.interrupt_groups,
           )


   def register(exporter):
       plugin = FeatureDetectionPlugin()
       exporter._feature_detection = plugin  # let templates see it
       return plugin  # opt in to post_export

The ``register`` body returns the plugin instance, opting it into the
post-export phase. ``post_export`` reads the collected node lists out of
``ctx.nodes``, runs the detector against ``ctx.options["interrupt_pattern"]``,
and writes ``schema.json`` to **both** locations:

* ``ctx.output_dir / "schema.json"`` -- next to the C++ sources, where build
  tooling expects it.
* ``ctx.output_dir / ctx.soc_name / "schema.json"`` -- inside the runtime
  package, so the generated module can import-time load it without a path
  lookup outside its own ``__init__.py``.

This double-write is a deliberate convention for any artifact that needs to
be both a build-time input and a runtime resource.

Custom plugin tutorial
----------------------

A short, self-contained example: a plugin that emits a Markdown register
summary alongside the generated module. The whole plugin fits in a single
file.

Project layout::

   src/peakrdl_pybind11/
       exporter.py
       exporter_plugins/
           __init__.py
           feature_detection.py
           markdown_summary.py        # <-- new file

The plugin module:

.. code-block:: python

   # peakrdl_pybind11/exporter_plugins/markdown_summary.py
   """Emit a register summary as Markdown next to the generated module."""

   from pathlib import Path


   class MarkdownSummaryPlugin:
       def post_export(self, ctx) -> None:
           lines: list[str] = [f"# {ctx.soc_name}", ""]
           for reg in ctx.nodes["regs"]:
               offset = reg.absolute_address
               desc = reg.get_property("desc") or ""
               lines.append(f"## `{reg.get_path()}` @ 0x{offset:08x}")
               if desc:
                   lines.append("")
                   lines.append(desc)
               lines.append("")
               for field in reg.fields():
                   bits = f"[{field.high}:{field.low}]"
                   lines.append(f"- **{field.inst_name}** {bits}")
               lines.append("")
           out = Path(ctx.output_dir) / f"{ctx.soc_name}.md"
           out.write_text("\n".join(lines), encoding="utf-8")


   def register(exporter):
       return MarkdownSummaryPlugin()

Drop the file in, run an export, and ``<output_dir>/<soc_name>.md``
materializes alongside the C++ and Python artifacts. No edits to
``exporter.py``, no changes to the CLI -- the ``register`` callback is the
contract.

Discovery semantics
-------------------

Discovery walks ``peakrdl_pybind11.exporter_plugins`` once per
``Pybind11Exporter`` instance. The rules:

* Modules whose name starts with an underscore (``_helpers.py``,
  ``_test_fixtures.py``) are **reserved for internals** and are skipped.
  Use this convention for shared helpers that should not be auto-loaded as
  plugins.
* Sub-packages are walked the same way; a directory containing an
  ``__init__.py`` is treated as a single module unless its ``__init__``
  re-exports its members.
* Modules that fail to import are logged and skipped; one broken plugin
  does not block the others.
* ``register`` is called exactly once per module, in alphabetical filename
  order. This makes the registration list deterministic and lets a
  plugin authored for ``a_priority`` rely on running before
  ``z_priority``.

``discover_plugins(exporter) -> list``
    Returns the list of plugin instances that opted into post-export (i.e.
    the non-``None`` return values from ``register``). Tests and
    introspection tools can call this directly to see which plugins are
    active for a given exporter.

A typical test-time usage:

.. code-block:: python

   exporter = Pybind11Exporter()
   plugins = discover_plugins(exporter)
   assert any(isinstance(p, FeatureDetectionPlugin) for p in plugins)

The list is in registration order; plugins added later by
``register_plugin`` appear at the end.
