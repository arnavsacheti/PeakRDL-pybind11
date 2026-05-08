Runtime Hook Registry
=====================

.. note::

   This page describes the **stable extension seam** that ships with the
   ``exp/api_overhaul`` line. Library extenders and downstream tooling
   authors hook into generated SoCs through this seam without modifying
   the Jinja templates.

Overview
--------

``peakrdl_pybind11.runtime._registry`` is the central seam for every
sibling unit in the API overhaul. The generated ``runtime.py`` for each
SoC walks the registries on import and fires hooks at well-defined
points; nothing in ``runtime.py.jinja`` (the per-SoC generated module)
needs to know which sibling units are present.

Sibling-unit modules placed inside ``peakrdl_pybind11.runtime`` are
**auto-imported** at package load. Each unit registers its hooks during
import; the generated runtime then walks the registry and applies them.
The result: adding a new behaviour (snapshots, observers, interrupt
detection, bus policies, widgets) is an additive drop-in. Templates and
existing units stay untouched.

The registry exposes five hook types, plus a small support API for
named master extensions, snapshot-based introspection, and the shared
side-effect badge dictionary.

The five hook types
-------------------

Each hook type has a decorator. Re-registering the same callable object
is a no-op — every store deduplicates by ``id(fn)``.

.. note::

   Idempotency is by **identity**, not equality. Two lambdas with the
   same body are different objects and will both register. Pass a named
   function if you need stable identity across re-imports.

``@register_register_enhancement``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Signature: ``fn(register_class, metadata: dict) -> None``.

Fires once per generated register class as ``runtime.py`` walks
``_REGISTER_FIELDS``. The ``metadata`` dict carries the per-register
field spec, writability map, and (optionally) a flag/enum type:

.. code-block:: python

   from peakrdl_pybind11.runtime._registry import register_register_enhancement

   @register_register_enhancement
   def attach_field_spec(cls, metadata):
       cls._field_spec = metadata["fields"]
       cls._writable = metadata["writable"]

``@register_field_enhancement``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Signature: ``fn(field_class) -> None``.

Fires once per generated field class. Use this for field-only behaviour
(typed read return values, raw accessors, repr customisation):

.. code-block:: python

   from peakrdl_pybind11.runtime._registry import register_field_enhancement

   @register_field_enhancement
   def attach_repr(cls):
       cls.__repr__ = lambda self: f"<{cls.__name__} value={int(self):#x}>"

``@register_post_create``
^^^^^^^^^^^^^^^^^^^^^^^^^

Signature: ``fn(soc) -> None``.

Fires once after ``soc = MySoc.create(...)`` in the generated module.
Sibling units use this to attach observers, snapshot tooling, or
interrupt-group descriptors:

.. code-block:: python

   from peakrdl_pybind11.runtime._registry import register_post_create

   @register_post_create
   def install_audit_log(soc):
       soc._audit_log = []

``@register_master_extension``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Signature: ``fn(master) -> None``.

Fires when a master is attached to a SoC (the generated ``wrap_master``
helper calls ``fire_master_extensions(master)`` after construction).
Sibling units use this to wire bus policies, retry decorators, or
tracing into the master:

.. code-block:: python

   from peakrdl_pybind11.runtime._registry import register_master_extension

   @register_master_extension
   def install_retry(master):
       master._retry_budget = 3

``@register_node_attribute("name")``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Signature: ``fn(node_instance) -> Any``.

Registers a **lazy attribute factory** keyed by name. The function fires
on first attribute access; the result is cached on the instance. Sibling
units add ``.info``, ``.snapshot``, ``.watch``, etc. through this seam:

.. code-block:: python

   from peakrdl_pybind11.runtime._registry import register_node_attribute

   @register_node_attribute("info")
   def make_info(node):
       return InfoAccessor(node)

Re-registering an existing name silently overwrites the previous factory
(the new sibling unit "wins") and emits a debug log line.

Named master extensions
-----------------------

For sibling units that need to attach a result-returning factory to a
specific master and retrieve the bundle later, the registry exposes a
**named** variant. Distinct from ``register_master_extension``: that one
fires every registered hook for side effects; the named variant is keyed
so callers can invoke a specific bundle factory and capture the return
value.

.. code-block:: python

   from peakrdl_pybind11.runtime._registry import (
       register_named_master_extension,
       attach_master_extension,
       get_master_extension_factory,
   )

   def build_bus_policies(master):
       return BusPolicyBundle(master, retry=3, cache=True)

   register_named_master_extension("bus_policies", build_bus_policies)

   bundle = attach_master_extension("bus_policies", master)
   factory = get_master_extension_factory("bus_policies")  # introspection

``attach_master_extension`` raises :class:`KeyError` if no extension is
registered under ``name``. ``get_master_extension_factory`` does the
same — useful when test code wants to inspect or rebind the factory
before invoking it. Re-registering an existing name overwrites the
previous factory and emits a debug log line.

Apply, fire, and snapshot helpers
---------------------------------

The generated ``runtime.py`` invokes the registry through a small
public surface. Sibling units calling these from Python (rather than
from generated code) is also supported.

Apply enhancements to classes
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

* ``apply_register_enhancements(cls, metadata)`` — run every registered
  register enhancement against ``cls``.
* ``apply_field_enhancements(cls)`` — run every registered field
  enhancement against ``cls``.
* ``apply_enhancements(register_classes={...}, field_classes=[...])`` —
  convenience wrapper that walks both at once. ``register_classes`` is a
  dict mapping register class to its metadata dict; ``field_classes`` is
  a flat list of field classes.

Fire instance hooks
^^^^^^^^^^^^^^^^^^^

* ``fire_post_create_hooks(soc)`` — fire every registered post-create
  hook against ``soc``.
* ``fire_master_extensions(master)`` — fire every registered master
  extension against ``master``.

Snapshot getters (introspection / testing)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

* ``get_register_enhancers()``
* ``get_field_enhancers()``
* ``get_post_create_hooks()``
* ``get_master_extensions()``

Each returns a fresh list of currently-registered callables. Snapshot
copies, not live references — mutating the returned list does not
affect the registry. Useful when a test needs to assert which sibling
units have wired themselves in.

Hook isolation
--------------

The internal ``_fire`` helper is **log-and-continue**, not log-and-raise.
One misbehaving sibling cannot poison the dispatch chain.

.. code-block:: python

   def _fire(store, label, target, *args):
       with _lock:
           funcs = list(store)
       for fn in funcs:
           try:
               fn(target, *args)
           except Exception:
               logger.exception("%s %r raised on %r", label, fn, target)

This is Django-signal-style hook isolation. The rationale:

* Sibling units register speculative attach helpers that may not apply
  to every target shape (a stub object, a slotted generated class
  without ``__dict__``).
* Silently skipping a misbehaving hook is the right policy when the
  alternative is breaking unrelated sibling units that share the same
  fire site.

Hook implementers should still catch their own exceptions for clarity,
but the runtime is robust to misbehaving sibling units. The traceback
goes through the registry logger ``peakrdl_pybind11.runtime.registry``.

Auto-discovery
--------------

Modules placed inside ``peakrdl_pybind11.runtime`` are auto-imported at
package load. The package ``__init__.py`` walks
``pkgutil.iter_modules`` in two passes:

1. **Underscore-prefixed first.** Modules whose name starts with ``_``
   (notably ``_registry`` and ``_default_shims``) are imported before
   any sibling unit. They are intentionally underscore-prefixed so the
   generated ``runtime.py`` can rely on the **defaults registering
   before any sibling unit**.
2. **Plain names second.** Sibling-unit modules use plain names
   (``snapshots``, ``bus_policies``, ``observers``, etc.).

If a sibling module fails to import, the failure is logged but never
raised — one broken sibling unit must not poison the whole runtime
surface for downstream users.

Within each pass, order is whatever ``pkgutil.iter_modules`` returns
(alphabetical on every reasonable filesystem). Stable enough for
default-then-sibling layering; sibling-unit ordering is **not** part of
the contract. Hooks that depend on another sibling running first should
look for the side effect (an attribute on the target, a flag in the
metadata) rather than rely on registration order.

Re-export contract
------------------

``peakrdl_pybind11.runtime.__init__.py`` re-exports public names from
sibling modules so downstream users can import them through a stable
path (``from peakrdl_pybind11.runtime import Snapshot, InterruptGroup``).
The canonical seam (``_registry``) wins when names collide.

The rules:

* The **canonical** name set is built from the public names in
  ``_registry`` plus the literals ``FieldValue``, ``RegisterValue``,
  ``_registry``. A sibling module that exports a name already in the
  canonical set is **not** re-exported under that name; the seam wins.
  (Pre-merge stubs that shadowed seam names should be imported from
  their module path explicitly rather than via the package re-export.)
* Underscore-prefixed names are skipped.
* If a sibling module defines ``__all__``, only those entries are
  re-exported.
* If a sibling module omits ``__all__``, every public, **module-defined**
  class or function is re-exported (filtered by
  ``getattr(value, "__module__", None) == full``). This catches modules
  like ``snapshot``, ``info``, ``routing``, and ``bits`` that surface
  public types without maintaining an explicit ``__all__``.

The end result: ``Snapshot``, ``InterruptGroup``, and friends appear as
attributes on ``peakrdl_pybind11.runtime`` even though they live in
sibling modules.

Side-effect badges
------------------

The registry module defines ``SIDE_EFFECT_BADGES``: a dict keyed by the
canonical RDL effect name, mapping to a single-glyph string. Used by
the bundled widgets renderer and reusable by downstream tooling that
wants the same visual vocabulary inline with field metadata.

.. code-block:: python

   from peakrdl_pybind11.runtime._registry import SIDE_EFFECT_BADGES

   SIDE_EFFECT_BADGES["rclr"]         # warning glyph
   SIDE_EFFECT_BADGES["singlepulse"]  # cycle glyph
   SIDE_EFFECT_BADGES["sticky"]       # sticky glyph
   SIDE_EFFECT_BADGES["volatile"]     # volatile glyph

Reusing the same dict means a notebook widget, a CLI ``info`` command,
and a downstream test report all render the same badge for the same
effect.

Worked example: timestamped read trace
--------------------------------------

A sibling unit that wraps every master attached through ``wrap_master``
to log every read with a wall-clock timestamp. The hook fires once when
the master is attached; it replaces the master's ``read`` method with a
wrapper that records the call.

.. code-block:: python

   # peakrdl_pybind11/runtime/read_trace.py
   """Timestamped read trace — example sibling unit."""

   from __future__ import annotations

   import logging
   import time

   from peakrdl_pybind11.runtime._registry import register_master_extension

   logger = logging.getLogger("peakrdl_pybind11.runtime.read_trace")

   __all__ = ["install_read_trace"]


   @register_master_extension
   def install_read_trace(master):
       """Wrap ``master.read`` so every read is logged with a timestamp."""
       original_read = master.read

       def traced_read(addr, width):
           started = time.monotonic()
           value = original_read(addr, width)
           logger.info(
               "READ ts=%.6f addr=%#x width=%d value=%#x",
               started, addr, width, value,
           )
           return value

       master.read = traced_read
       master._read_trace_installed = True

Drop the file at ``src/peakrdl_pybind11/runtime/read_trace.py`` and the
auto-discovery pass picks it up at next package import. The hook fires
inside ``wrap_master(...)`` for every master the user attaches.

Because the runtime fires hooks under the log-and-continue policy, a
buggy ``install_read_trace`` (raising on a master without a ``read``
attribute, for example) is logged through the registry logger and the
remaining sibling units still get a chance to run.
