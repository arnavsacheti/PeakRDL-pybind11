"""
``--replay <session.json>`` CLI subcommand (Unit 24).

Loads a recorded ``RecordingMaster`` session from disk and replays it
against an attached master via :class:`ReplayMaster.from_file`. The
replay surface is owned by Unit 20; until that lands, this module
soft-imports the implementation and emits a clear error if the
sibling has not yet shipped.

When ``--replay`` is set, the exporter skips its primary export and
this handler claims the run.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger("peakrdl_pybind11.cli.replay")

__all__ = ["add_arguments", "handle"]


def add_arguments(arg_group: argparse._ActionsContainer) -> None:
    """Register ``--replay``."""
    arg_group.add_argument(
        "--replay",
        dest="replay",
        metavar="SESSION_JSON",
        default=None,
        help=(
            "Replay a recorded RecordingMaster session against a freshly "
            "built master. Skips the primary export. The session file is "
            "the JSON produced by RecordingMaster.save (or a tracer's "
            "save())."
        ),
    )


def _load_replay_master_class() -> type | None:
    """Try every documented import path for ``ReplayMaster``.

    Returns the class or ``None`` if Unit 20 has not yet landed.
    """
    candidates = (
        "peakrdl_pybind11.masters.replay",
        "peakrdl_pybind11.masters",
        "peakrdl_pybind11.runtime.replay",
        "peakrdl_pybind11.runtime",
    )
    for module_name in candidates:
        try:
            module = __import__(module_name, fromlist=["ReplayMaster"])
        except ImportError:
            continue
        replay_master = getattr(module, "ReplayMaster", None)
        if replay_master is not None:
            return replay_master
    return None


def handle(options: argparse.Namespace) -> bool:
    """Replay the session if ``--replay`` was set."""
    session_arg: str | None = getattr(options, "replay", None)
    if not session_arg:
        return False

    session_path = Path(session_arg)
    if not session_path.exists():
        raise FileNotFoundError(f"replay session not found: {session_path}")

    replay_master_cls = _load_replay_master_class()
    if replay_master_cls is None:
        # Replaying from a session is meaningless without a working
        # ReplayMaster — fail loudly so the user installs / waits for
        # Unit 20 instead of getting silent confusion.
        from ..runtime import NotSupportedError

        raise NotSupportedError(
            "ReplayMaster is not available in this build. The --replay flag "
            "depends on Unit 20 (replay master) which has not yet landed in "
            "this branch. See docs/IDEAL_API_SKETCH.md §13.6."
        )

    # Each user invocation runs against a fresh master instance, so the
    # replay does not interleave with any other state. The handler
    # itself does not own a SoC tree (that requires the user's own
    # generated module); we drive the master directly from the session
    # file and let it replay every transaction.
    master = replay_master_cls.from_file(str(session_path))
    sys.stdout.write(
        f"Loaded replay session {session_path} into {type(master).__name__}\n"
    )
    sys.stdout.flush()
    # Caller-controlled replay: the user may want to wire the replay
    # master into their own tree before calling ``execute``. We expose
    # the constructed master via ``options.replay_master`` so post-
    # invocation Python (e.g. an ``--explore`` REPL) can pick it up.
    options.replay_master = master  # type: ignore[attr-defined]
    return True
