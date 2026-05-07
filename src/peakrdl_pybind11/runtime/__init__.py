"""Runtime helpers layered on top of generated SoC modules.

This package hosts pure-Python adapters that decorate the C++-generated
register/memory classes with richer ergonomics (typed values, memory
views, observers, ...). Each unit is self-contained and degrades
gracefully if its sibling units (registry, info) are not yet present.
"""

from __future__ import annotations

from .mem_view import (
    MemView,
    MemWindow,
    enhance_mem_class,
    enhance_mem_instance,
)

__all__ = [
    "MemView",
    "MemWindow",
    "enhance_mem_class",
    "enhance_mem_instance",
]
