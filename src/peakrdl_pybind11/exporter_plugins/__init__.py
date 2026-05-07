"""Exporter plugin auto-discovery.

This package is the seam that sibling units of the API overhaul use to
add codegen passes without modifying ``exporter.py``. Each plugin module
must export a ``register(exporter)`` function. The exporter calls
:func:`discover_plugins` once during ``__init__`` and the plugin's
``register`` is called immediately so it can hook into the exporter
(install Jinja filters, store references, etc.); plugins typically run
their codegen during ``export()`` via callbacks the exporter exposes.

If a plugin module fails to import it is logged and skipped — one bad
plugin must not take down the whole exporter for downstream users.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any

logger = logging.getLogger("peakrdl_pybind11.exporter_plugins")

__all__ = ["discover_plugins"]


def discover_plugins(exporter: Any) -> None:
    """Discover and register every exporter plugin module under this package.

    Args:
        exporter: The :class:`peakrdl_pybind11.exporter.Pybind11Exporter`
            instance. Each plugin's ``register`` callable receives this
            object so it can attach itself.
    """
    package_path = list(__path__)  # type: ignore[name-defined]
    package_name = __name__

    for info in pkgutil.iter_modules(package_path):
        if info.name.startswith("_"):
            # Underscore-prefixed modules are reserved for internals
            # (test fixtures, helpers); they don't expose ``register``.
            continue
        full_name = f"{package_name}.{info.name}"
        try:
            module = importlib.import_module(full_name)
        except Exception:
            logger.warning("failed to import exporter plugin %r", full_name, exc_info=True)
            continue

        register = getattr(module, "register", None)
        if register is None:
            logger.debug("exporter plugin %r has no register()", full_name)
            continue
        try:
            register(exporter)
        except Exception:
            logger.warning("exporter plugin %r register() raised", full_name, exc_info=True)
