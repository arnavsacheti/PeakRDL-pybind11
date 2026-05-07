"""Exporter plugin discovery seam.

This is the minimal scaffolding for sibling Unit 1; once that lands it will
replace this module with a richer registry. The contract a plugin must
satisfy:

* Provide a callable ``register(exporter)`` that hooks any state it needs
  onto the exporter instance, **or**
* Subclass :class:`ExporterPlugin` and implement ``post_export(context)``,
  which is invoked after the core export has finished writing files.

Plugins are discovered by importing every module in this package and
collecting the modules that expose a ``register`` callable. Future Unit 1
will likely swap to entry-point discovery, but per-module registration
keeps this contract stable for in-tree plugins.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from systemrdl.node import AddrmapNode

    from peakrdl_pybind11.exporter import Nodes, Pybind11Exporter


@dataclass
class PluginContext:
    """State handed to each plugin's ``post_export`` hook.

    A small read-only snapshot of what the core exporter just produced.
    Plugins use this to write supplementary artifacts into ``output_dir``
    (and ``output_dir / soc_name``).
    """

    exporter: Pybind11Exporter
    top_node: AddrmapNode
    output_dir: Path
    soc_name: str
    nodes: Nodes
    options: dict[str, Any]


class ExporterPlugin(Protocol):
    """Plugin interface. Implementations may be modules or callables."""

    def post_export(self, ctx: PluginContext) -> None: ...


# Optional plugin instances registered programmatically via ``register_plugin``.
_REGISTERED: list[ExporterPlugin] = []


def register_plugin(plugin: ExporterPlugin) -> None:
    """Register an externally-constructed plugin instance."""
    if plugin not in _REGISTERED:
        _REGISTERED.append(plugin)


def discover_plugins() -> list[ExporterPlugin]:
    """Return every plugin known at the time of the call.

    Plugins are sourced from two places:

    * Modules under ``peakrdl_pybind11.exporter_plugins`` that expose a
      callable ``register(exporter_module)``. The returned object (or the
      module itself if ``register`` returns None) is the plugin.
    * Anything passed to :func:`register_plugin`.
    """
    plugins: list[ExporterPlugin] = list(_REGISTERED)
    pkg_path = Path(__file__).parent
    for mod_info in pkgutil.iter_modules([str(pkg_path)]):
        if mod_info.name.startswith("_"):
            continue
        full_name = f"{__name__}.{mod_info.name}"
        try:
            module = importlib.import_module(full_name)
        except Exception:  # pragma: no cover - defensive
            continue
        register: Callable[[Any], ExporterPlugin | None] | None = getattr(module, "register", None)
        if register is None:
            continue
        instance = register(module)
        plugin: ExporterPlugin = instance if instance is not None else module  # type: ignore[assignment]
        if plugin not in plugins:
            plugins.append(plugin)
    return plugins


def run_post_export(ctx: PluginContext) -> None:
    """Invoke ``post_export`` on every discovered plugin."""
    for plugin in discover_plugins():
        hook = getattr(plugin, "post_export", None)
        if hook is None:
            continue
        hook(ctx)


__all__ = [
    "ExporterPlugin",
    "PluginContext",
    "discover_plugins",
    "register_plugin",
    "run_post_export",
]
