"""
Enhancement registries for the runtime API surface.

These registries are the minimal seam other Units (Unit 1 in particular) use
to attach methods, properties, and class-level conveniences onto the
generated descriptor classes for registers, fields, memories, and address
maps. The chip module collects entries during import and the generated
``apply_enhancements`` step iterates the registry once per class.

The registries themselves are intentionally tiny — module-level lists with a
decorator that appends. Higher-level wiring lives in the chip module and in
the unit that owns the descriptor classes; ``_registry`` is just storage.

This file is owned by Unit 1; Unit 9 ships a non-conflicting subset so that
``peakrdl_pybind11.runtime.wait_poll`` can import without depending on
sibling Units that have not landed yet. Sibling Units may extend this file;
please keep additions strictly additive.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Each enhancer takes ``(cls, metadata)`` and mutates the class in place.
# ``metadata`` is the per-class metadata dict the generator emits (the
# RDL path, address, width, etc.). Enhancers should be idempotent: an
# already-applied enhancer must be a no-op.
EnhancerFn = Callable[[type, dict[str, Any]], None]

_register_enhancers: list[EnhancerFn] = []
_field_enhancers: list[EnhancerFn] = []
_memory_enhancers: list[EnhancerFn] = []
_addrmap_enhancers: list[EnhancerFn] = []


def register_register_enhancement(fn: EnhancerFn) -> EnhancerFn:
    """Register *fn* as an enhancer applied to every generated Register class.

    The decorator returns *fn* unchanged so it can be used at module scope::

        @register_register_enhancement
        def _enhance(cls, metadata):
            cls.wait_for = wait_for

    The chip module is responsible for iterating
    :func:`get_register_enhancers` once per class at import time.
    """

    if fn not in _register_enhancers:
        _register_enhancers.append(fn)
    return fn


def register_field_enhancement(fn: EnhancerFn) -> EnhancerFn:
    """Register *fn* as an enhancer applied to every generated Field class."""

    if fn not in _field_enhancers:
        _field_enhancers.append(fn)
    return fn


def register_memory_enhancement(fn: EnhancerFn) -> EnhancerFn:
    """Register *fn* as an enhancer applied to every generated Memory class."""

    if fn not in _memory_enhancers:
        _memory_enhancers.append(fn)
    return fn


def register_addrmap_enhancement(fn: EnhancerFn) -> EnhancerFn:
    """Register *fn* as an enhancer applied to every generated AddrMap class."""

    if fn not in _addrmap_enhancers:
        _addrmap_enhancers.append(fn)
    return fn


def get_register_enhancers() -> tuple[EnhancerFn, ...]:
    """Return a snapshot of currently registered Register enhancers."""

    return tuple(_register_enhancers)


def get_field_enhancers() -> tuple[EnhancerFn, ...]:
    """Return a snapshot of currently registered Field enhancers."""

    return tuple(_field_enhancers)


def get_memory_enhancers() -> tuple[EnhancerFn, ...]:
    """Return a snapshot of currently registered Memory enhancers."""

    return tuple(_memory_enhancers)


def get_addrmap_enhancers() -> tuple[EnhancerFn, ...]:
    """Return a snapshot of currently registered AddrMap enhancers."""

    return tuple(_addrmap_enhancers)


def _reset_enhancers_for_testing() -> None:
    """Test-only hook: clear every enhancement registry.

    Intended for unit tests that need to assert how many enhancers a Unit
    contributes. Production code must never call this.
    """

    _register_enhancers.clear()
    _field_enhancers.clear()
    _memory_enhancers.clear()
    _addrmap_enhancers.clear()
