"""Runtime support package for the PeakRDL-pybind11 generated API.

See ``docs/IDEAL_API_SKETCH.md`` for the full design.

Sibling-unit modules placed inside this package are **auto-imported** at
package load time. This is the seam that lets each piece of the API
overhaul (snapshots, observers, interrupt detection, bus policies,
widgets, etc.) wire itself in without ``runtime.py.jinja`` having to
know about it.

Auto-import contract:

* Modules whose name starts with ``_`` (e.g. ``_default_shims``,
  ``_registry``) are part of the runtime's own implementation and are
  imported first. They are intentionally underscore-prefixed so the
  generated ``runtime.py`` can rely on the **defaults registering before
  any sibling unit**.
* Sibling-unit modules use plain names (e.g. ``snapshots``, ``bus_policies``).
* If a sibling module fails to import, the failure is logged and the
  package continues — one bad sibling must not break the whole runtime
  surface for downstream users.

The package re-exports ``RegisterValue`` / ``FieldValue`` from
:mod:`peakrdl_pybind11.runtime.values` so the new API vocabulary is
available from a stable import path::

    from peakrdl_pybind11.runtime import RegisterValue, FieldValue

These are the immutable, hashable ``int`` subclasses that the default
shim (see :mod:`peakrdl_pybind11.runtime._default_shims`) emits from
``register.read()`` / ``field.read()``. The legacy
``peakrdl_pybind11.int_types.RegisterInt`` / ``FieldInt`` types remain
importable for code that constructs them directly.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from . import _registry
from .values import FieldValue, RegisterValue

logger = logging.getLogger("peakrdl_pybind11.runtime")

__all__ = [
    "FieldValue",
    "RegisterValue",
    "_registry",
]


def _reexport_public_api() -> None:
    """Pull every sibling unit's public surface into this package namespace.

    Sibling tests and downstream users import names directly from
    ``peakrdl_pybind11.runtime``; auto-discovery alone loads modules but
    does not re-export their symbols. Each unit owns its public list, so
    we walk the loaded submodules and copy their ``__all__`` entries up.
    """
    import sys

    pkg = sys.modules[__name__]
    # Names defined in the canonical seam (``_registry``) always win. Any
    # sibling module that shadowed them was a pre-merge stub or a sibling
    # function that should be imported from its module path explicitly
    # rather than via the runtime package re-export.
    canonical = {n for n in vars(_registry) if not n.startswith("_")} | {
        "FieldValue",
        "RegisterValue",
        "_registry",
    }
    for info in pkgutil.iter_modules(__path__):  # type: ignore[name-defined]
        if info.name.startswith("_"):
            continue
        full = f"{__name__}.{info.name}"
        mod = sys.modules.get(full)
        if mod is None:
            continue
        names = getattr(mod, "__all__", None)
        if names is None:
            # Fall back: every public, module-defined class/function. Modules
            # that omitted ``__all__`` (snapshot, info, routing, bits) still
            # need to surface their public types to ``peakrdl_pybind11.runtime``.
            names = [
                n
                for n, v in vars(mod).items()
                if not n.startswith("_") and getattr(v, "__module__", None) == full
            ]
        for name in names:
            if name.startswith("_") or name in __all__ or name in canonical:
                continue
            value = getattr(mod, name, None)
            if value is None:
                continue
            setattr(pkg, name, value)
            __all__.append(name)


def _auto_import_modules() -> None:
    """Import every submodule under this package.

    Underscore-prefixed modules (the runtime's own internals) are imported
    first so default hooks register before sibling-unit hooks. A failed
    sibling import is logged but never raised — one broken sibling unit
    must not poison the whole runtime.
    """
    package_path = list(__path__)  # type: ignore[name-defined]
    package_name = __name__

    # Two passes: underscore-prefixed first, plain names second. Within
    # each pass the order is whatever ``pkgutil.iter_modules`` returns
    # (alphabetical on every reasonable filesystem). Stable enough for
    # default-then-sibling layering; sibling-unit ordering is not part
    # of the contract.
    found = list(pkgutil.iter_modules(package_path))
    underscore = [info for info in found if info.name.startswith("_")]
    rest = [info for info in found if not info.name.startswith("_")]

    for info in underscore + rest:
        full_name = f"{package_name}.{info.name}"
        try:
            importlib.import_module(full_name)
        except Exception:
            logger.warning("failed to import runtime module %r", full_name, exc_info=True)


_auto_import_modules()
_reexport_public_api()
