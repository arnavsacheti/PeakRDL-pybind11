"""
Central runtime hook registry.

This module is the **stable seam** that every sibling unit of the API
overhaul plugs into. Sibling units register callables here and the
generated runtime fires them at well-defined points; nothing in
``runtime.py.jinja`` (the per-SoC generated module) needs to know which
units are present.

Five registries live here:

* ``_register_enhancements`` — wrap/extend a generated **register** class
  with ``apply(cls, fields_spec)``.
* ``_field_enhancements`` — same idea for generated **field** classes.
  (Split from register enhancements because the existing template uses
  different per-field metadata; sibling units almost always target one or
  the other, not both.)
* ``_post_create_hooks`` — fire after ``soc = MySoc.create(...)``.
* ``_master_extensions`` — fire when a master is attached to a SoC.
* ``_node_attributes`` — lazy attribute factories attached to the node
  base class. Each factory returns the value for the attribute on first
  access; later units add ``.info``, ``.snapshot``, ``.watch`` etc.

All registration is thread-safe: a single ``threading.Lock`` guards each
mutation. Callable invocation is *not* serialized — the caller is
expected to hold any cross-cutting locks.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger("peakrdl_pybind11.runtime.registry")

# ---------------------------------------------------------------------------
# Type aliases. Kept loose so sibling units can register callables that take
# extra optional kwargs without breaking the registry.
# ---------------------------------------------------------------------------

RegisterEnhancement = Callable[[type, dict], None]
FieldEnhancement = Callable[[type], None]
PostCreateHook = Callable[[Any], None]
MasterExtension = Callable[[Any], None]
NodeAttributeFactory = Callable[[Any], Any]

# ---------------------------------------------------------------------------
# Internal registries. Lists preserve registration order so siblings can
# reason about composition: defaults register first, sibling units extend.
# ---------------------------------------------------------------------------

_register_enhancements: list[RegisterEnhancement] = []
_field_enhancements: list[FieldEnhancement] = []
_post_create_hooks: list[PostCreateHook] = []
_master_extensions: list[MasterExtension] = []
_node_attributes: dict[str, NodeAttributeFactory] = {}

# Identity sets so re-registration of the same callable is idempotent.
# Without these, importing a sibling module twice (rare, but happens with
# ``importlib.reload`` and the test harness) would double-register hooks.
_seen_register_enhancements: set[int] = set()
_seen_field_enhancements: set[int] = set()
_seen_post_create_hooks: set[int] = set()
_seen_master_extensions: set[int] = set()

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Decorator API
# ---------------------------------------------------------------------------


def register_register_enhancement(fn: RegisterEnhancement) -> RegisterEnhancement:
    """Register ``fn`` to be called for every generated register class.

    The callable receives ``(cls, fields_spec)`` where ``fields_spec`` is
    the per-register ``{field_name: (lsb, width)}`` dict produced by the
    template. Idempotent on identity: re-registering the same function
    is a no-op.
    """
    with _lock:
        if id(fn) in _seen_register_enhancements:
            return fn
        _seen_register_enhancements.add(id(fn))
        _register_enhancements.append(fn)
    return fn


def register_field_enhancement(fn: FieldEnhancement) -> FieldEnhancement:
    """Register ``fn`` to be called for every generated field class.

    The callable receives the class. Use this for field-only behaviour
    (typed read return value, raw accessors, etc.).
    """
    with _lock:
        if id(fn) in _seen_field_enhancements:
            return fn
        _seen_field_enhancements.add(id(fn))
        _field_enhancements.append(fn)
    return fn


def register_post_create(fn: PostCreateHook) -> PostCreateHook:
    """Register ``fn`` to fire after a SoC instance is created.

    Sibling units use this to attach observers, snapshot tooling,
    interrupt detection, etc.
    """
    with _lock:
        if id(fn) in _seen_post_create_hooks:
            return fn
        _seen_post_create_hooks.add(id(fn))
        _post_create_hooks.append(fn)
    return fn


def register_master_extension(fn: MasterExtension) -> MasterExtension:
    """Register ``fn`` to fire when a master attaches to a SoC.

    Sibling units use this to wire bus policies (retry, cache,
    barriers, tracing) into the master.
    """
    with _lock:
        if id(fn) in _seen_master_extensions:
            return fn
        _seen_master_extensions.add(id(fn))
        _master_extensions.append(fn)
    return fn


def register_node_attribute(name: str) -> Callable[[NodeAttributeFactory], NodeAttributeFactory]:
    """Decorator factory that registers a lazy node attribute.

    The decorated function is called with the node instance the first
    time the attribute is accessed; the result is cached on the instance.
    Re-registering the same name silently overwrites the previous factory
    (the new sibling unit "wins"). A debug log records the replacement.
    """

    def decorator(fn: NodeAttributeFactory) -> NodeAttributeFactory:
        with _lock:
            if name in _node_attributes and _node_attributes[name] is not fn:
                logger.debug("node attribute %r being overwritten by %r", name, fn)
            _node_attributes[name] = fn
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Apply API — the generated runtime.py imports and calls these.
# ---------------------------------------------------------------------------


def apply_register_enhancements(cls: type, metadata: dict) -> None:
    """Run every registered register enhancement against ``cls``."""
    # Snapshot under the lock so a long-running enhancement doesn't block
    # later registrations from sibling units imported on demand.
    with _lock:
        funcs = list(_register_enhancements)
    for fn in funcs:
        try:
            fn(cls, metadata)
        except Exception:
            logger.exception("register enhancement %r raised on %r", fn, cls)
            raise


def apply_field_enhancements(cls: type) -> None:
    """Run every registered field enhancement against ``cls``."""
    with _lock:
        funcs = list(_field_enhancements)
    for fn in funcs:
        try:
            fn(cls)
        except Exception:
            logger.exception("field enhancement %r raised on %r", fn, cls)
            raise


def fire_post_create_hooks(soc: Any) -> None:
    """Fire every registered post-create hook against ``soc``."""
    with _lock:
        hooks = list(_post_create_hooks)
    for fn in hooks:
        try:
            fn(soc)
        except Exception:
            logger.exception("post-create hook %r raised on %r", fn, soc)
            raise


def fire_master_extensions(master: Any) -> None:
    """Fire every registered master extension against ``master``."""
    with _lock:
        funcs = list(_master_extensions)
    for fn in funcs:
        try:
            fn(master)
        except Exception:
            logger.exception("master extension %r raised on %r", fn, master)
            raise


def attach_node_attributes(node_class: type | None) -> None:
    """Wire registered lazy attributes onto ``node_class``.

    If ``node_class`` is ``None`` (no shared base in the generated module
    yet — the current state during early API-overhaul units), this is a
    no-op so siblings can call it safely.
    """
    if node_class is None:
        return

    with _lock:
        attrs = dict(_node_attributes)

    for name, factory in attrs.items():
        # Skip if the class already defines the attribute (sibling unit
        # explicitly defined it on the class, factory is a fallback).
        if name in node_class.__dict__:
            continue

        def _make_property(fn: NodeAttributeFactory, attr_name: str) -> property:
            cache_key = f"_node_attr_cache_{attr_name}"

            def getter(self: Any) -> Any:
                cached = self.__dict__.get(cache_key)
                if cached is not None:
                    return cached
                value = fn(self)
                self.__dict__[cache_key] = value
                return value

            return property(getter)

        setattr(node_class, name, _make_property(factory, name))


# ---------------------------------------------------------------------------
# Test / introspection helpers. Sibling units do **not** depend on these;
# they exist so the registry test module can verify behaviour without
# reaching into private state.
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    """Clear every registry. Test-only; do **not** call from production."""
    with _lock:
        _register_enhancements.clear()
        _field_enhancements.clear()
        _post_create_hooks.clear()
        _master_extensions.clear()
        _node_attributes.clear()
        _seen_register_enhancements.clear()
        _seen_field_enhancements.clear()
        _seen_post_create_hooks.clear()
        _seen_master_extensions.clear()


def _snapshot() -> dict[str, Any]:
    """Return a shallow snapshot of every registry. Test-only."""
    with _lock:
        return {
            "register_enhancements": list(_register_enhancements),
            "field_enhancements": list(_field_enhancements),
            "post_create_hooks": list(_post_create_hooks),
            "master_extensions": list(_master_extensions),
            "node_attributes": dict(_node_attributes),
        }
