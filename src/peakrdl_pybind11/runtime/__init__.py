"""Runtime support package for the PeakRDL-pybind11 generated API.

See ``docs/IDEAL_API_SKETCH.md`` for the full design. This package is
the seam that sibling units of the API overhaul plug into; right now
it exposes only the common error type and the forward-compat
``RegisterValue`` / ``FieldValue`` aliases for ``RegisterInt`` /
``FieldInt``.

Sibling units extend this package by dropping additional modules in
here. We auto-import every plain-named submodule at load so a unit
that ships, say, ``snapshots.py`` is wired up without
``runtime.py.jinja`` (the per-SoC generated module) needing to know
about it. Underscore-prefixed modules are reserved for runtime
internals and load first; one bad sibling never poisons the whole
package — failures are logged and skipped.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from ..int_types import FieldInt as FieldValue  # noqa: F401  (re-export)
from ..int_types import RegisterInt as RegisterValue  # noqa: F401  (re-export)
from ._errors import NotSupportedError  # noqa: F401  (re-export)

logger = logging.getLogger("peakrdl_pybind11.runtime")

__all__ = [
    "FieldValue",
    "NotSupportedError",
    "RegisterValue",
]


def _auto_import_modules() -> None:
    """Import every submodule under this package.

    Underscore-prefixed modules (the runtime's own internals) are imported
    first so default hooks register before sibling-unit hooks. A failed
    sibling import is logged but never raised — one broken sibling unit
    must not poison the whole runtime.
    """
    package_path = list(__path__)
    package_name = __name__

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
