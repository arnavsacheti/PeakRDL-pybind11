CLI Plugin Seam
===============

.. note::

   **Aspirational documentation.** This page describes the target API
   defined in ``docs/IDEAL_API_SKETCH.md`` and the in-tree CLI plugin
   seam under :mod:`peakrdl_pybind11.cli`. Some of the names below
   (notably ``try_handle`` as a module-level entry point) are the
   target shape; the in-tree modules that landed first use the legacy
   ``handle`` name. The dispatcher accepts either.

Overview
--------

:mod:`peakrdl_pybind11.cli` is a package, not a module. Every
``.py`` file dropped inside it is auto-discovered when the
``peakrdl pybind11`` exporter is invoked, and may attach extra
argparse flags and/or run handlers in three lifecycle phases:

* **registration** — at ``add_exporter_arguments`` time, the module
  declares its flags on the same argparse group PeakRDL hands the
  exporter.
* **pre-export** — before any export work runs, the module may claim
  the run (e.g. ``--diff`` operates on existing snapshot files; there
  is nothing to export).
* **post-export** — after a successful export, the module may take an
  action that needs the freshly built bindings on disk (e.g.
  ``--explore`` imports the generated module and drops into a REPL).

The seam exists so library extenders can add flags like
``--export-yaml`` or ``--lint`` without modifying
``__peakrdl__.py``. Drop a file in :mod:`peakrdl_pybind11.cli` and the
exporter picks it up.

For the user-facing documentation of the in-tree subcommands
(``--explore``, ``--diff``, ``--replay``, ``--watch``,
``--strict-fields``), see :doc:`/concepts/cli_repl`.

Module contract
---------------

A CLI module may define any subset of the following names. Modules
that only register a flag and read it from ``options`` later (without
preempting or post-processing) need not define any handlers at all.

``add_arguments(arg_group)``
    Called from :py:meth:`peakrdl_pybind11.__peakrdl__.Exporter.add_exporter_arguments`.
    ``arg_group`` is the standard :class:`argparse._ActionsContainer`
    PeakRDL hands the exporter. Use the usual argparse API
    (``add_argument``, ``add_mutually_exclusive_group``, etc.). Flags
    declared here surface as attributes on the parsed
    :class:`argparse.Namespace` passed to the handlers below.

``try_handle(options) -> bool``
    Pre-export hook. Inspect ``options`` and return truthy if this
    module fully handled the run; the exporter will then **skip** its
    normal pipeline. Return falsy (or do not define ``try_handle``) to
    let the export proceed. Used by ``--diff`` / ``--replay`` — they
    operate on existing snapshot/session files and have nothing to
    export.

``handle(options)`` (legacy)
    Same semantics as ``try_handle``: a truthy return preempts the
    export. Sibling units that landed before ``try_handle`` was
    promoted to the canonical name use ``handle``; the dispatcher
    accepts either. New modules should prefer ``try_handle`` for
    clarity (the name signals "may or may not claim the run").

``post_handle(options)``
    Post-export hook. Runs after :py:meth:`Exporter.do_export`
    finishes successfully. Used by ``--explore`` (imports the
    generated module and drops into a REPL) and ``--watch`` (rebuilds
    on RDL changes). Because the export has already run by this point,
    the generated module is on disk and importable.

.. note::

   The first module whose pre-export hook returns truthy "wins" —
   later modules' pre-export hooks are not consulted. Allowing two
   preempting handlers in the same invocation is almost certainly a
   configuration mistake (e.g. ``--diff`` and ``--replay`` together
   makes no sense), so the dispatcher short-circuits.

Discovery API
-------------

The :mod:`peakrdl_pybind11.cli` package exports the following
top-level callables. Wiring code (``__peakrdl__.py``) and tests
should use these; do not import from individual CLI modules.

.. code-block:: python

   from peakrdl_pybind11 import cli

   cli.iter_modules()              # list of imported sibling-unit CLI modules
   cli.discover_subcommands(grp)   # call every module's add_arguments
   cli.try_handle(options)         # run pre-export hooks; True if any claimed
   cli.run_post_handlers(options)  # run every module's post_handle
   cli.run_handlers(options)       # alias for run_post_handlers (compat)

``iter_modules() -> list[ModuleType]``
    Returns every imported sibling-unit CLI module. Useful for
    introspection in tests. Modules whose name starts with ``_`` are
    skipped (see *Discovery semantics* below).

``discover_subcommands(arg_group) -> None``
    Calls every module's ``add_arguments(arg_group)``, in alphabetical
    order. The exporter calls this once from
    ``add_exporter_arguments``.

``try_handle(options) -> bool``
    Walks every module and invokes its pre-export hook
    (``try_handle`` if defined, else legacy ``handle``). Returns
    ``True`` at the first module that claims the run; returns
    ``False`` if every module passed. The exporter calls this at the
    top of ``do_export`` and short-circuits on ``True``.

``run_post_handlers(options) -> None``
    Walks every module and invokes its ``post_handle`` (or the legacy
    ``handle`` name where the module has no separate ``post_handle``;
    sibling units that landed before the split documented their
    post-export work under ``handle``). Called by the exporter after
    the export succeeds.

``run_handlers(options) -> None``
    Backward-compatible alias for ``run_post_handlers``. Earlier
    sibling units called the post-export entry point ``run_handlers``;
    new code should prefer ``run_post_handlers``.

Wiring into PeakRDL
-------------------

The exporter wires the dispatcher into PeakRDL's
:class:`ExporterSubcommandPlugin` lifecycle. In
``src/peakrdl_pybind11/__peakrdl__.py``:

.. code-block:: python

   from . import cli as _cli

   class Exporter(ExporterSubcommandPlugin):
       def add_exporter_arguments(self, arg_group):
           # ... declare core exporter flags (--soc-name, --gen-pyi, ...) ...

           # Sibling-unit CLI extensions discover themselves here.
           _cli.discover_subcommands(arg_group)

       def do_export(self, top_node, options):
           # Pre-export: --diff / --replay / similar may claim the run
           # before any export work happens.
           if _cli.try_handle(options):
               return

           # ... run the normal export pipeline ...

           # Post-export: --explore / --watch / ... act on the freshly
           # built bindings.
           _cli.run_post_handlers(options)

That is the entire integration surface. Any module dropped into
:mod:`peakrdl_pybind11.cli` participates in all three phases without
further wiring.

Worked example: ``--lint``
--------------------------

A linter that scans the RDL for common pitfalls — unused fields,
unset reset values — and exits non-zero on findings. Drop the
following at ``src/peakrdl_pybind11/cli/lint.py``:

.. code-block:: python

   """``--lint`` CLI subcommand: check the RDL for common pitfalls."""

   from __future__ import annotations

   import argparse
   import sys

   __all__ = ["add_arguments", "try_handle"]


   def add_arguments(arg_group: argparse._ActionsContainer) -> None:
       """Register ``--lint``."""
       arg_group.add_argument(
           "--lint",
           dest="lint",
           action="store_true",
           default=False,
           help=(
               "Lint the input RDL for common pitfalls (unused fields, "
               "unset reset values). Skips the primary export. Exits "
               "non-zero if any findings are reported."
           ),
       )


   def _lint(options: argparse.Namespace) -> list[str]:
       """Re-parse ``options.input`` via :class:`systemrdl.RDLCompiler`
       and return a list of ``"<path>: <message>"`` finding strings."""
       findings: list[str] = []
       # ... walk the tree, append per-pitfall lines ...
       return findings


   def try_handle(options: argparse.Namespace) -> bool:
       """Run the linter if ``--lint`` was set; report whether we claimed."""
       if not getattr(options, "lint", False):
           return False

       findings = _lint(options)
       for line in findings:
           sys.stderr.write(f"{line}\n")
       if findings:
           sys.exit(1)
       return True

The module declares ``--lint`` in ``add_arguments``. ``try_handle``
returns ``False`` when ``--lint`` is absent (export proceeds normally)
and ``True`` when ``--lint`` is set (export is skipped, linter runs,
process exits non-zero on findings). No ``post_handle`` is needed:
``--lint`` is fully a pre-export operation.

.. note::

   ``--lint`` deliberately runs *before* the export and is mutually
   exclusive with the build pipeline. If you want a lint-and-also-
   export flow, define a falsy-returning ``try_handle`` (so the
   export still runs) and accumulate findings into ``options`` for a
   ``post_handle`` to print after the build.

In-tree CLI modules
-------------------

The following modules ship with PeakRDL-pybind11 today; they live
under :mod:`peakrdl_pybind11.cli` and are good reading for anyone
writing a new module. See :doc:`/concepts/cli_repl` for the
user-facing documentation of the flags they expose.

``cli/explore.py``
    Adds ``--explore MODULE``. After the export completes,
    ``post_handle`` imports the generated module, calls
    ``MODULE.create()``, and drops into IPython (or stock
    :func:`code.interact`) with ``soc`` already bound in the namespace.

``cli/diff.py``
    Adds ``--diff SNAP_A SNAP_B`` (and ``--html`` for HTML rendering).
    Pre-export: deserializes both snapshots and prints a diff,
    delegating to :class:`Snapshot.diff` when available and falling
    back to a JSON-shape diff otherwise.

``cli/replay.py``
    Adds ``--replay SESSION_JSON``. Pre-export: loads a recorded
    :class:`RecordingMaster` session and replays it against a freshly
    built master via :class:`ReplayMaster.from_file`.

``cli/watch.py``
    Adds ``--watch INPUT_RDL``. Pre-export: watches the input RDL via
    :mod:`watchdog` and rebuilds the generated module on every save,
    calling ``soc.reload()`` to pick up the new tree without losing
    bus state.

``cli/strict_fields.py``
    Adds ``--strict-fields=<bool>``. Does **not** register a handler;
    the exporter consults :func:`is_strict_from_options` directly when
    rendering the runtime template. Default is strict: bare attribute
    assignment on a register raises :class:`AttributeError` outside a
    context manager. ``--strict-fields=false`` opts out and emits a
    :class:`DeprecationWarning`.

Discovery semantics
-------------------

A handful of rules govern which modules are loaded and how failures
are reported:

* Module names beginning with ``_`` are reserved for internals and
  skipped during discovery. Use this to keep helper modules under the
  :mod:`peakrdl_pybind11.cli` namespace without having them
  auto-registered (e.g. ``cli/_helpers.py``).
* Modules are discovered in the order :func:`pkgutil.iter_modules`
  yields them (alphabetical on most filesystems). Do not rely on
  registration order for correctness — modules should be independent.
* Failures during ``add_arguments`` or ``post_handle`` are logged via
  :func:`logging.exception` and re-raised. CLI failures are
  user-explicit (the user typed ``--something``), so the right default
  is loud failure, not silent fallback. This is the inverse of the
  ``_fire`` convention used for runtime hooks: a runtime hook
  swallowing a callback error preserves the chip's read/write path,
  but a CLI hook that swallows an error makes ``--lint`` quietly
  succeed when it should have surfaced a problem.

.. note::

   Module-import failures (i.e. the module file itself fails to
   import — syntax error, missing dependency at top level) are
   *logged and skipped* rather than raised. A typo in a brand-new
   third-party CLI module should not break the whole exporter for
   users who never asked for that module. The error is still in the
   log, and the missing flag will fail at parse time if the user does
   reach for it.
