"""Tests for the CLI subcommand seam (Unit 24, sketch §21 / §22.8).

Pure-Python tests: no cmake, no generated module. The five sibling
modules under :mod:`peakrdl_pybind11.cli` are exercised through the
discovery seam (:func:`peakrdl_pybind11.cli.discover_subcommands`,
:func:`try_handle`, :func:`run_post_handlers`) and via direct calls
where the seam already has the relevant inputs.

The build-time ``strict-fields`` flag is verified by rendering the
``runtime.py.jinja`` template with ``strict_fields=False`` and
running the rendered code in an isolated namespace; the test asserts
that a :class:`DeprecationWarning` fires on import.
"""

from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from peakrdl_pybind11 import cli
from peakrdl_pybind11.cli import diff as cli_diff
from peakrdl_pybind11.cli import explore as cli_explore
from peakrdl_pybind11.cli import replay as cli_replay
from peakrdl_pybind11.cli import strict_fields as cli_strict
from peakrdl_pybind11.cli import watch as cli_watch
from peakrdl_pybind11.runtime import NotSupportedError


def _patch_import_failure(prefix: str) -> Any:
    """Return a context manager that raises ImportError for any ``prefix.*``.

    Used to simulate a missing soft-dep regardless of whether the package
    is actually installed. We intercept ``builtins.__import__`` rather
    than mutating :data:`sys.modules` so the soft-import paths under
    test (which use the ``__import__`` builtin via ``import`` statements)
    see the failure exactly as a real missing package would surface.
    """
    real_import = __import__

    def _fake_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if name == prefix or name.startswith(f"{prefix}."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, globals, locals, fromlist, level)

    return mock.patch("builtins.__import__", side_effect=_fake_import)


# ---------------------------------------------------------------------------
# Discovery seam
# ---------------------------------------------------------------------------


class TestDiscovery:
    """Verifies that all five sibling modules are picked up."""

    def test_iter_modules_finds_five_subcommands(self) -> None:
        names = {m.__name__.rsplit(".", 1)[1] for m in cli.iter_modules()}
        # Five Unit-24 modules; future units may add more, so allow
        # supersets but require these specific names to be present.
        assert {"explore", "diff", "replay", "watch", "strict_fields"}.issubset(names)

    def test_discover_subcommands_registers_each_flag(self) -> None:
        parser = argparse.ArgumentParser()
        group = parser.add_argument_group("exporter args")
        cli.discover_subcommands(group)
        # argparse exposes --foo / --foo-bar as ``--foo``/``--foo-bar``;
        # the sketch documents these specific flag names so we check for
        # the full set.
        action_strings = {opt for action in parser._actions for opt in action.option_strings}
        assert "--explore" in action_strings
        assert "--diff" in action_strings
        assert "--replay" in action_strings
        assert "--watch" in action_strings
        assert "--strict-fields" in action_strings


# ---------------------------------------------------------------------------
# --diff
# ---------------------------------------------------------------------------


def _write_snapshot(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestDiff:
    """Round-trip --diff with two saved snapshots; output mentions paths."""

    def test_compute_diff_reports_changed_added_removed(self, tmp_path: Path) -> None:
        snap_a = tmp_path / "a.json"
        snap_b = tmp_path / "b.json"
        _write_snapshot(snap_a, {"uart": {"control": 0x10, "status": 0x01}})
        _write_snapshot(snap_b, {"uart": {"control": 0x22, "status": 0x01, "data": 0xA5}})

        diff = cli_diff.compute_diff(snap_a, snap_b)
        # control changed, data added; status unchanged.
        changed_paths = {entry[0] for entry in diff["changed"]}
        added_paths = {entry[0] for entry in diff["added"]}
        assert "uart.control" in changed_paths
        assert "uart.data" in added_paths
        assert diff["removed"] == []

    def test_format_diff_text_mentions_changed_paths(self, tmp_path: Path) -> None:
        snap_a = tmp_path / "a.json"
        snap_b = tmp_path / "b.json"
        _write_snapshot(snap_a, {"uart": {"control": 0x10}})
        _write_snapshot(snap_b, {"uart": {"control": 0x22}})
        diff = cli_diff.compute_diff(snap_a, snap_b)
        rendered = cli_diff.format_diff(diff, html=False)
        assert "uart.control" in rendered
        assert "0x10" in rendered
        assert "0x22" in rendered

    def test_format_diff_html_emits_table(self, tmp_path: Path) -> None:
        snap_a = tmp_path / "a.json"
        snap_b = tmp_path / "b.json"
        _write_snapshot(snap_a, {"uart": {"control": 0x10}})
        _write_snapshot(snap_b, {"uart": {"control": 0x22}})
        diff = cli_diff.compute_diff(snap_a, snap_b)
        rendered = cli_diff.format_diff(diff, html=True)
        assert "<table>" in rendered
        assert "uart.control" in rendered

    def test_handle_writes_diff_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        snap_a = tmp_path / "a.json"
        snap_b = tmp_path / "b.json"
        _write_snapshot(snap_a, {"uart": {"control": 0x10}})
        _write_snapshot(snap_b, {"uart": {"control": 0x22}})
        options = argparse.Namespace(diff=[str(snap_a), str(snap_b)], diff_html=False)
        assert cli_diff.handle(options) is True
        captured = capsys.readouterr()
        assert "uart.control" in captured.out

    def test_handle_returns_false_when_diff_unset(self) -> None:
        options = argparse.Namespace(diff=None, diff_html=False)
        assert cli_diff.handle(options) is False

    def test_handle_raises_when_snapshot_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.json"
        snap_b = tmp_path / "b.json"
        _write_snapshot(snap_b, {})
        options = argparse.Namespace(diff=[str(missing), str(snap_b)], diff_html=False)
        with pytest.raises(FileNotFoundError):
            cli_diff.handle(options)


# ---------------------------------------------------------------------------
# --replay
# ---------------------------------------------------------------------------


class _FakeReplayMaster:
    """Minimal stand-in for a Unit-20 ``ReplayMaster``."""

    @classmethod
    def from_file(cls, path: str) -> "_FakeReplayMaster":
        instance = cls()
        instance.session_path = path
        return instance


class TestReplay:
    def test_handle_returns_false_when_replay_unset(self) -> None:
        options = argparse.Namespace(replay=None)
        assert cli_replay.handle(options) is False

    def test_handle_raises_when_session_missing(self, tmp_path: Path) -> None:
        options = argparse.Namespace(replay=str(tmp_path / "nope.json"))
        with pytest.raises(FileNotFoundError):
            cli_replay.handle(options)

    def test_handle_loads_replay_master(self, tmp_path: Path) -> None:
        session = tmp_path / "sess.json"
        session.write_text("{}", encoding="utf-8")
        options = argparse.Namespace(replay=str(session))
        with mock.patch.object(
            cli_replay, "_load_replay_master_class", return_value=_FakeReplayMaster
        ):
            assert cli_replay.handle(options) is True
        # The handler stashes the constructed master on options so a
        # downstream user-script (or REPL) can pick it up.
        assert isinstance(options.replay_master, _FakeReplayMaster)
        assert Path(options.replay_master.session_path).resolve() == session.resolve()

    def test_handle_raises_not_supported_when_replay_master_missing(
        self, tmp_path: Path
    ) -> None:
        session = tmp_path / "sess.json"
        session.write_text("{}", encoding="utf-8")
        options = argparse.Namespace(replay=str(session))
        with mock.patch.object(cli_replay, "_load_replay_master_class", return_value=None):
            with pytest.raises(NotSupportedError):
                cli_replay.handle(options)


# ---------------------------------------------------------------------------
# --watch
# ---------------------------------------------------------------------------


class TestWatch:
    def test_handle_returns_false_when_watch_unset(self) -> None:
        options = argparse.Namespace(watch=None)
        assert cli_watch.handle(options) is False

    def test_handle_raises_not_supported_when_watchdog_missing(
        self, tmp_path: Path
    ) -> None:
        rdl = tmp_path / "input.rdl"
        rdl.write_text("addrmap dummy {};", encoding="utf-8")
        options = argparse.Namespace(watch=str(rdl))

        with _patch_import_failure("watchdog"):
            with pytest.raises(NotSupportedError) as excinfo:
                cli_watch.handle(options)
        # Message points the user at the documented install path.
        assert "watchdog" in str(excinfo.value)
        assert "peakrdl-pybind11[notebook]" in str(excinfo.value)

    def test_import_watchdog_raises_not_supported_directly(self) -> None:
        """The helper itself raises so callers can probe without a flag set."""
        with _patch_import_failure("watchdog"):
            with pytest.raises(NotSupportedError):
                cli_watch.import_watchdog()


# ---------------------------------------------------------------------------
# --explore
# ---------------------------------------------------------------------------


class _FakeGeneratedModule:
    """Stand-in for a generated SoC module (the thing --explore imports)."""

    def __init__(self) -> None:
        self.created: list[Any] = []

    def create(self) -> Any:
        soc = object()
        self.created.append(soc)
        return soc


class TestExplore:
    def test_post_handle_noop_when_explore_unset(self) -> None:
        options = argparse.Namespace(explore=None, output=None)
        # No spawn_repl must be invoked. Patching the spawn function
        # gives us a clean assertion.
        with mock.patch.object(cli_explore, "spawn_repl") as spawn:
            cli_explore.post_handle(options)
        spawn.assert_not_called()

    def test_post_handle_spawns_repl_with_soc_in_namespace(
        self, tmp_path: Path
    ) -> None:
        fake = _FakeGeneratedModule()
        options = argparse.Namespace(explore="my_chip", output=str(tmp_path))
        captured: dict[str, Any] = {}

        def _spawn(namespace: dict[str, Any], banner: str = "") -> None:
            captured["namespace"] = namespace
            captured["banner"] = banner

        with mock.patch.object(cli_explore, "_import_generated_module", return_value=fake):
            with mock.patch.object(cli_explore, "spawn_repl", side_effect=_spawn):
                cli_explore.post_handle(options)

        assert "soc" in captured["namespace"]
        assert "my_chip" in captured["namespace"]
        assert captured["namespace"]["soc"] is fake.created[0]

    def test_post_handle_raises_when_module_missing_create(self, tmp_path: Path) -> None:
        class _NoCreate:
            pass

        options = argparse.Namespace(explore="busted", output=str(tmp_path))
        with mock.patch.object(cli_explore, "_import_generated_module", return_value=_NoCreate()):
            with mock.patch.object(cli_explore, "spawn_repl"):
                with pytest.raises(AttributeError):
                    cli_explore.post_handle(options)


# ---------------------------------------------------------------------------
# --strict-fields=<bool>
# ---------------------------------------------------------------------------


class TestStrictFields:
    def test_default_is_strict(self) -> None:
        options = argparse.Namespace()
        assert cli_strict.is_strict_from_options(options) is True

    @pytest.mark.parametrize(
        "literal,expected",
        [
            ("true", True),
            ("True", True),
            ("yes", True),
            ("on", True),
            ("1", True),
            ("false", False),
            ("False", False),
            ("no", False),
            ("off", False),
            ("0", False),
        ],
    )
    def test_parser_accepts_canonical_booleans(self, literal: str, expected: bool) -> None:
        assert cli_strict.parse_strict_fields_value(literal) is expected

    def test_parser_rejects_garbage(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            cli_strict.parse_strict_fields_value("nope")

    def test_argparse_round_trip(self) -> None:
        parser = argparse.ArgumentParser()
        cli_strict.add_arguments(parser)
        ns = parser.parse_args(["--strict-fields=false"])
        assert ns.strict_fields is False
        ns = parser.parse_args(["--strict-fields=true"])
        assert ns.strict_fields is True
        ns = parser.parse_args([])  # default
        assert ns.strict_fields is True

    @staticmethod
    def _exec_runtime_prologue(strict: bool) -> tuple[dict[str, Any], list[warnings.WarningMessage]]:
        """Render the runtime template prologue and exec it in isolation.

        We only need the prologue (everything up to the generated
        flag/enum classes) — the rest of the template references symbols
        that do not exist outside a real export. The native-import block
        is rewritten to a no-op so the exec does not need a real C
        extension.
        """
        from peakrdl_pybind11.exporter import Pybind11Exporter

        template = Pybind11Exporter().env.get_template("runtime.py.jinja")
        rendered = template.render(
            soc_name="dummy_soc",
            top_node=None,
            nodes={
                "regs": [],
                "flag_regs": [],
                "enum_regs": [],
                "register_members": {},
            },
            strict_fields=strict,
        )
        prologue, _, _ = rendered.partition("# Generated flag/enum types")
        cleaned = prologue.replace(
            "try:\n    from ._dummy_soc_native import *\n",
            "try:\n    pass\n",
        )
        ns: dict[str, Any] = {"__name__": f"_strict_fields_test_{strict}"}
        with warnings.catch_warnings(record=True) as collected:
            warnings.simplefilter("always")
            exec(compile(cleaned, "<runtime-test>", "exec"), ns)
        return ns, list(collected)

    def test_generated_init_emits_deprecation_warning(self) -> None:
        """``--strict-fields=false`` build must emit DeprecationWarning at import."""
        ns, collected = self._exec_runtime_prologue(strict=False)
        deprecations = [w for w in collected if issubclass(w.category, DeprecationWarning)]
        assert deprecations, (
            "Generated runtime with --strict-fields=false must emit a "
            "DeprecationWarning at import; got none."
        )
        assert any("strict-fields" in str(w.message) for w in deprecations)
        assert ns["_PEAKRDL_STRICT_FIELDS"] is False

    def test_generated_init_no_warning_when_strict(self) -> None:
        """The default (strict=True) build must NOT emit DeprecationWarning."""
        ns, collected = self._exec_runtime_prologue(strict=True)
        deprecations = [w for w in collected if issubclass(w.category, DeprecationWarning)]
        assert not deprecations, (
            f"Default build must not emit DeprecationWarning; got {deprecations!r}"
        )
        assert ns["_PEAKRDL_STRICT_FIELDS"] is True


# ---------------------------------------------------------------------------
# Pre/post-handle orchestration
# ---------------------------------------------------------------------------


class TestOrchestration:
    """The ``try_handle`` / ``run_post_handlers`` seam composes correctly."""

    def test_try_handle_returns_false_with_no_flags(self) -> None:
        options = argparse.Namespace(
            diff=None, replay=None, watch=None, explore=None, strict_fields=True
        )
        assert cli.try_handle(options) is False

    def test_try_handle_returns_true_when_diff_set(self, tmp_path: Path) -> None:
        snap_a = tmp_path / "a.json"
        snap_b = tmp_path / "b.json"
        _write_snapshot(snap_a, {})
        _write_snapshot(snap_b, {})
        options = argparse.Namespace(
            diff=[str(snap_a), str(snap_b)],
            diff_html=False,
            replay=None,
            watch=None,
            explore=None,
            strict_fields=True,
        )
        # Suppress the diff output during the orchestration test.
        with mock.patch("sys.stdout"):
            assert cli.try_handle(options) is True

    def test_run_post_handlers_invokes_explore_only_when_set(self) -> None:
        # No explore set -> spawn_repl must not fire even though the
        # post-handler chain runs.
        options = argparse.Namespace(
            diff=None, replay=None, watch=None, explore=None, strict_fields=True, output=None
        )
        with mock.patch.object(cli_explore, "spawn_repl") as spawn:
            cli.run_post_handlers(options)
        spawn.assert_not_called()
