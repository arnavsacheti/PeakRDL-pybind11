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

The package also re-exports ``RegisterValue`` / ``FieldValue`` as aliases
for ``RegisterInt`` / ``FieldInt`` so the new API vocabulary is available
from a stable import path::

    from peakrdl_pybind11.runtime import RegisterValue, FieldValue
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from ..int_types import FieldInt as FieldValue
from ..int_types import RegisterInt as RegisterValue
from . import _registry

logger = logging.getLogger("peakrdl_pybind11.runtime")

__all__ = [
    "FieldValue",
    "RegisterValue",
    "_registry",
]


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
