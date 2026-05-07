"""CLI extension auto-discovery.

This package is the seam that sibling units of the API overhaul use to
attach extra arguments to the ``peakrdl pybind11`` invocation and run
post-export handlers, without touching ``__peakrdl__.py``. Each module
inside this package may export:

* ``add_arguments(arg_group)`` — called from
  :py:meth:`Exporter.add_exporter_arguments`. ``arg_group`` is the same
  argparse action container PeakRDL hands the exporter.
* ``handle(options)`` — called from :py:meth:`Exporter.do_export` after
  the exporter has run. ``options`` is the parsed argparse namespace.

Module names beginning with ``_`` are treated as internal and skipped.

Despite the package name, these are *not* new top-level subcommands of
``peakrdl``; the ``peakrdl pybind11`` subcommand is owned by
``__peakrdl__.py`` (see :class:`peakrdl_pybind11.__peakrdl__.Exporter`).
``cli`` modules are sibling-unit extensions that compose with that
single subcommand.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import pkgutil
from types import ModuleType

logger = logging.getLogger("peakrdl_pybind11.cli")

__all__ = [
    "discover_subcommands",
    "iter_modules",
    "run_handlers",
    "run_post_handlers",
    "try_handle",
]


def _iter_modules() -> list[ModuleType]:
    """Yield every imported sibling-unit CLI module under this package."""
    package_path = list(__path__)  # type: ignore[name-defined]
    package_name = __name__
    modules: list[ModuleType] = []
    for info in pkgutil.iter_modules(package_path):
        if info.name.startswith("_"):
            continue
        full_name = f"{package_name}.{info.name}"
        try:
            modules.append(importlib.import_module(full_name))
        except Exception:
            logger.warning("failed to import CLI module %r", full_name, exc_info=True)
    return modules


def iter_modules() -> list[ModuleType]:
    """Public wrapper around :func:`_iter_modules` for tests / introspection."""
    return _iter_modules()


def discover_subcommands(arg_group: argparse._ActionsContainer) -> None:
    """Run ``add_arguments`` from every sibling-unit CLI module.

    Args:
        arg_group: argparse action container to extend. The exporter
            passes the same group it received from PeakRDL.
    """
    for module in _iter_modules():
        add_arguments = getattr(module, "add_arguments", None)
        if add_arguments is None:
            continue
        try:
            add_arguments(arg_group)
        except Exception:
            logger.warning(
                "CLI module %r add_arguments() raised", module.__name__, exc_info=True
            )


def try_handle(options: argparse.Namespace) -> bool:
    """Invoke ``handle`` on each sibling-unit CLI module before export.

    Each module's ``handle(options)`` may return truthy to indicate it has
    fully handled the run; in that case the exporter skips its primary
    export. Modules that return falsy (or do not define ``handle``) are
    inert and let the export proceed.

    The first module that returns truthy "wins" — later modules are not
    consulted, because allowing two preempting handlers to run in the
    same invocation is almost certainly a configuration mistake.

    Returns:
        ``True`` if any handler claimed the run; ``False`` otherwise.
    """
    for module in _iter_modules():
        handle = getattr(module, "handle", None)
        if handle is None:
            continue
        try:
            if handle(options):
                return True
        except Exception:
            logger.exception("CLI module %r handle() raised", module.__name__)
            raise
    return False


def run_post_handlers(options: argparse.Namespace) -> None:
    """Run ``post_handle`` on every sibling-unit CLI module after export.

    Used by post-export niceties (e.g. ``--explore`` drops into a REPL
    once the generated module is on disk). Exceptions propagate — the
    user explicitly asked for X via the flag, silent failure is wrong.
    """
    for module in _iter_modules():
        post_handle = getattr(module, "post_handle", None)
        if post_handle is None:
            continue
        try:
            post_handle(options)
        except Exception:
            logger.exception("CLI module %r post_handle() raised", module.__name__)
            raise


# Backwards-compatible alias — earlier sibling units called the
# post-export entry point ``run_handlers``. New code should prefer
# :func:`run_post_handlers`.
run_handlers = run_post_handlers
