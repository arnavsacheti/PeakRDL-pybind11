"""
``--explore <module>`` CLI subcommand (Unit 24).

After the export completes, ``--explore`` imports the generated
module and drops the user into an interactive REPL with ``soc =
create()`` already in the namespace. IPython is preferred when
available; we fall back to :func:`code.interact` for a stock-Python
build.

Lifecycle:

* ``add_arguments`` registers the flag with the exporter group.
* ``post_handle`` runs *after* :py:meth:`Exporter.do_export`. The
  generated module is therefore on disk before we try to import it.

``--explore`` is intentionally **post-export**, not preempting: the
sketch describes ``--explore`` as the "create the module, then drop
into REPL" workflow, not a standalone command. Implementing it via
``post_handle`` keeps the regular export path identical and makes
test-driving the import side easy without spinning up the build.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from typing import Any

logger = logging.getLogger("peakrdl_pybind11.cli.explore")

__all__ = ["add_arguments", "post_handle", "spawn_repl"]


def add_arguments(arg_group: argparse._ActionsContainer) -> None:
    """Register ``--explore``."""
    arg_group.add_argument(
        "--explore",
        dest="explore",
        metavar="MODULE",
        default=None,
        help=(
            "After export, spawn an interactive REPL with `soc = "
            "MODULE.create()` already bound in the namespace. Uses "
            "IPython if available, else stock `code.interact`. The "
            "MODULE argument is the import name of the generated "
            "module (the value of --soc-name)."
        ),
    )


def _try_import_ipython_shell() -> type | None:
    """Return an IPython interactive embed if IPython is importable."""
    try:
        from IPython.terminal.embed import InteractiveShellEmbed  # type: ignore
    except ImportError:
        return None
    return InteractiveShellEmbed


def spawn_repl(namespace: dict[str, Any], banner: str = "") -> None:
    """Drop into IPython if available, else into ``code.interact``.

    Public so tests can call it with a stub namespace and verify
    behaviour without spinning a real interpreter.
    """
    embed_cls = _try_import_ipython_shell()
    if embed_cls is not None:
        shell = embed_cls(banner1=banner)
        shell(local_ns=namespace)
        return

    import code

    code.interact(banner=banner, local=namespace)


def _import_generated_module(name: str) -> Any:  # noqa: ANN401
    """Import the generated module by name.

    The export step writes the module under ``options.output``; the
    user's PYTHONPATH (or ``options.output`` itself, when added by the
    caller) needs to make it importable. We re-raise with a helpful
    message if the import fails so the user does not have to debug an
    opaque ``ModuleNotFoundError``.
    """
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ImportError(
            f"--explore could not import generated module {name!r}. "
            "Make sure the export ran successfully and the output "
            "directory is on PYTHONPATH (e.g. via `pip install -e .` "
            "from the export directory, or by setting PYTHONPATH)."
        ) from exc


def post_handle(options: argparse.Namespace) -> None:
    """Spawn the REPL if ``--explore`` was set."""
    module_name: str | None = getattr(options, "explore", None)
    if not module_name:
        return

    output = getattr(options, "output", None)
    if output is not None and output not in sys.path:
        sys.path.insert(0, str(output))

    module = _import_generated_module(module_name)
    create = getattr(module, "create", None)
    if create is None:
        raise AttributeError(
            f"generated module {module_name!r} does not export create(); "
            "expected the standard PeakRDL-pybind11 entry point."
        )
    soc = create()
    namespace = {
        "soc": soc,
        module_name: module,
    }
    banner = (
        f"PeakRDL-pybind11 explore: soc = {module_name}.create()\n"
        "Use `soc.dump()` to print the tree (when implemented)."
    )
    spawn_repl(namespace, banner=banner)
