"""Runtime support package for the PeakRDL-pybind11 generated API.

See ``docs/IDEAL_API_SKETCH.md`` for the full design.

This package houses sibling-unit modules that wire themselves into the
generated runtime via the registry seam in :mod:`._registry`. Each
sibling unit imports ``_registry`` and decorates its hook callables
(register enhancements, post-create hooks, master extensions, lazy node
attributes); the generated ``runtime.py`` fires those hooks at the
appropriate points.

Importing this package eagerly imports :mod:`._registry` and the bundled
sibling-unit modules so all registration happens at runtime-package load
time, before the generated module touches its register/field classes.
"""

from __future__ import annotations

from . import _registry, transactions  # noqa: F401  (side-effecting imports register hooks)

__all__ = [
    "_registry",
    "transactions",
]
