"""
Lightweight registration seam (Unit 1).

Generated modules call into this module at import time to attach
runtime-only Python features (rich repr, widgets, snapshots, ...) onto
the C++ register/field/memory classes.

Unit 1 owns this module; other units consume it. If Unit 1 has not
landed yet on the branch, the surface defined here is intentionally
minimal: a pair of `register_*_enhancement` functions plus a couple of
hook lists. Later units can extend the surface without breaking the
contract documented here.
"""

from __future__ import annotations

from collections.abc import Callable

# A "register enhancement" is any callable that takes a generated node
# class (Reg, Field, Mem, RegFile, AddrMap) and decorates it with extra
# Python-only attributes/methods. These run once per class at module
# import time.
_register_enhancements: list[Callable[[type], None]] = []

# A "master extension" runs once per generated SoC class (the top-level
# AddrMap). Used for cross-cutting features that need a handle on the
# whole tree -- for instance, the `watch()` method on snapshots or the
# `_repr_html_` registered on the SoC root.
_master_extensions: list[Callable[[type], None]] = []


def register_register_enhancement(fn: Callable[[type], None]) -> Callable[[type], None]:
    """Register *fn* to run on every generated register/field/mem/regfile/addrmap class.

    The function is also returned, so it can be used as a decorator.
    """
    _register_enhancements.append(fn)
    return fn


def register_master_extension(fn: Callable[[type], None]) -> Callable[[type], None]:
    """Register *fn* to run on every generated top-level SoC class.

    The function is also returned, so it can be used as a decorator.
    """
    _master_extensions.append(fn)
    return fn


def apply_register_enhancements(cls: type) -> None:
    """Apply every registered enhancement to *cls*. Called by generated modules."""
    for fn in _register_enhancements:
        fn(cls)


def apply_master_extensions(cls: type) -> None:
    """Apply every registered master-level extension to *cls*. Called by generated modules."""
    for fn in _master_extensions:
        fn(cls)


def _reset_for_tests() -> None:
    """Drop all registrations. Test-only escape hatch."""
    _register_enhancements.clear()
    _master_extensions.clear()


# Public re-exports for type-checkers / IDE completion.
__all__ = [
    "apply_master_extensions",
    "apply_register_enhancements",
    "register_master_extension",
    "register_register_enhancement",
]


# Side-effect badge alphabet shared across runtime modules. Kept here so
# a single import gets the canonical glyph table -- changing them in one
# place updates every renderer.
SIDE_EFFECT_BADGES: dict[str, str] = {
    "rclr": "⚠",          # ⚠ read-clears
    "singlepulse": "↻",    # ↻ single-pulse
    "sticky": "✱",         # ✱ sticky
    "volatile": "⚡",       # ⚡ hardware-volatile
}
