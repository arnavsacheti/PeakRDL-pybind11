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
from typing import Any

logger = logging.getLogger("peakrdl_pybind11.cli")

__all__ = ["discover_subcommands", "run_handlers"]


def _iter_modules() -> list[Any]:
    """Yield every imported sibling-unit CLI module under this package."""
    package_path = list(__path__)  # type: ignore[name-defined]
    package_name = __name__
    modules: list[Any] = []
    for info in pkgutil.iter_modules(package_path):
        if info.name.startswith("_"):
            continue
        full_name = f"{package_name}.{info.name}"
        try:
            modules.append(importlib.import_module(full_name))
        except Exception:
            logger.warning("failed to import CLI module %r", full_name, exc_info=True)
    return modules


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


def run_handlers(options: argparse.Namespace) -> None:
    """Run ``handle`` from every sibling-unit CLI module.

    Called after :py:meth:`Exporter.do_export` finishes its primary
    export. Handler exceptions are logged and re-raised so the user sees
    the failure (a CLI handler is closer to "the user explicitly asked
    for X" than a runtime hook is, so silent failure is the wrong
    default here).
    """
    for module in _iter_modules():
        handle = getattr(module, "handle", None)
        if handle is None:
            continue
        try:
            handle(options)
        except Exception:
            logger.exception("CLI module %r handle() raised", module.__name__)
            raise
