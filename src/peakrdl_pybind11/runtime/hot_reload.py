"""Python-side hot-reload support for generated SoC modules.

Implements the runtime contract described in ``IDEAL_API_SKETCH.md`` §21
(``--watch`` + ``soc.reload``) and §22.6 (decision log: opt-in, loud,
invalidate handles, refuse during context managers, preserve bus state):

* ``soc.reload()`` re-imports the generated module, bumps a global
  generation counter so outstanding ``RegisterValue``/``Snapshot`` handles
  raise ``StaleHandleError`` on use, refuses to swap the tree while a
  context manager is active, and reattaches the existing master to the
  freshly built tree. Hardware bus state is untouched — only the host-side
  bindings get replaced.
* The reload policy is controlled by the module-level ``policy`` attribute,
  which is either ``"warn"`` (the default) or ``"fail"``.

Cross-unit seams
----------------
* Other units (``RegisterValue`` / ``Snapshot``) capture
  :func:`current_generation` at construction time and call
  :func:`check_generation` before any operation that might observe stale
  state. The contract is intentionally narrow and lives in this module so
  every consumer sees the same counter.
* Transaction / write-only / no-side-effects context managers register
  themselves via :func:`register_context` and unregister on exit. Reload
  refuses to swap the tree while at least one is active.
* :func:`attach_reload` binds the entry point as ``soc.reload`` on a
  freshly constructed SoC; Unit 1's ``register_post_create`` seam (when
  present) calls it during ``Soc.create``. The attachment also works
  standalone for tests and direct host-side use.
"""

from __future__ import annotations

import importlib
import threading
import types
import warnings
from collections.abc import Callable
from typing import Any

# Soc instances are dynamically built (one Python module per chip), so the
# host-side helpers can only reason about them through duck typing. The type
# alias makes the intent explicit at every call site without forcing each
# function to opt out of ANN401 individually.
_Soc = Any

__all__ = [
    "attach_reload",
    "check_generation",
    "current_generation",
    "policy",
    "register_context",
    "reload",
    "unregister_context",
]


# ---------------------------------------------------------------------------
# Public configuration
# ---------------------------------------------------------------------------

#: Reload policy, mirrored under ``peakrdl.reload.policy`` for the user-facing
#: documented surface. ``"warn"`` (default) emits ``UserWarning`` and
#: invalidates outstanding handles; ``"fail"`` raises :class:`RuntimeError`
#: instead of swapping the tree.
policy: str = "warn"

_VALID_POLICIES: frozenset[str] = frozenset({"warn", "fail"})


# ---------------------------------------------------------------------------
# Generation counter — drives ``RegisterValue`` / ``Snapshot`` staleness
# ---------------------------------------------------------------------------

# Monotonic counter; bumped on each successful reload. Long handles snapshot
# the value at creation and compare on use. We use a ``threading.Lock`` so
# concurrent reloads in the same process serialise — reloads are rare,
# correctness wins over throughput.
_generation_lock = threading.Lock()
_generation: int = 0


def current_generation() -> int:
    """Return the current global handle generation.

    Other units (``RegisterValue``, ``Snapshot``) capture this value when
    constructed and feed it back into :func:`check_generation` before every
    operation that could expose stale state.
    """

    return _generation


def check_generation(captured: int) -> None:
    """Raise ``StaleHandleError`` if ``captured`` is not the current generation.

    The error class lives in ``peakrdl_pybind11.errors`` (Unit 2). We import
    lazily to avoid a hard dependency on the sibling unit's import order; if
    the canonical class is unavailable we fall back to a locally-defined
    placeholder that subclasses ``RuntimeError`` so the user-facing behaviour
    is preserved.
    """

    if captured == _generation:
        return
    raise _resolve_stale_handle_class()(
        f"handle from generation {captured} used after reload "
        f"(current generation {_generation}); soc.reload() invalidates "
        "outstanding RegisterValue/Snapshot instances"
    )


def _resolve_stale_handle_class() -> type[Exception]:
    """Return the canonical ``StaleHandleError`` if Unit 2 has landed."""

    try:
        from peakrdl_pybind11 import errors as _errors
    except ImportError:
        return _LocalStaleHandleError
    cls = getattr(_errors, "StaleHandleError", None)
    if isinstance(cls, type) and issubclass(cls, Exception):
        return cls
    return _LocalStaleHandleError


class _LocalStaleHandleError(RuntimeError):
    """Fallback ``StaleHandleError`` used until Unit 2 ships ``errors``."""


# ---------------------------------------------------------------------------
# Active-context tracking — reload refuses while any context manager is open
# ---------------------------------------------------------------------------

# §22.6 specifies thread-local tracking: each thread's transactions /
# write-only blocks are scoped to that thread, and ``soc.reload()`` should
# refuse on the same thread that owns the open context. Cross-thread reload
# coordination is a higher-level concern and not part of this seam.
_active_contexts = threading.local()


def _thread_contexts() -> set[object]:
    """Return the calling thread's set of active contexts, lazily allocated."""

    contexts = getattr(_active_contexts, "contexts", None)
    if contexts is None:
        contexts = set()
        _active_contexts.contexts = contexts
    return contexts


def register_context(ctx: object) -> None:
    """Mark ``ctx`` as an active context manager (transaction, write-only, …).

    Idempotent: re-registering the same object is a no-op. Callers should
    pair every successful registration with :func:`unregister_context` in a
    ``finally`` block.
    """

    _thread_contexts().add(ctx)


def unregister_context(ctx: object) -> None:
    """Remove ``ctx`` from the active set; tolerates missing entries."""

    _thread_contexts().discard(ctx)


def _has_active_contexts() -> bool:
    return bool(getattr(_active_contexts, "contexts", None))


# ---------------------------------------------------------------------------
# Reload entry point
# ---------------------------------------------------------------------------


def reload(soc: _Soc) -> None:
    """Re-import the generated module backing ``soc`` and rebind the tree.

    Steps, in order:

    1. Refuse if any context manager is still active — the staged work
       belongs to the *old* tree and we cannot guarantee a sane swap.
       Checked **before** the policy gate so users see the real cause.
    2. Honour ``policy = "fail"`` by raising ``RuntimeError`` and leaving
       the world untouched.
    3. Resolve the generated module from ``soc`` (``__module__`` of the
       SoC class, falling back to a ``_module_name`` attribute) and call
       :func:`importlib.reload` on it.
    4. Build a fresh SoC by calling the new module's factory and reattach
       the existing master so hardware bus state is preserved.
    5. Bump the generation counter — *after* a successful reload so a
       failed import does not strand handles in a useless state.
    6. Emit a ``UserWarning`` so the loud-by-default contract is honoured.
    """

    if _has_active_contexts():
        raise RuntimeError(
            "soc.reload() refused: a context manager is still active "
            "(transaction, write-only, etc.) — exit it before reloading"
        )

    if policy == "fail":
        raise RuntimeError("soc.reload() blocked by policy='fail'")
    if policy not in _VALID_POLICIES:
        raise RuntimeError(
            f"unknown peakrdl.reload.policy={policy!r}; expected one of {sorted(_VALID_POLICIES)!r}"
        )

    module = _resolve_module(soc)
    reloaded = importlib.reload(module)
    _rebind_soc(soc, reloaded)

    # Bump on success only — a failed reload leaves outstanding handles
    # bound to the still-live old tree.
    global _generation
    with _generation_lock:
        _generation += 1

    warnings.warn(
        "soc reloaded; outstanding RegisterValue/Snapshot are now stale",
        category=UserWarning,
        stacklevel=2,
    )


def _resolve_module(soc: _Soc) -> types.ModuleType:
    """Return the module object backing ``soc``.

    Generated modules expose ``Soc.create(master=...)`` and live at the
    package's top level. We first honour an explicit ``_module_name`` hint,
    then fall back to ``type(soc).__module__`` — both routes resolve via
    ``sys.modules``/``importlib`` so any aliasing (e.g. ``mychip._native``
    re-exported through ``mychip``) lands on the same module object.
    """

    name = getattr(soc, "_module_name", None) or type(soc).__module__
    if not name or name == "__main__":
        raise RuntimeError(
            "soc.reload() cannot determine the generated module to reload; "
            "set soc._module_name or define the SoC class in an importable module"
        )
    return importlib.import_module(name)


def _rebind_soc(soc: _Soc, reloaded_module: types.ModuleType) -> None:
    """Reconstruct the SoC tree from ``reloaded_module`` and graft it onto ``soc``.

    The exact factory varies between generated modules; we look for the
    documented entry points in priority order. Whatever shape the new soc
    takes, the *old* master is moved across so bus state is preserved.
    """

    factory = _find_factory(reloaded_module)
    if factory is None:
        raise RuntimeError(
            f"reloaded module {reloaded_module.__name__!r} exposes no "
            "create()/Soc.create() factory; cannot rebind tree"
        )

    master = getattr(soc, "master", None)
    new_soc = factory(master=master)

    # Re-bind: we keep the identity of ``soc`` (callers' references stay
    # valid) but its internals are wholesale replaced by the new tree.
    # ``reload`` is reattached afterwards so the bound method is a clean
    # closure over the new state.
    new_dict = getattr(new_soc, "__dict__", None)
    if new_dict is None:
        raise RuntimeError("reloaded SoC has no __dict__; cannot rebind onto existing instance")
    soc.__dict__.clear()
    soc.__dict__.update(new_dict)

    # Bus state preservation contract: even if the new factory built a
    # different master object (or none), the original master continues to
    # drive hardware. The §22.6 decision is unconditional on this point.
    if master is not None:
        soc.master = master

    # ``soc.__dict__.clear()`` removed the previous bound ``reload``; rebind.
    attach_reload(soc)


def _find_factory(module: types.ModuleType) -> Callable[..., object] | None:
    """Locate a SoC factory on ``module``.

    Order:
    1. ``module.create`` — the generated package's documented entry point.
    2. ``module.Soc.create`` — the class-bound classmethod form.
    """

    direct = getattr(module, "create", None)
    if callable(direct):
        return direct
    soc_cls = getattr(module, "Soc", None)
    if soc_cls is not None:
        cls_create = getattr(soc_cls, "create", None)
        if callable(cls_create):
            return cls_create
    return None


# ---------------------------------------------------------------------------
# Wiring — attach to a soc instance, optionally via Unit 1's seam
# ---------------------------------------------------------------------------


def attach_reload(soc: _Soc) -> None:
    """Bind ``soc.reload`` so the documented user surface works.

    Calling ``soc.reload()`` is forwarded to :func:`reload` with ``soc``
    captured. We also surface :func:`register_context` and
    :func:`unregister_context` as private hooks on the SoC for sibling
    units that wish to call them through the soc handle rather than
    importing this module directly.
    """

    soc.reload = lambda: reload(soc)
    soc._register_context = register_context
    soc._unregister_context = unregister_context


# Wire into Unit 1's ``register_post_create`` seam if it has shipped. The
# seam is best-effort: when Unit 1 is absent, callers can invoke
# :func:`attach_reload` directly (e.g. inside the generated runtime).
def _install_post_create_hook() -> None:
    try:
        from peakrdl_pybind11 import _registry
    except ImportError:
        return
    register_post_create = getattr(_registry, "register_post_create", None)
    if not callable(register_post_create):
        return
    register_post_create(attach_reload)


_install_post_create_hook()


def __getattr__(name: str) -> type[Exception]:
    """Expose ``StaleHandleError`` for symmetry with sibling units.

    The canonical class lives in ``peakrdl_pybind11.errors`` once Unit 2
    has shipped. This proxy lets ``from peakrdl_pybind11.runtime.hot_reload
    import StaleHandleError`` succeed regardless of import order.
    """

    if name == "StaleHandleError":
        return _resolve_stale_handle_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
