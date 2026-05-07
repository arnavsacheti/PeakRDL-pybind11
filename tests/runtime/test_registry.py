"""Unit tests for ``peakrdl_pybind11.runtime._registry``.

These tests exercise the seam itself — no generated module, no cmake.
They run by default and never opt into the ``integration`` mark.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from peakrdl_pybind11.runtime import _registry


@pytest.fixture(autouse=True)
def _isolate_registry() -> Any:
    """Snapshot every registry, run the test, then restore.

    Without this every test would leave global state behind that the
    next test would observe — and the registry is intentionally a
    process-wide singleton (sibling units register at import time).
    """
    snap = _registry._snapshot()
    yield
    # Restore by replacing the lists/dicts in-place so any reference
    # held elsewhere keeps pointing at the same object.
    with _registry._lock:
        _registry._register_enhancements[:] = snap["register_enhancements"]
        _registry._field_enhancements[:] = snap["field_enhancements"]
        _registry._post_create_hooks[:] = snap["post_create_hooks"]
        _registry._master_extensions[:] = snap["master_extensions"]
        _registry._node_attributes.clear()
        _registry._node_attributes.update(snap["node_attributes"])
        _registry._seen_register_enhancements.clear()
        _registry._seen_register_enhancements.update(
            id(fn) for fn in snap["register_enhancements"]
        )
        _registry._seen_field_enhancements.clear()
        _registry._seen_field_enhancements.update(id(fn) for fn in snap["field_enhancements"])
        _registry._seen_post_create_hooks.clear()
        _registry._seen_post_create_hooks.update(id(fn) for fn in snap["post_create_hooks"])
        _registry._seen_master_extensions.clear()
        _registry._seen_master_extensions.update(id(fn) for fn in snap["master_extensions"])


# ---------------------------------------------------------------------------
# Decorator stores callable
# ---------------------------------------------------------------------------


def test_register_register_enhancement_stores_callable() -> None:
    @_registry.register_register_enhancement
    def hook(cls: type, metadata: dict) -> None:
        pass

    assert hook in _registry._snapshot()["register_enhancements"]


def test_register_field_enhancement_stores_callable() -> None:
    @_registry.register_field_enhancement
    def hook(cls: type) -> None:
        pass

    assert hook in _registry._snapshot()["field_enhancements"]


def test_register_post_create_stores_callable() -> None:
    @_registry.register_post_create
    def hook(soc: Any) -> None:
        pass

    assert hook in _registry._snapshot()["post_create_hooks"]


def test_register_master_extension_stores_callable() -> None:
    @_registry.register_master_extension
    def hook(master: Any) -> None:
        pass

    assert hook in _registry._snapshot()["master_extensions"]


def test_register_node_attribute_stores_callable() -> None:
    @_registry.register_node_attribute("my_attr")
    def factory(node: Any) -> str:
        return "value"

    assert "my_attr" in _registry._snapshot()["node_attributes"]


# ---------------------------------------------------------------------------
# Idempotency: re-registering the same callable is a no-op
# ---------------------------------------------------------------------------


def test_re_registering_register_enhancement_does_not_duplicate() -> None:
    def hook(cls: type, metadata: dict) -> None:
        pass

    _registry.register_register_enhancement(hook)
    _registry.register_register_enhancement(hook)
    matches = [fn for fn in _registry._snapshot()["register_enhancements"] if fn is hook]
    assert len(matches) == 1


def test_re_registering_field_enhancement_does_not_duplicate() -> None:
    def hook(cls: type) -> None:
        pass

    _registry.register_field_enhancement(hook)
    _registry.register_field_enhancement(hook)
    matches = [fn for fn in _registry._snapshot()["field_enhancements"] if fn is hook]
    assert len(matches) == 1


def test_re_registering_post_create_does_not_duplicate() -> None:
    def hook(soc: Any) -> None:
        pass

    _registry.register_post_create(hook)
    _registry.register_post_create(hook)
    matches = [fn for fn in _registry._snapshot()["post_create_hooks"] if fn is hook]
    assert len(matches) == 1


def test_re_registering_master_extension_does_not_duplicate() -> None:
    def hook(master: Any) -> None:
        pass

    _registry.register_master_extension(hook)
    _registry.register_master_extension(hook)
    matches = [fn for fn in _registry._snapshot()["master_extensions"] if fn is hook]
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# Apply / fire functions actually invoke the registered callables
# ---------------------------------------------------------------------------


def test_apply_register_enhancements_invokes_hook() -> None:
    seen: list[tuple[type, dict]] = []

    @_registry.register_register_enhancement
    def hook(cls: type, metadata: dict) -> None:
        seen.append((cls, metadata))

    class FakeReg:
        pass

    _registry.apply_register_enhancements(FakeReg, {"fields": {"a": (0, 1)}})
    # The default shim plus our hook both ran; both received the same args.
    matches = [item for item in seen if item[0] is FakeReg]
    assert len(matches) == 1
    assert matches[0][1] == {"fields": {"a": (0, 1)}}


def test_apply_field_enhancements_invokes_hook() -> None:
    seen: list[type] = []

    @_registry.register_field_enhancement
    def hook(cls: type) -> None:
        seen.append(cls)

    class FakeField:
        pass

    _registry.apply_field_enhancements(FakeField)
    assert FakeField in seen


def test_fire_post_create_hooks_invokes_hook() -> None:
    seen: list[Any] = []

    @_registry.register_post_create
    def hook(soc: Any) -> None:
        seen.append(soc)

    fake_soc = object()
    _registry.fire_post_create_hooks(fake_soc)
    assert seen == [fake_soc]


def test_fire_master_extensions_invokes_hook() -> None:
    seen: list[Any] = []

    @_registry.register_master_extension
    def hook(master: Any) -> None:
        seen.append(master)

    fake_master = object()
    _registry.fire_master_extensions(fake_master)
    assert seen == [fake_master]


# ---------------------------------------------------------------------------
# attach_node_attributes wires lazy properties
# ---------------------------------------------------------------------------


def test_attach_node_attributes_with_none_is_noop() -> None:
    # Should not raise.
    _registry.attach_node_attributes(None)


def test_attach_node_attributes_installs_lazy_property() -> None:
    counter = {"calls": 0}

    @_registry.register_node_attribute("computed")
    def factory(node: Any) -> int:
        counter["calls"] += 1
        return 42

    class Node:
        pass

    _registry.attach_node_attributes(Node)
    n = Node()
    assert n.computed == 42
    assert n.computed == 42  # cached
    assert counter["calls"] == 1


def test_attach_node_attributes_does_not_overwrite_existing() -> None:
    @_registry.register_node_attribute("info")
    def factory(node: Any) -> str:
        return "from-registry"

    class Node:
        info = "preset"  # explicit class attribute should win

    _registry.attach_node_attributes(Node)
    assert Node.info == "preset"


# ---------------------------------------------------------------------------
# Auto-discovery picks up modules under runtime/
# ---------------------------------------------------------------------------


def test_default_shims_are_registered() -> None:
    """Importing ``peakrdl_pybind11.runtime`` must auto-load the default shims."""
    # The runtime package is already loaded by the test session; verify
    # the default shim functions are in the registries.
    snap = _registry._snapshot()
    register_names = {fn.__name__ for fn in snap["register_enhancements"]}
    field_names = {fn.__name__ for fn in snap["field_enhancements"]}
    assert "_default_register_shim" in register_names
    assert "_default_field_shim" in field_names


def test_auto_import_picks_up_fake_module() -> None:
    """A new module dropped into ``runtime/`` is auto-imported on package load."""
    import peakrdl_pybind11.runtime as runtime_pkg

    fake_marker = "_test_auto_import_marker_42"

    # Drop a fake sibling module next to ``_default_shims.py``.
    pkg_dir = Path(runtime_pkg.__file__).parent
    fake_path = pkg_dir / "_test_fake_sibling.py"
    fake_path.write_text(
        textwrap.dedent(
            f"""
            from peakrdl_pybind11.runtime import _registry

            @_registry.register_register_enhancement
            def {fake_marker}(cls, metadata):
                pass
            """
        )
    )

    try:
        # Reload the package so ``_auto_import_modules`` runs again.
        importlib.reload(runtime_pkg)
        snap = _registry._snapshot()
        assert any(fn.__name__ == fake_marker for fn in snap["register_enhancements"])
    finally:
        fake_path.unlink(missing_ok=True)
        # Pop the imported module so subsequent reloads don't see a stale
        # cached copy.
        sys.modules.pop(f"{runtime_pkg.__name__}._test_fake_sibling", None)


# ---------------------------------------------------------------------------
# Forward-compat aliases at the package level
# ---------------------------------------------------------------------------


def test_runtime_re_exports_register_value_alias() -> None:
    from peakrdl_pybind11 import int_types
    from peakrdl_pybind11.runtime import FieldValue, RegisterValue

    assert RegisterValue is int_types.RegisterInt
    assert FieldValue is int_types.FieldInt


def test_top_level_init_re_exports_value_aliases() -> None:
    from peakrdl_pybind11 import FieldInt, FieldValue, RegisterInt, RegisterValue

    assert RegisterValue is RegisterInt
    assert FieldValue is FieldInt
