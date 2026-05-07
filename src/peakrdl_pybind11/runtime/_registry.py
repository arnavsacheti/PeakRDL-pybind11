"""Enhancement registration seam.

The full Unit 1 implementation owns the post-import wiring that walks
generated register/field classes and applies enhancements. Until that lands,
this module exposes the minimal hooks other runtime modules depend on so
they can register themselves at import time. Unit 1 will replace the body
without changing the interface.

Two collections are exposed:

* ``register_register_enhancement(fn)`` — ``fn(cls, fields_spec)`` is invoked
  for every generated register class. ``fields_spec`` is the
  ``{field_name: (lsb, width)}`` mapping the generator already builds (see
  ``templates/runtime.py.jinja``).
* ``register_field_enhancement(fn)`` — ``fn(cls)`` is invoked for every
  generated field class. ``cls`` is expected to expose ``lsb``, ``width`` and
  the future ``info`` namespace described in sketch §4.2.

Generated runtime code calls ``apply_enhancements(reg_classes, field_classes)``
once at module load. In tests the helpers can be invoked manually against
hand-rolled mock classes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

RegisterEnhancement = Callable[[type, Mapping[str, tuple[int, int]]], None]
FieldEnhancement = Callable[[type], None]

_register_enhancements: list[RegisterEnhancement] = []
_field_enhancements: list[FieldEnhancement] = []


def register_register_enhancement(fn: RegisterEnhancement) -> RegisterEnhancement:
    """Register a callable to enhance every generated register class.

    Returning the input makes ``register_register_enhancement`` usable as a
    decorator.
    """
    _register_enhancements.append(fn)
    return fn


def register_field_enhancement(fn: FieldEnhancement) -> FieldEnhancement:
    """Register a callable to enhance every generated field class."""
    _field_enhancements.append(fn)
    return fn


def apply_enhancements(
    register_classes: Mapping[type, Mapping[str, tuple[int, int]]],
    field_classes: Iterable[type],
) -> None:
    """Run all registered enhancements over the supplied classes.

    Called by the generated runtime once the native module has been
    imported. ``register_classes`` maps each register class to its
    ``{field_name: (lsb, width)}`` spec (already produced by the Jinja
    template). ``field_classes`` is a flat iterable of every field class.
    """
    for reg_cls, fields_spec in register_classes.items():
        for fn in _register_enhancements:
            fn(reg_cls, fields_spec)
    for field_cls in field_classes:
        for fn in _field_enhancements:
            fn(field_cls)


def _reset_for_tests() -> None:
    """Drop registered callbacks. Tests use this to isolate registrations."""
    _register_enhancements.clear()
    _field_enhancements.clear()
