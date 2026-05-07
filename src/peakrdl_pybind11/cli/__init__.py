"""CLI extension auto-discovery.

This package is the seam sibling units of the API overhaul use to attach
extra arguments to the ``peakrdl pybind11`` invocation, preempt the
export phase (``--diff``/``--replay`` do not need to generate code), and
run post-export handlers (``--explore``/``--watch``).

Each module inside this package may export any subset of:

* ``add_arguments(arg_group)`` — called from
  :py:meth:`Exporter.add_exporter_arguments`. ``arg_group`` is the same
  argparse action container PeakRDL hands the exporter.
* ``try_handle(options) -> bool`` — preempt the normal export. If a CLI
  module returns ``True`` the exporter skips export for this invocation
  (used by ``--diff``/``--replay``). The first module that returns
  ``True`` wins; later modules' ``try_handle`` is not called.
* ``handle(options)`` — legacy entry point; kept as an alias for
  ``run_post_handlers`` for backward compatibility with sibling units
  that landed before ``try_handle`` existed.
* ``post_handle(options)`` — called after the export finishes (used by
  ``--explore``/``--watch``). Failures are logged and re-raised so the
  user sees them.

Module names beginning with ``_`` are treated as internal and skipped.

Despite the package name, these are *not* new top-level subcommands of
``peakrdl``; the ``peakrdl pybind11`` subcommand is owned by
``__peakrdl__.py``. ``cli`` modules are sibling-unit extensions that
compose with that single subcommand.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import pkgutil
from typing import Any

logger = logging.getLogger("peakrdl_pybind11.cli")

__all__ = [
    "discover_subcommands",
    "iter_modules",
    "run_handlers",
    "run_post_handlers",
    "try_handle",
]


def iter_modules() -> list[Any]:
    """Return every imported sibling-unit CLI module under this package."""
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


# Legacy private alias — sibling units that imported ``_iter_modules``
# before ``iter_modules`` was promoted public continue to work.
_iter_modules = iter_modules


def discover_subcommands(arg_group: argparse._ActionsContainer) -> None:
    """Run ``add_arguments`` from every sibling-unit CLI module."""
    for module in iter_modules():
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
    """Run pre-export ``try_handle`` from every sibling-unit CLI module.

    Returns ``True`` as soon as one module claims the invocation (e.g.
    ``--diff`` does not need a fresh export — it operates on existing
    snapshot files). Returns ``False`` when no module claims it; the
    exporter then runs its normal pipeline.

    A CLI module may expose its preempt logic as either ``try_handle``
    (preferred — explicit name) or as a ``handle`` that returns a
    truthy value (legacy — Unit 24's ``--diff``/``--replay`` modules).
    """
    for module in iter_modules():
        fn = getattr(module, "try_handle", None) or getattr(module, "handle", None)
        if fn is None:
            continue
        try:
            handled = fn(options)
        except Exception:
            logger.exception("CLI module %r try_handle() raised", module.__name__)
            raise
        if handled:
            return True
    return False


def run_post_handlers(options: argparse.Namespace) -> None:
    """Run post-export ``post_handle`` from every sibling-unit CLI module.

    Called after :py:meth:`Exporter.do_export` finishes its primary
    export (used by ``--explore``/``--watch``). Handler exceptions are
    logged and re-raised — the user explicitly asked for the post-export
    behaviour, so silent failure is wrong.
    """
    for module in iter_modules():
        for hook_name in ("post_handle", "handle"):
            fn = getattr(module, hook_name, None)
            if fn is None:
                continue
            try:
                fn(options)
            except Exception:
                logger.exception("CLI module %r %s() raised", module.__name__, hook_name)
                raise
            break


# Legacy alias — sibling units (and ``__peakrdl__.py`` before it was
# updated) call ``run_handlers`` to mean the post-export hook chain.
run_handlers = run_post_handlers
