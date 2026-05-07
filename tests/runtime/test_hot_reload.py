"""Tests for ``peakrdl_pybind11.runtime.hot_reload``.

These tests stand on their own — they fake the generated SoC module rather
than depending on Units 1/2/3/8 having shipped. The behaviours under test
are exactly those promised by ``IDEAL_API_SKETCH.md`` §21 and §22.6:

* ``soc.reload()`` invalidates outstanding ``RegisterValue`` / ``Snapshot``
  handles.
* It refuses while any context manager is active.
* ``policy = "fail"`` aborts instead of warning.
* Hardware bus state — represented here by the master object — survives.
"""

from __future__ import annotations

import sys
import types
import warnings
from typing import Any

import pytest

from peakrdl_pybind11.runtime import hot_reload

# ---------------------------------------------------------------------------
# Fakes — a generated-module shape thin enough to drive every code path
# ---------------------------------------------------------------------------


class _FakeMaster:
    """Stand-in for a bus master. Holds bus state we want preserved."""

    def __init__(self) -> None:
        self.read_count = 0
        self.write_count = 0
        # Identity sentinel: lets tests assert "the same master survives".
        self.id_token = object()


class _FakeSoc:
    """Minimal SoC with the seams ``hot_reload`` expects."""

    def __init__(self, master: _FakeMaster | None = None) -> None:
        self.master = master
        # Tag instances so we can tell pre-/post-reload trees apart.
        self.build_token = object()


class _FakeSnapshot:
    """Behaves like Unit 8's ``Snapshot`` for staleness purposes only."""

    def __init__(self) -> None:
        self._generation = hot_reload.current_generation()

    def diff(self, other: _FakeSnapshot) -> dict[str, Any]:
        hot_reload.check_generation(self._generation)
        hot_reload.check_generation(other._generation)
        return {}


class _FakeRegisterValue:
    """Behaves like Unit 3's ``RegisterValue`` for staleness purposes only."""

    def __init__(self, raw: int) -> None:
        self._generation = hot_reload.current_generation()
        self._raw = raw

    def __int__(self) -> int:
        hot_reload.check_generation(self._generation)
        return self._raw


# ---------------------------------------------------------------------------
# Module installer — registers a fake generated package with ``Soc.create``
# ---------------------------------------------------------------------------


def _install_fake_module(name: str) -> types.ModuleType:
    """Create + register a synthetic generated module under ``name``.

    The module exposes ``create(master=...)`` returning a fresh ``_FakeSoc``.
    Tests pair this with the ``patch_importlib_reload`` autouse fixture so
    ``importlib.reload`` re-executes the install function rather than going
    through the normal finder/loader machinery (which can't see our purely
    in-memory module).
    """

    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module

    def create(master: _FakeMaster | None = None) -> _FakeSoc:
        return _FakeSoc(master=master)

    module.create = create  # type: ignore[attr-defined]
    return module


def _silent_reload(soc: _FakeSoc) -> None:
    """Run ``soc.reload()`` while suppressing the expected UserWarning.

    Most tests don't care about the warning itself — they're checking
    side effects of a successful reload. The dedicated warning test
    asserts the warning is emitted in the default policy.
    """

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        soc.reload()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_importlib_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``importlib.reload`` reinstall our fake modules in place.

    ``importlib.reload`` normally consults the import machinery to find a
    spec for the module. Synthetic modules registered straight into
    ``sys.modules`` (the cheapest way to mock a generated package) have no
    finder, so the real ``reload`` raises ``ModuleNotFoundError``. Tests
    only need the *behaviour* of ``reload`` — module dict refreshed in
    place — so we substitute a small stand-in that mirrors that behaviour.
    """

    def fake_reload(module: types.ModuleType) -> types.ModuleType:
        name = module.__name__
        if name not in sys.modules:
            raise ImportError(f"module {name!r} not in sys.modules")
        # Reinstall using the same name; the helper preserves identity by
        # mutating the existing module object's namespace.
        return _install_fake_module(name)

    import importlib

    monkeypatch.setattr(importlib, "reload", fake_reload)


@pytest.fixture
def fake_module(request: pytest.FixtureRequest) -> types.ModuleType:
    """Provide a uniquely-named fake generated module per test, auto-cleaned."""

    name = f"_peakrdl_hot_reload_fake_{request.node.name}"
    module = _install_fake_module(name)
    yield module
    sys.modules.pop(name, None)


@pytest.fixture
def soc(fake_module: types.ModuleType) -> _FakeSoc:
    """Build a soc instance bound to ``fake_module`` and attach reload."""

    master = _FakeMaster()
    instance = fake_module.create(master=master)  # type: ignore[attr-defined]
    # Mirror what ``register_post_create`` would do for a real generated SoC.
    instance.__class__.__module__ = fake_module.__name__
    hot_reload.attach_reload(instance)
    return instance


@pytest.fixture(autouse=True)
def _reset_hot_reload_state() -> None:
    """Keep state tidy between tests — policy, contexts, generation counter."""

    saved_policy = hot_reload.policy
    saved_generation = hot_reload._generation
    yield
    hot_reload.policy = saved_policy
    hot_reload._generation = saved_generation
    # Drain anything tests forgot to clean up. ``_thread_contexts`` lazily
    # creates the per-thread set, so reach in directly.
    hot_reload._thread_contexts().clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reload_invalidates_outstanding_snapshot(soc: _FakeSoc) -> None:
    """Old snapshots raise StaleHandleError after reload."""

    snap_before = _FakeSnapshot()
    snap_concurrent = _FakeSnapshot()

    _silent_reload(soc)

    with pytest.raises(hot_reload.StaleHandleError):
        snap_before.diff(snap_concurrent)


def test_reload_invalidates_outstanding_register_value(soc: _FakeSoc) -> None:
    """RegisterValue handles raise StaleHandleError after reload."""

    value = _FakeRegisterValue(0x1234)
    assert int(value) == 0x1234  # Pre-reload uses are fine.

    _silent_reload(soc)

    with pytest.raises(hot_reload.StaleHandleError):
        int(value)


def test_reload_emits_loud_warning_by_default(soc: _FakeSoc) -> None:
    """Default policy ('warn') emits a UserWarning naming the consequences."""

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        soc.reload()

    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert user_warnings, "expected a UserWarning from soc.reload()"
    assert "stale" in str(user_warnings[0].message).lower()


def test_reload_blocked_by_active_context_manager(soc: _FakeSoc) -> None:
    """An open context manager makes reload raise RuntimeError."""

    sentinel = object()
    hot_reload.register_context(sentinel)
    try:
        with pytest.raises(RuntimeError, match="context manager"):
            soc.reload()
    finally:
        hot_reload.unregister_context(sentinel)

    # Once the context exits, reload works again — proving the block is gated
    # on *active* contexts and not a permanent latch.
    _silent_reload(soc)


def test_reload_active_context_check_precedes_policy_fail(soc: _FakeSoc) -> None:
    """When both conditions hit, the user sees the context-manager error first."""

    hot_reload.policy = "fail"
    sentinel = object()
    hot_reload.register_context(sentinel)
    try:
        with pytest.raises(RuntimeError, match="context manager"):
            soc.reload()
    finally:
        hot_reload.unregister_context(sentinel)


def test_reload_policy_fail_blocks_reload(soc: _FakeSoc) -> None:
    """policy='fail' raises a RuntimeError that mentions the policy."""

    hot_reload.policy = "fail"
    starting_generation = hot_reload.current_generation()

    with pytest.raises(RuntimeError, match=r"policy='fail'"):
        soc.reload()

    # Generation must NOT advance on a policy-fail (no handles invalidated).
    assert hot_reload.current_generation() == starting_generation


def test_reload_invalid_policy_is_a_clear_error(soc: _FakeSoc) -> None:
    """A typo in ``policy`` should not silently fall through to 'warn'."""

    hot_reload.policy = "loud"

    with pytest.raises(RuntimeError, match=r"unknown peakrdl\.reload\.policy"):
        soc.reload()


def test_reload_preserves_master_identity(soc: _FakeSoc) -> None:
    """The bus master survives reload unchanged — bus state is untouched."""

    master_before = soc.master
    assert master_before is not None
    token = master_before.id_token

    _silent_reload(soc)

    assert soc.master is master_before, "master identity must be preserved"
    assert soc.master.id_token is token, "bus state must not be reset"


def test_reload_generation_advances_on_success(soc: _FakeSoc) -> None:
    """Generation counter increases monotonically on successful reload."""

    g0 = hot_reload.current_generation()
    _silent_reload(soc)
    g1 = hot_reload.current_generation()
    _silent_reload(soc)
    g2 = hot_reload.current_generation()

    assert g1 == g0 + 1
    assert g2 == g1 + 1


def test_check_generation_passes_for_current() -> None:
    """``check_generation`` is a no-op when the captured value is current."""

    hot_reload.check_generation(hot_reload.current_generation())


def test_check_generation_raises_for_stale_value(soc: _FakeSoc) -> None:
    """``check_generation`` raises when the captured value is stale."""

    captured = hot_reload.current_generation()
    _silent_reload(soc)

    with pytest.raises(hot_reload.StaleHandleError):
        hot_reload.check_generation(captured)


def test_register_unregister_context_round_trip() -> None:
    """register_context and unregister_context round-trip cleanly."""

    sentinel = object()
    hot_reload.register_context(sentinel)
    # Idempotent: re-registering does not duplicate.
    hot_reload.register_context(sentinel)
    hot_reload.unregister_context(sentinel)
    # Tolerates double-unregister.
    hot_reload.unregister_context(sentinel)


def test_attach_reload_binds_method_on_soc(fake_module: types.ModuleType) -> None:
    """attach_reload installs ``soc.reload`` and the private context hooks."""

    instance = fake_module.create(master=_FakeMaster())  # type: ignore[attr-defined]
    instance.__class__.__module__ = fake_module.__name__
    assert not hasattr(instance, "reload")

    hot_reload.attach_reload(instance)

    assert callable(instance.reload)
    assert instance._register_context is hot_reload.register_context
    assert instance._unregister_context is hot_reload.unregister_context


def test_reload_rebinds_to_new_tree(soc: _FakeSoc) -> None:
    """After reload, soc internals point at the freshly-built tree."""

    token_before = soc.build_token

    _silent_reload(soc)

    assert soc.build_token is not token_before, (
        "reload should rebuild the internal tree (build_token must change)"
    )


def test_reload_method_survives_reload(soc: _FakeSoc) -> None:
    """Calling reload twice in a row works — the bound method is reattached."""

    _silent_reload(soc)
    assert callable(soc.reload)
    _silent_reload(soc)


def test_module_level_stale_handle_error_alias_exists() -> None:
    """``hot_reload.StaleHandleError`` resolves even before Unit 2 ships."""

    cls = hot_reload.StaleHandleError
    assert isinstance(cls, type)
    assert issubclass(cls, Exception)


def test_unknown_module_attribute_still_raises() -> None:
    """The proxy on ``hot_reload`` does not swallow real lookup failures."""

    with pytest.raises(AttributeError):
        hot_reload.this_attribute_does_not_exist  # noqa: B018
