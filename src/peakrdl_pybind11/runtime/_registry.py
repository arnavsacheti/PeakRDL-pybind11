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
from collections.abc import Callable
from typing import Any

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

# Side-effect badge glyphs shared across the runtime (widgets, repr, error
# messages). Keyed by the canonical RDL effect name. Sibling units (notably
# ``runtime/widgets.py``) read this to render ⚠ rclr / ↻ singlepulse / ✱
# sticky / ⚡ volatile inline with field metadata. Per sketch §5.1.
SIDE_EFFECT_BADGES: dict[str, str] = {
    "rclr": "⚠",  # ⚠
    "singlepulse": "↻",  # ↻
    "sticky": "✱",  # ✱
    "volatile": "⚡",  # ⚡
}

# Identity sets so re-registration of the same callable is idempotent.
# Without these, importing a sibling module twice (rare, but happens with
# ``importlib.reload`` and the test harness) would double-register hooks.
_seen_register_enhancements: set[int] = set()
_seen_field_enhancements: set[int] = set()
_seen_post_create_hooks: set[int] = set()
_seen_master_extensions: set[int] = set()

_lock = threading.Lock()


def _register_unique(fn: Callable, store: list, seen: set[int]) -> None:
    """Append ``fn`` to ``store`` once, guarded by ``_lock``."""
    with _lock:
        if id(fn) in seen:
            return
        seen.add(id(fn))
        store.append(fn)


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
    _register_unique(fn, _register_enhancements, _seen_register_enhancements)
    return fn


def register_field_enhancement(fn: FieldEnhancement) -> FieldEnhancement:
    """Register ``fn`` to be called for every generated field class.

    The callable receives the class. Use this for field-only behaviour
    (typed read return value, raw accessors, etc.).
    """
    _register_unique(fn, _field_enhancements, _seen_field_enhancements)
    return fn


def register_post_create(fn: PostCreateHook) -> PostCreateHook:
    """Register ``fn`` to fire after a SoC instance is created.

    Sibling units use this to attach observers, snapshot tooling,
    interrupt detection, etc.
    """
    _register_unique(fn, _post_create_hooks, _seen_post_create_hooks)
    return fn


def register_master_extension(fn: MasterExtension) -> MasterExtension:
    """Register ``fn`` to fire when a master attaches to a SoC.

    Sibling units use this to wire bus policies (retry, cache,
    barriers, tracing) into the master.
    """
    _register_unique(fn, _master_extensions, _seen_master_extensions)
    return fn


# Named master extensions — for sibling units (notably bus_policies) that
# need to attach a result-returning factory to a specific master and
# retrieve the bundle later by a stable name.
_named_master_extensions: dict[str, Callable[[Any], Any]] = {}


def register_named_master_extension(name: str, fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Register a result-returning master extension under ``name``.

    Distinct from :func:`register_master_extension`: that one fires every
    registered hook for side effects; this one is keyed so callers can
    invoke a specific bundle factory and capture the result.
    """
    with _lock:
        if name in _named_master_extensions and _named_master_extensions[name] is not fn:
            logger.debug("named master extension %r being overwritten", name)
        _named_master_extensions[name] = fn
    return fn


def attach_master_extension(name: str, master: Any) -> Any:
    """Invoke a named master extension factory against ``master``.

    Returns whatever the factory returns (typically a policy bundle bound
    to the master). Raises :class:`KeyError` if no extension is registered
    under ``name``.
    """
    fn = get_master_extension_factory(name)
    return fn(master)


def get_master_extension_factory(name: str) -> Callable[[Any], Any]:
    """Return the named master extension factory.

    Raises :class:`KeyError` if no extension is registered under ``name``.
    Useful when the caller needs to inspect or re-bind the factory before
    invoking it.
    """
    with _lock:
        fn = _named_master_extensions.get(name)
    if fn is None:
        raise KeyError(f"no master extension registered under name {name!r}")
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


def _fire(store: list, label: str, target: Any, *args: Any) -> None:
    """Snapshot ``store`` under ``_lock`` and invoke each callable on the targets.

    The snapshot lets a long-running hook avoid blocking new
    registrations from sibling units imported on demand.

    Per-hook failures are logged and **swallowed** (Django-signal-style
    isolation) so one misbehaving sibling cannot poison the whole
    invocation chain. This matters because sibling units register
    speculative attach helpers (``attach_trace``, ``install``,
    ``attach_post_create``) that may not apply to every target shape;
    silently skipping them is the right policy when the target is a
    stub object or a slotted generated class without ``__dict__``.
    """
    with _lock:
        funcs = list(store)
    for fn in funcs:
        try:
            fn(target, *args)
        except Exception:
            logger.exception("%s %r raised on %r", label, fn, target)


def apply_register_enhancements(cls: type, metadata: dict) -> None:
    """Run every registered register enhancement against ``cls``."""
    _fire(_register_enhancements, "register enhancement", cls, metadata)


def apply_field_enhancements(cls: type) -> None:
    """Run every registered field enhancement against ``cls``."""
    _fire(_field_enhancements, "field enhancement", cls)


def apply_enhancements(
    register_classes: dict[type, dict] | None = None,
    field_classes: list[type] | None = None,
) -> None:
    """Apply every registered enhancement to multiple classes at once.

    Convenience wrapper used by sibling tests that drive the seam from
    Python (the generated ``runtime.py`` calls the per-class helpers
    directly). ``register_classes`` maps each register class to its
    metadata dict; ``field_classes`` is a flat list of field classes.
    """
    for cls, metadata in (register_classes or {}).items():
        apply_register_enhancements(cls, metadata)
    for cls in field_classes or []:
        apply_field_enhancements(cls)


def fire_post_create_hooks(soc: Any) -> None:
    """Fire every registered post-create hook against ``soc``."""
    _fire(_post_create_hooks, "post-create hook", soc)


def fire_master_extensions(master: Any) -> None:
    """Fire every registered master extension against ``master``."""
    _fire(_master_extensions, "master extension", master)


def get_register_enhancers() -> list[RegisterEnhancement]:
    """Return a snapshot of currently-registered register enhancements."""
    with _lock:
        return list(_register_enhancements)


def get_field_enhancers() -> list[FieldEnhancement]:
    """Return a snapshot of currently-registered field enhancements."""
    with _lock:
        return list(_field_enhancements)


def get_post_create_hooks() -> list[PostCreateHook]:
    """Return a snapshot of currently-registered post-create hooks."""
    with _lock:
        return list(_post_create_hooks)


def get_master_extensions() -> list[MasterExtension]:
    """Return a snapshot of currently-registered master extensions."""
    with _lock:
        return list(_master_extensions)


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
