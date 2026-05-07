"""
``--diff <snapA> <snapB>`` CLI subcommand (Unit 24).

Diffs two snapshot JSON files and prints (or writes to HTML) the
result. When ``--diff`` is set, the exporter skips its primary export
and this handler claims the run.

The diff itself is delegated to :class:`Snapshot.diff` from Unit 8 —
which may not yet be present on every branch. We soft-import the
runtime ``snapshots`` module and fall back to a minimal local
JSON-shape diff so the CLI keeps working in the half-landed world of
sibling units. The minimal diff is documented as a fallback (so
nobody confuses it for the real implementation) and points at the
sketch.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from html import escape as html_escape
from pathlib import Path
from typing import Any

logger = logging.getLogger("peakrdl_pybind11.cli.diff")

__all__ = ["add_arguments", "compute_diff", "format_diff", "handle"]


def add_arguments(arg_group: argparse._ActionsContainer) -> None:
    """Register ``--diff`` and the companion ``--html`` flag."""
    arg_group.add_argument(
        "--diff",
        dest="diff",
        nargs=2,
        metavar=("SNAP_A", "SNAP_B"),
        default=None,
        help=(
            "Diff two snapshot JSON files (see Snapshot.to_json / "
            ".from_json). Skips the primary export. Output is a text "
            "diff by default; pass --html to render HTML to stdout."
        ),
    )
    arg_group.add_argument(
        "--html",
        dest="diff_html",
        action="store_true",
        default=False,
        help="When used with --diff, render the diff as HTML on stdout.",
    )


# ---------------------------------------------------------------------------
# Snapshot import (soft) and fallback diff
# ---------------------------------------------------------------------------


def _load_snapshot_class() -> type | None:
    """Try every documented import path for the Snapshot type.

    Returns ``None`` if none are available — callers fall back to the
    JSON-level diff in that case. We tolerate a missing Snapshot
    implementation so the CLI is testable before Unit 8 lands.
    """
    candidates = (
        ("peakrdl_pybind11.runtime.snapshots", "Snapshot"),
        ("peakrdl_pybind11.runtime", "Snapshot"),
    )
    for module_name, attr in candidates:
        try:
            module = __import__(module_name, fromlist=[attr])
        except ImportError:
            continue
        snapshot_cls = getattr(module, attr, None)
        if snapshot_cls is not None:
            return snapshot_cls
    return None


def _load_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _flatten(obj: object, prefix: str = "") -> dict[str, Any]:
    """Flatten a JSON object into ``{path: leaf_value}``.

    Used by the fallback diff. Keeps the shape narrow on purpose: the
    real :class:`Snapshot.diff` will produce richer output (typed
    rows, side-effect annotations, etc.) once Unit 8 lands.
    """
    flat: dict[str, Any] = {}
    if isinstance(obj, dict):
        # Treat ``{"path": ..., "value": ...}`` snapshot rows specially
        # so the fallback diff produces useful output even when the
        # snapshot uses Unit 8's row-list shape.
        if "path" in obj and "value" in obj and not any(
            isinstance(v, (dict, list)) for v in obj.values()
        ):
            flat[str(obj["path"]) or prefix or "<root>"] = obj["value"]
            return flat
        for key, value in obj.items():
            sub = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten(value, sub))
    elif isinstance(obj, list):
        # Snapshot lists are typically lists of row dicts; recurse so
        # row paths bubble up.
        for index, value in enumerate(obj):
            sub = f"{prefix}[{index}]" if prefix else f"[{index}]"
            flat.update(_flatten(value, sub))
    else:
        flat[prefix or "<root>"] = obj
    return flat


def compute_diff(snap_a_path: Path, snap_b_path: Path) -> dict[str, Any]:
    """Compute the diff between two snapshot files.

    Returns a dict with three keys::

        {
            "changed": [(path, before, after), ...],
            "added":   [(path, value), ...],
            "removed": [(path, value), ...],
        }

    Real snapshots from Unit 8 have a ``.diff`` method that produces a
    richer object; if that surface is available we delegate to it and
    coerce its output into the same dict shape.
    """
    snapshot_cls = _load_snapshot_class()
    if snapshot_cls is not None:
        try:
            snap_a = snapshot_cls.from_json(str(snap_a_path))  # type: ignore[attr-defined]
            snap_b = snapshot_cls.from_json(str(snap_b_path))  # type: ignore[attr-defined]
            real = snap_b.diff(snap_a)
            # Best-effort coercion: a Unit-8 SnapshotDiff exposes
            # ``changed`` / ``added`` / ``removed`` (possibly as lists
            # of rows, possibly as something richer). Pull those keys
            # out via attribute *or* mapping access.
            return _coerce_real_diff(real)
        except (AttributeError, TypeError, ValueError):
            # If the real Snapshot is present but its ``from_json`` or
            # ``diff`` does not match the expected interface, fall back
            # to JSON-level diff rather than crashing.
            logger.debug(
                "Snapshot.diff returned an unrecognised shape; using JSON fallback",
                exc_info=True,
            )

    flat_a = _flatten(_load_json(snap_a_path))
    flat_b = _flatten(_load_json(snap_b_path))
    keys_a = set(flat_a)
    keys_b = set(flat_b)
    return {
        "changed": sorted(
            (path, flat_a[path], flat_b[path])
            for path in keys_a & keys_b
            if flat_a[path] != flat_b[path]
        ),
        "added": sorted((path, flat_b[path]) for path in keys_b - keys_a),
        "removed": sorted((path, flat_a[path]) for path in keys_a - keys_b),
    }


def _coerce_real_diff(diff: object) -> dict[str, Any]:
    """Best-effort conversion of a real ``SnapshotDiff`` to the dict shape."""
    def _get(name: str) -> Any:  # noqa: ANN401
        # Support both attribute and mapping access — Unit 8 may pick
        # either, and we don't want the CLI to be fragile to that
        # choice.
        if hasattr(diff, name):
            return getattr(diff, name)
        if isinstance(diff, dict) and name in diff:
            return diff[name]
        return []

    return {
        "changed": list(_get("changed")),
        "added": list(_get("added")),
        "removed": list(_get("removed")),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_value(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"0x{value:x}" if value >= 0 else str(value)
    return repr(value)


def _format_text(diff: dict[str, Any]) -> str:
    lines: list[str] = []
    total = len(diff["changed"]) + len(diff["added"]) + len(diff["removed"])
    if total == 0:
        return "no differences\n"
    lines.append(f"{total} differences")
    for row in diff["changed"]:
        path, before, after = row[0], row[1], row[2]
        lines.append(f"  changed  {path}: {_format_value(before)} -> {_format_value(after)}")
    for row in diff["added"]:
        path, value = row[0], row[1]
        lines.append(f"  added    {path}: {_format_value(value)}")
    for row in diff["removed"]:
        path, value = row[0], row[1]
        lines.append(f"  removed  {path}: {_format_value(value)}")
    return "\n".join(lines) + "\n"


def _format_html(diff: dict[str, Any]) -> str:
    rows: list[str] = []
    for row in diff["changed"]:
        path, before, after = row[0], row[1], row[2]
        rows.append(
            "<tr class='changed'><td>changed</td>"
            f"<td>{html_escape(str(path))}</td>"
            f"<td>{html_escape(_format_value(before))}</td>"
            f"<td>{html_escape(_format_value(after))}</td></tr>"
        )
    for row in diff["added"]:
        path, value = row[0], row[1]
        rows.append(
            "<tr class='added'><td>added</td>"
            f"<td>{html_escape(str(path))}</td>"
            "<td></td>"
            f"<td>{html_escape(_format_value(value))}</td></tr>"
        )
    for row in diff["removed"]:
        path, value = row[0], row[1]
        rows.append(
            "<tr class='removed'><td>removed</td>"
            f"<td>{html_escape(str(path))}</td>"
            f"<td>{html_escape(_format_value(value))}</td>"
            "<td></td></tr>"
        )
    return (
        "<!doctype html><html><body><table>"
        "<thead><tr><th>kind</th><th>path</th><th>before</th>"
        "<th>after</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></body></html>\n"
    )


def format_diff(diff: dict[str, Any], *, html: bool = False) -> str:
    """Render a diff dict as text (default) or HTML."""
    return _format_html(diff) if html else _format_text(diff)


def handle(options: argparse.Namespace) -> bool:
    """Run the diff if ``--diff`` was set; report whether we claimed the run."""
    diff_arg: list[str] | None = getattr(options, "diff", None)
    if not diff_arg:
        return False

    snap_a_path = Path(diff_arg[0])
    snap_b_path = Path(diff_arg[1])
    for path in (snap_a_path, snap_b_path):
        if not path.exists():
            raise FileNotFoundError(f"snapshot file not found: {path}")

    diff = compute_diff(snap_a_path, snap_b_path)
    rendered = format_diff(diff, html=bool(getattr(options, "diff_html", False)))
    sys.stdout.write(rendered)
    sys.stdout.flush()
    return True
