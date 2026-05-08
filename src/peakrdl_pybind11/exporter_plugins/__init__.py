"""Exporter plugin auto-discovery + post-export dispatch.

This package is the seam that sibling units of the API overhaul use to
add codegen passes without modifying ``exporter.py``. There are two
discovery modes:

* **Side-effect ``register(exporter)``** — each plugin module exposes a
  ``register`` function that is called once during exporter construction.
  The function may install Jinja filters, store references on the
  exporter, etc. If it returns an object, that object becomes a plugin
  instance available to the post-export dispatch (next bullet).
* **Post-export ``post_export(ctx)``** — plugin instances may also
  implement ``post_export(ctx: PluginContext)``. After the main exporter
  finishes generating descriptors / bindings / runtime / stubs, every
  registered plugin's ``post_export`` is called in registration order.
  This is where late codegen (interrupt detection, schema.json, stubs
  enrichment) runs.

If a plugin module fails to import or its ``register``/``post_export``
raises, the failure is logged and the rest of the exporter continues.
One bad plugin must not take down the whole pipeline for downstream users.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("peakrdl_pybind11.exporter_plugins")

__all__ = [
    "PluginContext",
    "discover_plugins",
    "register_plugin",
    "registered_plugins",
    "run_post_export",
]


# ---------------------------------------------------------------------------
# Public dataclass passed to every plugin's ``post_export`` callback. Plain
# attributes only — sibling units that build a context manually (e.g. for
# a unit test) should not need keyword arguments to construct it.
# ---------------------------------------------------------------------------


@dataclass
class PluginContext:
    """Bundle handed to every ``plugin.post_export(ctx)`` callback.

    Attributes
    ----------
    exporter
        The :class:`peakrdl_pybind11.exporter.Pybind11Exporter` instance.
        Late codegen often needs the exporter's helpers (Jinja env,
        ``_pybind_name_from_node``, etc.).
    top_node
        The :class:`systemrdl.node.AddrmapNode` that anchors the export.
    output_dir
        Filesystem path where the generated artefacts live.
    soc_name
        The SoC name passed to ``Pybind11Exporter.export``.
    nodes
        Walker output from the main exporter (the ``Nodes`` TypedDict
        with addrmaps/regfiles/regs/fields/mems lists).
    options
        Free-form dict of CLI-derived options (``interrupt_pattern``,
        etc.). Plugins fish out their own keys.
    """

    exporter: Any
    top_node: Any
    output_dir: Any
    soc_name: str
    nodes: Any
    options: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Plugin instance registry
# ---------------------------------------------------------------------------


_plugin_instances: list[Any] = []


def register_plugin(plugin: Any) -> Any:
    """Register ``plugin`` (any object) for post-export dispatch.

    Idempotent: registering the same instance twice is a no-op.
    """
    if plugin is None:
        return plugin
    if plugin not in _plugin_instances:
        _plugin_instances.append(plugin)
    return plugin


def registered_plugins() -> list[Any]:
    """Return a snapshot of registered plugin instances (in order)."""
    return list(_plugin_instances)


# ---------------------------------------------------------------------------
# Discovery + dispatch
# ---------------------------------------------------------------------------


def discover_plugins(exporter: Any = None) -> list[Any]:
    """Discover and register every exporter plugin module under this package.

    For each plugin module:

    1. Import the module (skipping underscore-prefixed reserved names).
    2. Call its ``register(exporter)`` if present. The return value, if
       not ``None``, is treated as a plugin instance and added to the
       post-export registry.

    Returns the list of registered plugin instances (post this call) so
    test code can introspect what was discovered without needing access
    to the exporter.
    """
    package_path = list(__path__)  # type: ignore[name-defined]
    package_name = __name__

    for info in pkgutil.iter_modules(package_path):
        if info.name.startswith("_"):
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
            result = register(exporter)
        except Exception:
            logger.warning("exporter plugin %r register() raised", full_name, exc_info=True)
            continue
        register_plugin(result)

    return registered_plugins()


def run_post_export(ctx: PluginContext) -> None:
    """Fire every registered plugin's ``post_export(ctx)`` callback.

    Failures in one plugin are logged but do not stop the dispatch chain
    — the main exporter has already declared success at this point.
    """
    for plugin in list(_plugin_instances):
        post_export = getattr(plugin, "post_export", None)
        if post_export is None:
            continue
        try:
            post_export(ctx)
        except Exception:
            logger.warning("plugin %r post_export() raised", plugin, exc_info=True)
