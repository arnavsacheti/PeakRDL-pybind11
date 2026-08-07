#!/usr/bin/env python3
"""Render the checked-in benchmark JSON as documentation SVGs."""

from __future__ import annotations

import argparse
import html
import json
import math
from collections.abc import Callable
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPOSITORY_ROOT / "benchmarks" / "results" / "release_history.json"
DEFAULT_SCALE_INPUT = REPOSITORY_ROOT / "benchmarks" / "results" / "scale_envelope.json"
DEFAULT_SPARSE_INPUT = REPOSITORY_ROOT / "benchmarks" / "results" / "sparse_scale_envelope.json"
DEFAULT_FIELD_PROFILE_INPUT = REPOSITORY_ROOT / "benchmarks" / "results" / "field_profile_envelope.json"
DEFAULT_OUTPUT_PROFILE_INPUT = REPOSITORY_ROOT / "benchmarks" / "results" / "output_profile_envelope.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "docs" / "_static" / "benchmarks"

FIELD_PROFILE_TITLE = "Field-profile comparison at the largest shared register count"
FIELD_PROFILE_DESCRIPTION = (
    "Four grouped horizontal bar charts compare total fields, source export time, generation peak "
    "resident memory, and generated source size for six field layouts at the largest register count "
    "shared by every profile."
)
FIELD_PROFILE_IDS = (
    "word32",
    "bytes4",
    "mixed-1-8-4",
    "nibbles5",
    "flags8-packed",
    "flags8-spread",
)
FIELD_PROFILE_LABELS = {
    "word32": "word32",
    "bytes4": "bytes4",
    "mixed-1-8-4": "mixed 1/8/4",
    "nibbles5": "nibbles5",
    "flags8-packed": "flags8 packed",
    "flags8-spread": "flags8 spread",
}
OUTPUT_PROFILE_IDS = ("full", "compact", "minimal")
OUTPUT_PROFILE_TITLE = "Generated-output profiles at the largest shared register count"
OUTPUT_PROFILE_DESCRIPTION = (
    "Four horizontal bar charts compare total generated source, source export time, generation peak "
    "resident memory, and a deflate-compressed package-text proxy for full, compact, and minimal "
    "output profiles at the largest register count shared by every profile. The deflate proxy is "
    "not a wheel size."
)


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1
    magnitude = 10 ** math.floor(math.log10(value))
    normalized = value / magnitude
    step = 1 if normalized <= 1 else 2 if normalized <= 2 else 5 if normalized <= 5 else 10
    return step * magnitude


def _format_tick(value: float) -> str:
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.0f}"
    if value >= 1:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _polyline(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def _chart(
    releases: list[dict],
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    title: str,
    unit: str,
    series: list[tuple[str, str, str]],
    show_x_labels: bool = True,
) -> list[str]:
    left, right, top, bottom = 54, 14, 30, 42 if show_x_labels else 20
    plot_x = x + left
    plot_y = y + top
    plot_width = width - left - right
    plot_height = height - top - bottom
    maximum = _nice_ceiling(max(float(release[key]) for _, key, _ in series for release in releases) * 1.08)
    x_step = plot_width / max(len(releases) - 1, 1)

    output = [
        f'<g class="panel" aria-label="{html.escape(title)}">',
        f'<text class="panel-title" x="{x:.1f}" y="{y + 16:.1f}">{html.escape(title)}</text>',
        f'<text class="unit" x="{x + width:.1f}" y="{y + 16:.1f}" text-anchor="end">{html.escape(unit)}</text>',
    ]

    for tick in range(5):
        value = maximum * tick / 4
        tick_y = plot_y + plot_height - plot_height * tick / 4
        output.extend(
            (
                f'<line class="grid" x1="{plot_x:.1f}" y1="{tick_y:.1f}" '
                f'x2="{plot_x + plot_width:.1f}" y2="{tick_y:.1f}"/>',
                f'<text class="tick" x="{plot_x - 8:.1f}" y="{tick_y + 4:.1f}" '
                f'text-anchor="end">{_format_tick(value)}</text>',
            )
        )

    output.append(
        f'<line class="axis" x1="{plot_x:.1f}" y1="{plot_y + plot_height:.1f}" '
        f'x2="{plot_x + plot_width:.1f}" y2="{plot_y + plot_height:.1f}"/>'
    )

    if show_x_labels:
        for index, release in enumerate(releases):
            point_x = plot_x + index * x_step
            output.append(
                f'<text class="tick" x="{point_x:.1f}" y="{plot_y + plot_height + 22:.1f}" '
                f'text-anchor="middle">{html.escape(release["ref"])}</text>'
            )

    for series_index, (label, key, css_class) in enumerate(series):
        points = []
        for index, release in enumerate(releases):
            point_x = plot_x + index * x_step
            point_y = plot_y + plot_height * (1 - float(release[key]) / maximum)
            points.append((point_x, point_y))
        output.append(
            f'<polyline class="series {css_class}" points="{_polyline(points)}" '
            f'aria-label="{html.escape(label)}"/>'
        )
        for point_x, point_y in points:
            output.append(
                f'<circle class="point {css_class}" cx="{point_x:.1f}" cy="{point_y:.1f}" r="3.5"/>'
            )

        last_x, last_y = points[-1]
        if len(series) == 1:
            label_offset = -9
        else:
            first_is_higher = float(releases[-1][series[0][1]]) >= float(releases[-1][series[1][1]])
            label_offset = (
                (-9 if first_is_higher else 16) if series_index == 0 else (16 if first_is_higher else -9)
            )
        output.append(
            f'<text class="value-label" x="{last_x - 4:.1f}" y="{last_y + label_offset:.1f}" '
            f'text-anchor="end">{_format_tick(float(releases[-1][key]))}</text>'
        )

    if len(series) > 1:
        legend_x = plot_x + 7
        legend_y = plot_y + 10
        for index, (label, _, css_class) in enumerate(series):
            item_y = legend_y + index * 18
            output.extend(
                (
                    f'<line class="series {css_class}" x1="{legend_x:.1f}" y1="{item_y:.1f}" '
                    f'x2="{legend_x + 18:.1f}" y2="{item_y:.1f}"/>',
                    f'<text class="legend" x="{legend_x + 25:.1f}" y="{item_y + 4:.1f}">{html.escape(label)}</text>',
                )
            )

    output.append("</g>")
    return output


def _svg_start(width: int, height: int, title: str, description: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="title description">',
        f'<title id="title">{html.escape(title)}</title>',
        f'<desc id="description">{html.escape(description)}</desc>',
        """<style>
            :root { color-scheme: light dark; }
            .panel-title, .value-label, .legend { fill: #172033; }
            .panel-title { font: 600 14px system-ui, sans-serif; }
            .unit, .tick { fill: #5d6678; font: 11px system-ui, sans-serif; }
            .legend, .value-label { font: 11px system-ui, sans-serif; }
            .grid { stroke: #d9dde6; stroke-width: 1; }
            .axis { stroke: #838b9b; stroke-width: 1; }
            .series { fill: none; stroke-width: 2.25; stroke-linejoin: round; stroke-linecap: round; }
            .point { stroke-width: 2; }
            .series-a { stroke: #2563eb; }
            .series-b { stroke: #c2410c; }
            .point.series-a { fill: #2563eb; }
            .point.series-b { fill: #c2410c; }
            @media (prefers-color-scheme: dark) {
                .panel-title, .value-label, .legend { fill: #ecf0f7; }
                .unit, .tick { fill: #aab2c2; }
                .grid { stroke: #3b4352; }
                .axis { stroke: #7d8799; }
                .series-a { stroke: #60a5fa; }
                .series-b { stroke: #fb923c; }
                .point.series-a { fill: #60a5fa; }
                .point.series-b { fill: #fb923c; }
            }
        </style>""",
    ]


def _render_pipeline(releases: list[dict]) -> str:
    parts = _svg_start(
        920,
        570,
        "Generation and build cost across PeakRDL-pybind11 releases",
        "Four line charts compare generation time, wheel build time, generation peak memory, and build peak memory from v0.2.0 through v0.8.5.",
    )
    panels = (
        (20, 12, "RDL to sources", "milliseconds", [("Generation", "generation_ms", "series-a")]),
        (470, 12, "Sources to wheel", "seconds", [("Build", "build_s", "series-a")]),
        (
            20,
            288,
            "Generation peak memory",
            "MiB RSS",
            [("Generation", "generation_peak_rss_mib", "series-a")],
        ),
        (
            470,
            288,
            "Build peak memory",
            "MiB RSS",
            [("Build", "build_peak_rss_mib", "series-a")],
        ),
    )
    for x, y, title, unit, series in panels:
        parts.extend(
            _chart(
                releases,
                x=x,
                y=y,
                width=430,
                height=264,
                title=title,
                unit=unit,
                series=series,
            )
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _render_runtime_and_size(releases: list[dict]) -> str:
    parts = _svg_start(
        920,
        300,
        "Runtime access latency and artifact size across PeakRDL-pybind11 releases",
        "Two line charts compare register read and write latency and generated source and wheel size from v0.2.0 through v0.8.5.",
    )
    parts.extend(
        _chart(
            releases,
            x=20,
            y=12,
            width=430,
            height=276,
            title="Register access latency",
            unit="microseconds per call",
            series=[("Read", "read_us", "series-a"), ("Write", "write_us", "series-b")],
        )
    )
    parts.extend(
        _chart(
            releases,
            x=470,
            y=12,
            width=430,
            height=276,
            title="Generated artifact size",
            unit="KiB",
            series=[("Sources", "source_kib", "series-a"), ("Wheel", "wheel_kib", "series-b")],
        )
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _scale_label(registers: int) -> str:
    if registers > 100_000:
        return "100k+"
    if registers >= 1_000:
        return f"{registers // 1_000}k"
    return str(registers)


def _format_count(value: float) -> str:
    """Format an integral chart value without losing useful separators."""
    return f"{value:,.0f}"


def _field_profile_rows(payload: dict) -> tuple[int, list[dict]]:
    """Return the six profile measurements at their largest common size.

    Schema version 2 intentionally keeps profile metadata separate from the
    observations.  Matching on the profile identifier makes the renderer
    tolerant of a collector changing its serialization order, while retaining
    the documented comparison order in the resulting figure.
    """
    if payload.get("schema_version") != 2:
        raise ValueError("field-profile input must use schema_version 2")
    if payload.get("kind") != "field-profile-matrix":
        raise ValueError("field-profile input must have kind 'field-profile-matrix'")

    benchmark = payload.get("benchmark")
    profiles = benchmark.get("profiles") if isinstance(benchmark, dict) else None
    series = payload.get("series")
    if not isinstance(profiles, list) or not isinstance(series, list):
        raise ValueError("field-profile input must contain benchmark.profiles and series lists")

    profile_labels: dict[str, str] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError("field-profile metadata entries must be objects")
        profile_id = profile.get("id")
        label = profile.get("label")
        if not isinstance(profile_id, str) or not isinstance(label, str):
            raise ValueError("each field-profile metadata entry needs string id and label values")
        profile_labels[profile_id] = label

    points_by_profile: dict[str, list[dict]] = {}
    for item in series:
        if not isinstance(item, dict):
            raise ValueError("field-profile series entries must be objects")
        # Accept the collector's ``field_profile`` key and the shorter aliases
        # used by earlier schema-v2 design drafts.
        profile_id = item.get("profile") or item.get("profile_id") or item.get("field_profile")
        points = item.get("points")
        if not isinstance(profile_id, str) or not isinstance(points, list):
            raise ValueError("each field-profile series needs a profile and points list")
        if profile_id in points_by_profile:
            raise ValueError(f"duplicate field-profile series for {profile_id!r}")
        points_by_profile[profile_id] = points

    missing = [profile_id for profile_id in FIELD_PROFILE_IDS if profile_id not in points_by_profile]
    if missing:
        raise ValueError(f"field-profile input is missing series: {', '.join(missing)}")
    missing_labels = [profile_id for profile_id in FIELD_PROFILE_IDS if profile_id not in profile_labels]
    if missing_labels:
        raise ValueError(f"field-profile input is missing profile metadata: {', '.join(missing_labels)}")

    register_sets: list[set[int]] = []
    for profile_id in FIELD_PROFILE_IDS:
        points = points_by_profile[profile_id]
        if not points:
            raise ValueError(f"field-profile series {profile_id!r} has no points")
        try:
            register_sets.append({int(point["registers"]) for point in points})
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"field-profile series {profile_id!r} has an invalid registers value") from exc

    shared_registers = set.intersection(*register_sets)
    if not shared_registers:
        raise ValueError("field-profile series have no shared register count")
    largest_registers = max(shared_registers)
    if largest_registers < 1:
        raise ValueError("field-profile shared register count must be positive")

    rows = []
    required_metrics = ("fields", "export_s", "generation_peak_rss_mib", "source_bytes")
    for profile_id in FIELD_PROFILE_IDS:
        selected = [
            point for point in points_by_profile[profile_id] if int(point["registers"]) == largest_registers
        ]
        if len(selected) != 1:
            raise ValueError(
                f"field-profile series {profile_id!r} must have exactly one point at {largest_registers} registers"
            )
        point = selected[0]
        try:
            values = {metric: float(point[metric]) for metric in required_metrics}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"field-profile series {profile_id!r} has an invalid measurement at {largest_registers} registers"
            ) from exc
        if any(not math.isfinite(value) or value < 0 for value in values.values()):
            raise ValueError(
                f"field-profile series {profile_id!r} has a non-finite or negative measurement "
                f"at {largest_registers} registers"
            )
        if not values["fields"].is_integer():
            raise ValueError(
                f"field-profile series {profile_id!r} has a non-integral field total at {largest_registers} registers"
            )
        rows.append(
            {
                "label": FIELD_PROFILE_LABELS[profile_id],
                "description": profile_labels[profile_id],
                **values,
            }
        )
    return largest_registers, rows


def _output_profile_rows(payload: dict) -> tuple[int, list[dict]]:
    """Return output-profile measurements at their largest common size."""
    if payload.get("schema_version") != 2:
        raise ValueError("output-profile input must use schema_version 2")
    if payload.get("kind") != "output-profile-matrix":
        raise ValueError("output-profile input must have kind 'output-profile-matrix'")

    benchmark = payload.get("benchmark")
    profiles = benchmark.get("profiles") if isinstance(benchmark, dict) else None
    series = payload.get("series")
    if not isinstance(profiles, list) or not isinstance(series, list):
        raise ValueError("output-profile input must contain benchmark.profiles and series lists")

    metadata_ids = {
        profile.get("id")
        for profile in profiles
        if isinstance(profile, dict) and isinstance(profile.get("id"), str)
    }
    missing_metadata = [profile_id for profile_id in OUTPUT_PROFILE_IDS if profile_id not in metadata_ids]
    if missing_metadata:
        raise ValueError(f"output-profile input is missing profile metadata: {', '.join(missing_metadata)}")

    points_by_profile: dict[str, list[dict]] = {}
    for item in series:
        if not isinstance(item, dict):
            raise ValueError("output-profile series entries must be objects")
        profile_id = item.get("profile")
        points = item.get("points")
        if not isinstance(profile_id, str) or not isinstance(points, list):
            raise ValueError("each output-profile series needs a profile and points list")
        if profile_id in points_by_profile:
            raise ValueError(f"duplicate output-profile series for {profile_id!r}")
        points_by_profile[profile_id] = points

    missing = [profile_id for profile_id in OUTPUT_PROFILE_IDS if profile_id not in points_by_profile]
    if missing:
        raise ValueError(f"output-profile input is missing series: {', '.join(missing)}")

    register_sets: list[set[int]] = []
    for profile_id in OUTPUT_PROFILE_IDS:
        points = points_by_profile[profile_id]
        if not points:
            raise ValueError(f"output-profile series {profile_id!r} has no points")
        try:
            register_sets.append({int(point["registers"]) for point in points})
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"output-profile series {profile_id!r} has an invalid registers value") from exc

    shared_registers = set.intersection(*register_sets)
    if not shared_registers:
        raise ValueError("output-profile series have no shared register count")
    largest_registers = max(shared_registers)
    if largest_registers < 1:
        raise ValueError("output-profile shared register count must be positive")

    required_metrics = (
        "total_bytes",
        "cpp_bytes",
        "python_bytes",
        "package_bytes",
        "root_mirror_bytes",
        "schema_bytes",
        "stub_bytes",
        "package_text_deflate_bytes_proxy",
        "export_s",
        "peak_rss_mib",
    )
    rows = []
    for profile_id in OUTPUT_PROFILE_IDS:
        selected = [
            point for point in points_by_profile[profile_id] if int(point["registers"]) == largest_registers
        ]
        if len(selected) != 1:
            raise ValueError(
                f"output-profile series {profile_id!r} must have exactly one point at {largest_registers} registers"
            )
        point = selected[0]
        try:
            values = {metric: float(point[metric]) for metric in required_metrics}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"output-profile series {profile_id!r} has an invalid measurement at "
                f"{largest_registers} registers"
            ) from exc
        if any(not math.isfinite(value) or value < 0 for value in values.values()):
            raise ValueError(
                f"output-profile series {profile_id!r} has a non-finite or negative measurement "
                f"at {largest_registers} registers"
            )
        rows.append(
            {
                "label": profile_id.title(),
                **values,
                "total_gib": values["total_bytes"] / (1024**3),
                "peak_rss_gib": values["peak_rss_mib"] / 1024,
                "deflate_proxy_mib": values["package_text_deflate_bytes_proxy"] / (1024**2),
            }
        )
    return largest_registers, rows


def _horizontal_bar_chart(
    rows: list[dict],
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    title: str,
    unit: str,
    key: str,
    value_formatter: Callable[[float], str],
) -> list[str]:
    """Render one accessible horizontal-bar panel for the profile matrix."""
    left, right, top, bottom = 142, 24, 40, 36
    plot_x = x + left
    plot_y = y + top
    plot_width = width - left - right
    plot_height = height - top - bottom
    maximum = _nice_ceiling(max(float(row[key]) for row in rows) * 1.08)
    row_height = plot_height / len(rows)
    bar_height = min(24, row_height * 0.58)

    output = [
        f'<g class="panel" aria-label="{html.escape(title)}">',
        f'<text class="panel-title" x="{x:.1f}" y="{y + 17:.1f}">{html.escape(title)}</text>',
        f'<text class="unit" x="{x + width:.1f}" y="{y + 17:.1f}" text-anchor="end">{html.escape(unit)}</text>',
    ]
    for tick in range(5):
        value = maximum * tick / 4
        tick_x = plot_x + plot_width * tick / 4
        output.extend(
            (
                f'<line class="grid" x1="{tick_x:.1f}" y1="{plot_y:.1f}" '
                f'x2="{tick_x:.1f}" y2="{plot_y + plot_height:.1f}"/>',
                f'<text class="tick" x="{tick_x:.1f}" y="{plot_y + plot_height + 22:.1f}" '
                f'text-anchor="middle">{html.escape(value_formatter(value))}</text>',
            )
        )

    output.append(
        f'<line class="axis" x1="{plot_x:.1f}" y1="{plot_y + plot_height:.1f}" '
        f'x2="{plot_x + plot_width:.1f}" y2="{plot_y + plot_height:.1f}"/>'
    )
    for index, row in enumerate(rows):
        center_y = plot_y + row_height * (index + 0.5)
        bar_width = plot_width * float(row[key]) / maximum
        bar_y = center_y - bar_height / 2
        css_class = f"profile-{index}"
        output.extend(
            (
                f'<text class="bar-label" x="{plot_x - 10:.1f}" y="{center_y + 4:.1f}" '
                f'text-anchor="end">{html.escape(str(row["label"]))}</text>',
                f'<rect class="bar {css_class}" x="{plot_x:.1f}" y="{bar_y:.1f}" '
                f'width="{bar_width:.1f}" height="{bar_height:.1f}" rx="2" '
                f'aria-label="{html.escape(str(row["label"]))}: {html.escape(value_formatter(float(row[key])))} {html.escape(unit)}"/>',
            )
        )
        label_x = plot_x + bar_width + 6
        text_anchor = "start"
        if label_x > plot_x + plot_width - 38:
            label_x = plot_x + bar_width - 6
            text_anchor = "end"
        output.append(
            f'<text class="bar-value" x="{label_x:.1f}" y="{center_y + 4:.1f}" '
            f'text-anchor="{text_anchor}">{html.escape(value_formatter(float(row[key])))}</text>'
        )
    output.append("</g>")
    return output


def _bar_chart_style() -> str:
    return """<style>
            .bar-label, .bar-value { fill: #172033; font: 11px system-ui, sans-serif; }
            .bar { stroke: none; }
            .profile-0 { fill: #2563eb; }
            .profile-1 { fill: #0f766e; }
            .profile-2 { fill: #7c3aed; }
            .profile-3 { fill: #c2410c; }
            .profile-4 { fill: #be123c; }
            .profile-5 { fill: #4d7c0f; }
            @media (prefers-color-scheme: dark) {
                .bar-label, .bar-value { fill: #ecf0f7; }
                .profile-0 { fill: #60a5fa; }
                .profile-1 { fill: #2dd4bf; }
                .profile-2 { fill: #a78bfa; }
                .profile-3 { fill: #fb923c; }
                .profile-4 { fill: #fb7185; }
                .profile-5 { fill: #a3e635; }
            }
        </style>"""


def _render_field_profile_comparison(payload: dict) -> str:
    largest_registers, rows = _field_profile_rows(payload)
    title = f"{FIELD_PROFILE_TITLE} ({_format_count(largest_registers)} registers)"
    description = f"{FIELD_PROFILE_DESCRIPTION} The shared comparison point is {_format_count(largest_registers)} registers."
    parts = _svg_start(1160, 720, title, description)
    parts.append(_bar_chart_style())
    panels = (
        (20, 18, "Total fields", "fields", "fields", _format_count),
        (600, 18, "Source export time", "seconds", "export_s", _format_tick),
        (20, 374, "Generation peak memory", "MiB RSS", "generation_peak_rss_mib", _format_tick),
        (600, 374, "Generated source footprint", "GiB", "source_gib", _format_tick),
    )
    for row in rows:
        row["source_gib"] = row["source_bytes"] / (1024**3)
    for x, y, panel_title, unit, key, formatter in panels:
        parts.extend(
            _horizontal_bar_chart(
                rows,
                x=x,
                y=y,
                width=540,
                height=320,
                title=panel_title,
                unit=unit,
                key=key,
                value_formatter=formatter,
            )
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _render_output_profile_comparison(payload: dict) -> str:
    largest_registers, rows = _output_profile_rows(payload)
    title = f"{OUTPUT_PROFILE_TITLE} ({_format_count(largest_registers)} registers)"
    description = (
        f"{OUTPUT_PROFILE_DESCRIPTION} The shared comparison point is "
        f"{_format_count(largest_registers)} registers."
    )
    parts = _svg_start(1160, 720, title, description)
    parts.append(_bar_chart_style())
    panels = (
        (20, 18, "Total generated source", "GiB", "total_gib", _format_tick),
        (600, 18, "Source export time", "seconds", "export_s", _format_tick),
        (20, 374, "Generation peak memory", "GiB RSS", "peak_rss_gib", _format_tick),
        (
            600,
            374,
            "Package-text deflate proxy",
            "MiB · NOT WHEEL SIZE",
            "deflate_proxy_mib",
            _format_tick,
        ),
    )
    for x, y, panel_title, unit, key, formatter in panels:
        parts.extend(
            _horizontal_bar_chart(
                rows,
                x=x,
                y=y,
                width=540,
                height=320,
                title=panel_title,
                unit=unit,
                key=key,
                value_formatter=formatter,
            )
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _render_scale_envelope(points: list[dict]) -> str:
    chart_points = [
        {
            "ref": _scale_label(point["registers"]),
            "compile_s": point["compile_s"],
            "export_s": point["export_s"],
            "peak_rss_gib": point["generation_peak_rss_mib"] / 1024,
            "source_gib": point["source_bytes"] / (1024**3),
        }
        for point in points
    ]
    parts = _svg_start(
        920,
        570,
        "Exporter scale envelope through 100k registers and 500k fields",
        "Three line charts compare compile and export time, peak resident memory, and generated source size from one thousand through one hundred thousand and one registers.",
    )
    parts.extend(
        _chart(
            chart_points,
            x=20,
            y=12,
            width=880,
            height=264,
            title="Whole-region generation time",
            unit="seconds",
            series=[("Compile + elaborate", "compile_s", "series-a"), ("Export", "export_s", "series-b")],
        )
    )
    parts.extend(
        _chart(
            chart_points,
            x=20,
            y=288,
            width=430,
            height=264,
            title="Generation peak memory",
            unit="GiB RSS",
            series=[("Peak RSS", "peak_rss_gib", "series-a")],
        )
    )
    parts.extend(
        _chart(
            chart_points,
            x=470,
            y=288,
            width=430,
            height=264,
            title="Generated source footprint",
            unit="GiB",
            series=[("Sources", "source_gib", "series-a")],
        )
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _render_sparse_comparison(dense_points: list[dict], sparse_points: list[dict]) -> str:
    dense_by_registers = {point["registers"]: point for point in dense_points}
    sparse_by_registers = {point["registers"]: point for point in sparse_points}
    register_counts = sorted(dense_by_registers.keys() & sparse_by_registers.keys())
    chart_points = []
    for registers in register_counts:
        dense = dense_by_registers[registers]
        sparse = sparse_by_registers[registers]
        chart_points.append(
            {
                "ref": _scale_label(registers),
                "dense_export_s": dense["export_s"],
                "sparse_export_s": sparse["export_s"],
                "dense_peak_rss_gib": dense["generation_peak_rss_mib"] / 1024,
                "sparse_peak_rss_gib": sparse["generation_peak_rss_mib"] / 1024,
                "dense_source_gib": dense["source_bytes"] / (1024**3),
                "sparse_source_gib": sparse["source_bytes"] / (1024**3),
            }
        )

    parts = _svg_start(
        920,
        570,
        "Contiguous versus sparse address scaling through a 2 TiB span",
        "Three line charts compare export time, peak resident memory, and generated source size "
        "for contiguous layouts and layouts spread from zero through two tebibytes.",
    )
    parts.extend(
        _chart(
            chart_points,
            x=20,
            y=12,
            width=880,
            height=264,
            title="Source export time",
            unit="seconds",
            series=[
                ("Contiguous", "dense_export_s", "series-a"),
                ("Sparse through 2 TiB", "sparse_export_s", "series-b"),
            ],
        )
    )
    parts.extend(
        _chart(
            chart_points,
            x=20,
            y=288,
            width=430,
            height=264,
            title="Generation peak memory",
            unit="GiB RSS",
            series=[
                ("Contiguous", "dense_peak_rss_gib", "series-a"),
                ("Sparse through 2 TiB", "sparse_peak_rss_gib", "series-b"),
            ],
        )
    )
    parts.extend(
        _chart(
            chart_points,
            x=470,
            y=288,
            width=430,
            height=264,
            title="Generated source footprint",
            unit="GiB",
            series=[
                ("Contiguous", "dense_source_gib", "series-a"),
                ("Sparse through 2 TiB", "sparse_source_gib", "series-b"),
            ],
        )
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--scale-input", type=Path, default=DEFAULT_SCALE_INPUT)
    parser.add_argument("--sparse-input", type=Path, default=DEFAULT_SPARSE_INPUT)
    parser.add_argument("--field-profile-input", type=Path, default=DEFAULT_FIELD_PROFILE_INPUT)
    parser.add_argument("--output-profile-input", type=Path, default=DEFAULT_OUTPUT_PROFILE_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    payload = json.loads(args.input.read_text())
    releases = payload["releases"]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "release-pipeline.svg").write_text(_render_pipeline(releases))
    (args.output_dir / "release-runtime-size.svg").write_text(_render_runtime_and_size(releases))
    scale_payload = json.loads(args.scale_input.read_text())
    (args.output_dir / "scale-envelope.svg").write_text(_render_scale_envelope(scale_payload["points"]))
    sparse_payload = json.loads(args.sparse_input.read_text())
    (args.output_dir / "sparse-address-comparison.svg").write_text(
        _render_sparse_comparison(scale_payload["points"], sparse_payload["points"])
    )
    if args.field_profile_input.exists():
        field_profile_payload = json.loads(args.field_profile_input.read_text())
        (args.output_dir / "field-profile-comparison.svg").write_text(
            _render_field_profile_comparison(field_profile_payload)
        )
    elif args.field_profile_input != DEFAULT_FIELD_PROFILE_INPUT:
        parser.error(f"field-profile input does not exist: {args.field_profile_input}")
    if args.output_profile_input.exists():
        output_profile_payload = json.loads(args.output_profile_input.read_text())
        (args.output_dir / "output-profile-comparison.svg").write_text(
            _render_output_profile_comparison(output_profile_payload)
        )
    elif args.output_profile_input != DEFAULT_OUTPUT_PROFILE_INPUT:
        parser.error(f"output-profile input does not exist: {args.output_profile_input}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
