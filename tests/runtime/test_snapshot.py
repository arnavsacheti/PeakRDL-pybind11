"""Tests for ``runtime/snapshot.py`` (Unit 8, sketch §15).

These tests build a small mock SoC because the generated soc surface
hasn't landed yet. The mock implements the minimum protocol the snapshot
implementation relies on:

* ``soc.walk()`` — yields nodes.
* node ``.info`` — has ``path``, ``access``, and (optionally) ``on_read``.
* node ``.peek()``, ``.read()``, ``.write(value)``.
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from pathlib import Path

import pytest

from peakrdl_pybind11.runtime import (
    SideEffectError,
    Snapshot,
    SnapshotDiff,
    register_post_create,
)


# ---------------------------------------------------------------------------
# Mock soc plumbing
# ---------------------------------------------------------------------------


@dataclass
class _Info:
    path: str
    access: str = "rw"  # "ro" / "wo" / "rw" / "rwclr" / etc.
    on_read: str | None = None  # None | "rclr" | "rset"
    address: int = 0
    reset: int = 0


class _Reg:
    """A tiny mock register with peek/read/write semantics."""

    def __init__(
        self,
        path: str,
        value: int = 0,
        access: str = "rw",
        on_read: str | None = None,
        peekable: bool = True,
        address: int = 0,
    ) -> None:
        self.info = _Info(path=path, access=access, on_read=on_read, address=address)
        self._value = value
        self.peekable = peekable
        self.read_count = 0
        self.write_count = 0
        self.peek_count = 0

    def peek(self) -> int:
        if not self.peekable:
            raise NotImplementedError("master can't peek")
        self.peek_count += 1
        return self._value

    def read(self) -> int:
        # Side-effecting reads model on_read semantics.
        self.read_count += 1
        v = self._value
        if self.info.on_read == "rclr":
            self._value = 0
        elif self.info.on_read == "rset":
            self._value = (1 << 32) - 1
        return v

    def write(self, value: int) -> None:
        self.write_count += 1
        self._value = int(value)


class _Soc:
    """A minimal soc with nodes registered into a flat dict."""

    def __init__(self) -> None:
        self._nodes: list[_Reg] = []

    def add(self, reg: _Reg) -> _Reg:
        self._nodes.append(reg)
        return reg

    def walk(self) -> list[_Reg]:
        return list(self._nodes)


def _make_soc() -> _Soc:
    """Build a fresh soc with two writable registers."""
    soc = _Soc()
    soc.add(_Reg("uart.control", value=0x22, access="rw", address=0x4000_1000))
    soc.add(_Reg("uart.data", value=0x55, access="rw", address=0x4000_1004))
    register_post_create(soc)
    return soc


# ---------------------------------------------------------------------------
# Basic capture & diff
# ---------------------------------------------------------------------------


class TestBasicSnapshot:
    def test_snapshot_captures_all_readable(self) -> None:
        soc = _make_soc()
        snap = soc.snapshot()
        assert isinstance(snap, Snapshot)
        assert snap["uart.control"] == 0x22
        assert snap["uart.data"] == 0x55
        assert sorted(snap.paths) == ["uart.control", "uart.data"]

    def test_snapshot_uses_peek_by_default(self) -> None:
        soc = _make_soc()
        soc.snapshot()
        for node in soc.walk():
            assert node.peek_count == 1
            assert node.read_count == 0

    def test_diff_shows_changes(self) -> None:
        soc = _make_soc()
        before = soc.snapshot()
        # Modify one register's "hardware" state directly (bypass write counter).
        for node in soc.walk():
            if node.info.path == "uart.control":
                node._value = 0xAB

        after = soc.snapshot()
        diff = after.diff(before)

        assert isinstance(diff, SnapshotDiff)
        assert not diff.is_empty
        assert "uart.control" in diff.changed
        before_val, after_val = diff.changed["uart.control"]
        assert before_val == 0x22
        assert after_val == 0xAB
        # uart.data unchanged
        assert "uart.data" not in diff.changed

    def test_diff_empty_when_no_changes(self) -> None:
        soc = _make_soc()
        a = soc.snapshot()
        b = soc.snapshot()
        diff = b.diff(a)
        assert diff.is_empty
        assert len(diff) == 0
        assert not diff
        assert "0 differences" in str(diff)

    def test_diff_str_pretty_format(self) -> None:
        soc = _make_soc()
        before = soc.snapshot()
        for node in soc.walk():
            if node.info.path == "uart.control":
                node._value = 0xAB
        after = soc.snapshot()
        text = str(after.diff(before))
        assert "uart.control" in text
        assert "0x000000ab" in text or "0xab" in text


# ---------------------------------------------------------------------------
# Structural attribute / item access
# ---------------------------------------------------------------------------


class TestAccess:
    def test_dotted_path_via_getitem(self) -> None:
        soc = _make_soc()
        snap = soc.snapshot()
        assert snap["uart.control"] == 0x22

    def test_attribute_access_returns_subtree(self) -> None:
        soc = _make_soc()
        snap = soc.snapshot()
        sub = snap.uart  # subtree
        assert isinstance(sub, Snapshot)
        # Inside the subtree, paths are stripped.
        assert sub["control"] == 0x22
        assert sub["data"] == 0x55

    def test_attribute_chain_resolves_to_leaf(self) -> None:
        soc = _make_soc()
        snap = soc.snapshot()
        # snap.uart returns subtree; .control is a leaf inside it.
        assert snap.uart["control"] == 0x22

    def test_unknown_path_raises_key_error(self) -> None:
        soc = _make_soc()
        snap = soc.snapshot()
        with pytest.raises(KeyError):
            _ = snap["does.not.exist"]

    def test_contains(self) -> None:
        soc = _make_soc()
        snap = soc.snapshot()
        assert "uart.control" in snap
        assert "missing.path" not in snap


# ---------------------------------------------------------------------------
# Where filter
# ---------------------------------------------------------------------------


class TestWhereFilter:
    def test_where_glob_filters_subtree(self) -> None:
        soc = _Soc()
        soc.add(_Reg("uart.control", value=1, access="rw"))
        soc.add(_Reg("uart.data", value=2, access="rw"))
        soc.add(_Reg("gpio.dir", value=3, access="rw"))
        register_post_create(soc)

        snap = soc.snapshot(where="uart.*")
        assert sorted(snap.paths) == ["uart.control", "uart.data"]
        assert "gpio.dir" not in snap

    def test_where_callable(self) -> None:
        soc = _Soc()
        soc.add(_Reg("uart.control", value=1, access="rw"))
        soc.add(_Reg("gpio.dir", value=3, access="rw"))
        register_post_create(soc)

        snap = soc.snapshot(where=lambda p: p.startswith("gpio."))
        assert snap.paths == ["gpio.dir"]


# ---------------------------------------------------------------------------
# Side-effect safety
# ---------------------------------------------------------------------------


class TestSideEffectSafety:
    def test_rclr_register_blocks_default_snapshot(self) -> None:
        """A register with on_read=rclr and no peek must abort by default."""
        soc = _Soc()
        soc.add(
            _Reg(
                "uart.intr_status",
                value=0xF,
                access="rw",
                on_read="rclr",
                peekable=False,
            )
        )
        register_post_create(soc)

        with pytest.raises(SideEffectError):
            soc.snapshot()

    def test_rclr_register_allowed_with_destructive_flag(self) -> None:
        soc = _Soc()
        node = _Reg(
            "uart.intr_status",
            value=0xF,
            access="rw",
            on_read="rclr",
            peekable=False,
        )
        soc.add(node)
        register_post_create(soc)

        snap = soc.snapshot(allow_destructive=True)
        assert snap["uart.intr_status"] == 0xF
        # on_read=rclr means the read clears the register
        assert node.read_count == 1
        assert node._value == 0

    def test_rclr_with_peek_does_not_clear(self) -> None:
        """Even rclr can be safely snapshotted if the master supports peek."""
        soc = _Soc()
        node = _Reg(
            "uart.intr_status",
            value=0xF,
            access="rw",
            on_read="rclr",
            peekable=True,
        )
        soc.add(node)
        register_post_create(soc)

        snap = soc.snapshot()
        assert snap["uart.intr_status"] == 0xF
        assert node.peek_count == 1
        assert node.read_count == 0  # peek prevented destructive read


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class TestRestore:
    def test_dry_run_returns_intended_writes(self) -> None:
        soc = _make_soc()
        snap = soc.snapshot()

        # Mutate hardware state — restore should put it back.
        for node in soc.walk():
            node._value ^= 0xFF

        intended = soc.restore(snap, dry_run=True)
        # Two writable registers should appear.
        assert len(intended) == 2
        paths = {p for p, _ in intended}
        assert paths == {"uart.control", "uart.data"}

        # Values match the snapshot
        intended_dict = dict(intended)
        assert intended_dict["uart.control"] == 0x22
        assert intended_dict["uart.data"] == 0x55

        # Dry run did NOT issue writes.
        for node in soc.walk():
            assert node.write_count == 0

    def test_actual_restore_issues_writes(self) -> None:
        soc = _make_soc()
        snap = soc.snapshot()

        # Mutate hardware state.
        for node in soc.walk():
            node._value = 0xDEAD

        soc.restore(snap)
        # All writable registers should be written exactly once.
        for node in soc.walk():
            assert node.write_count == 1
            # And they should now hold the snapshot value.
            if node.info.path == "uart.control":
                assert node._value == 0x22
            elif node.info.path == "uart.data":
                assert node._value == 0x55

    def test_restore_skips_read_only(self) -> None:
        soc = _Soc()
        rw = _Reg("uart.control", value=0x22, access="rw")
        ro = _Reg("uart.status", value=0xFF, access="ro")
        soc.add(rw)
        soc.add(ro)
        register_post_create(soc)

        snap = soc.snapshot()
        # Mutate everything.
        rw._value = 0
        ro._value = 0

        intended = soc.restore(snap, dry_run=True)
        paths = {p for p, _ in intended}
        assert paths == {"uart.control"}  # read-only excluded

        soc.restore(snap)
        assert rw._value == 0x22
        assert rw.write_count == 1
        assert ro._value == 0  # not restored
        assert ro.write_count == 0

    def test_restore_from_subtree_snapshot(self) -> None:
        """Restoring a prefix-stripped subtree view still hits absolute paths."""
        soc = _Soc()
        soc.add(_Reg("uart.control", value=0x11, access="rw"))
        soc.add(_Reg("uart.data", value=0x22, access="rw"))
        soc.add(_Reg("gpio.dir", value=0x33, access="rw"))
        register_post_create(soc)

        full = soc.snapshot()
        uart_view = full.uart  # subtree: keys are "control", "data"
        assert sorted(uart_view.paths) == ["control", "data"]

        # Mutate hardware so restore has work to do.
        for node in soc.walk():
            node._value = 0xDEAD

        intended = soc.restore(uart_view, dry_run=True)
        paths = {p for p, _ in intended}
        # Only uart.* paths come back, with their absolute names preserved.
        assert paths == {"uart.control", "uart.data"}


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_json_round_trip(self, tmp_path: Path) -> None:
        soc = _make_soc()
        snap = soc.snapshot()

        out = tmp_path / "snap.json"
        snap.to_json(str(out))

        # Sanity: file is valid JSON
        data = json.loads(out.read_text())
        assert "values" in data
        assert "metadata" in data

        # Round-trip
        loaded = Snapshot.from_json(str(out))
        assert loaded == snap
        assert loaded["uart.control"] == 0x22
        assert loaded["uart.data"] == 0x55

        # Metadata structure preserved (path, access at minimum).
        for path in loaded.paths:
            md = loaded.metadata[path]
            assert getattr(md, "path", None) == path
            assert getattr(md, "access", None) == "rw"

    def test_to_json_returns_string(self) -> None:
        soc = _make_soc()
        snap = soc.snapshot()
        text = snap.to_json()
        assert isinstance(text, str)
        data = json.loads(text)
        assert data["values"]["uart.control"] == 0x22

    def test_pickle_round_trip(self) -> None:
        soc = _make_soc()
        snap = soc.snapshot()
        data = pickle.dumps(snap)
        loaded = pickle.loads(data)
        assert isinstance(loaded, Snapshot)
        assert loaded == snap
        assert loaded["uart.control"] == 0x22
        # Metadata survives pickle as well
        md = loaded.metadata["uart.control"]
        assert getattr(md, "path", None) == "uart.control"
        assert getattr(md, "access", None) == "rw"


# ---------------------------------------------------------------------------
# Snapshot identity / hash
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_snapshot_hashable_and_dict_keyable(self) -> None:
        soc = _make_soc()
        a = soc.snapshot()
        b = soc.snapshot()
        # Same values means same hash.
        assert hash(a) == hash(b)
        assert a == b

        d = {a: "first"}
        assert d[b] == "first"  # b hashes to the same bucket

    def test_snapshot_eq_compares_values_only(self) -> None:
        s1 = Snapshot({"a": 1, "b": 2})
        s2 = Snapshot({"a": 1, "b": 2})
        s3 = Snapshot({"a": 1, "b": 3})
        assert s1 == s2
        assert s1 != s3


# ---------------------------------------------------------------------------
# SnapshotDiff assertions
# ---------------------------------------------------------------------------


class TestDiffAssertions:
    def _diff_with(self, changed: dict[str, tuple[int, int]] | None = None,
                    added: dict[str, int] | None = None,
                    removed: dict[str, int] | None = None) -> SnapshotDiff:
        return SnapshotDiff(
            changed=changed or {},
            added=added or {},
            removed=removed or {},
        )

    def test_assert_only_changed_passes_when_match(self) -> None:
        diff = self._diff_with(changed={"uart.intr_state.tx_done": (0, 1)})
        # Should not raise
        diff.assert_only_changed("uart.intr_state.*")

    def test_assert_only_changed_passes_with_multiple_globs(self) -> None:
        diff = self._diff_with(
            changed={"uart.intr_state.tx": (0, 1), "uart.data": (0, 5)},
        )
        diff.assert_only_changed("uart.intr_state.*", "uart.data")

    def test_assert_only_changed_raises_on_unexpected_path(self) -> None:
        diff = self._diff_with(
            changed={"uart.intr_state.tx": (0, 1), "gpio.dir": (0, 1)},
        )
        with pytest.raises(AssertionError) as exc:
            diff.assert_only_changed("uart.intr_state.*")
        assert "gpio.dir" in str(exc.value)

    def test_assert_only_changed_no_args_requires_empty_diff(self) -> None:
        empty = self._diff_with()
        empty.assert_only_changed()  # should not raise

        non_empty = self._diff_with(changed={"a": (0, 1)})
        with pytest.raises(AssertionError):
            non_empty.assert_only_changed()


# ---------------------------------------------------------------------------
# Diff: added / removed paths
# ---------------------------------------------------------------------------


class TestDiffAddedRemoved:
    def test_added_paths(self) -> None:
        before = Snapshot({"a": 1})
        after = Snapshot({"a": 1, "b": 2})
        diff = after.diff(before)
        assert diff.added == {"b": 2}
        assert diff.removed == {}
        assert diff.changed == {}

    def test_removed_paths(self) -> None:
        before = Snapshot({"a": 1, "b": 2})
        after = Snapshot({"a": 1})
        diff = after.diff(before)
        assert diff.added == {}
        assert diff.removed == {"b": 2}
        assert diff.changed == {}


# ---------------------------------------------------------------------------
# HTML repr (Jupyter)
# ---------------------------------------------------------------------------


class TestHTMLRepr:
    def test_repr_html_returns_html_with_changed(self) -> None:
        diff = SnapshotDiff(
            changed={"uart.control": (0x00, 0x22)},
            added={"new.path": 0xAB},
            removed={"old.path": 0xCD},
        )
        html = diff._repr_html_()
        assert "<table" in html
        assert "uart.control" in html
        assert "new.path" in html
        assert "old.path" in html

    def test_repr_html_empty_diff(self) -> None:
        diff = SnapshotDiff(changed={}, added={}, removed={})
        html = diff._repr_html_()
        assert "no differences" in html.lower()
