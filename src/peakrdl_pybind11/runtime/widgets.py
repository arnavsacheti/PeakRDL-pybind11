"""
Jupyter & rich-display surface (Unit 15).

Implements §5 of ``docs/IDEAL_API_SKETCH.md``:

* ``_repr_html_`` for every node kind (Reg, Field, Mem, RegFile, AddrMap)
  + ``MemView``, ``SnapshotDiff``, ``InterruptGroup``.
* ``_repr_pretty_`` for IPython terminals (ANSI-coloured, aligned columns).
* ``watch(node, period=0.1)`` -- ipywidgets-backed live monitor.

The module is **pure Python** and has **no required third-party
dependencies**. ``ipywidgets`` is a soft dependency -- importing this
module with no ipywidgets installed succeeds; only calling :func:`watch`
raises :class:`NotSupportedError`.

The renderers are intentionally tolerant of missing metadata: when a
generated node lacks an ``info`` namespace, we fall back to whatever
attributes the C++ ``RegisterBase``/``FieldBase``/``MemoryBase`` classes
already expose. That way this module is useful both before and after
Unit 4 (``info``), Unit 8 (``SnapshotDiff``), Unit 10 (``MemView``) and
Unit 12 (``InterruptGroup``) land.
"""

# Renderers operate on duck-typed nodes from generated SoC modules, user
# subclasses, and test fakes -- no shared base class exists. ``Any`` is
# the only honest input annotation here.
# ruff: noqa: ANN401

from __future__ import annotations

import html as _html
import threading
from collections.abc import Callable, Sequence
from typing import Any

from ._registry import (
    SIDE_EFFECT_BADGES,
    register_master_extension,
    register_register_enhancement,
)

# Soft import of ipywidgets. Only :func:`watch` needs it; the rest of
# the rich-display surface (HTML repr, pretty repr, hex dumps, diff
# tables) is pure Python and ships without third-party deps.
try:  # pragma: no cover - exercised indirectly
    import ipywidgets  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised in tests via patching
    ipywidgets = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Local error type. Unit 19 owns the canonical error hierarchy; until it
# lands we ship a minimal stub here so the public ``raise
# NotSupportedError("...")`` contract from the API sketch is honoured.
# ``except Exception`` (the typical user catch-all) still catches it.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - real Unit 19 error type if available
    from peakrdl_pybind11.errors import NotSupportedError  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - fallback for unit isolation

    class NotSupportedError(RuntimeError):
        """Raised when the surrounding environment cannot satisfy a feature.

        Replaced by the canonical ``peakrdl_pybind11.errors.NotSupportedError``
        once Unit 19 lands.
        """


# ---------------------------------------------------------------------------
# Style tokens. Inline-styled spans keep the rendered HTML self-contained
# (no external CSS), which is what JupyterLab and VS Code's notebook
# viewer expect; both strip <style> blocks aggressively.
# ---------------------------------------------------------------------------
_ACCESS_COLORS: dict[str, str] = {
    "rw": "#1565c0",   # blue
    "ro": "#616161",   # grey
    "wo": "#ef6c00",   # orange
    "na": "#c62828",   # red
    "rwl": "#1565c0",
    "r": "#616161",
    "w": "#ef6c00",
}

_ANSI_ACCESS_COLORS: dict[str, str] = {
    "rw": "\x1b[34m",      # blue
    "ro": "\x1b[37m",      # grey/white
    "wo": "\x1b[33m",      # orange/yellow
    "na": "\x1b[31m",      # red
    "rwl": "\x1b[34m",
    "r": "\x1b[37m",
    "w": "\x1b[33m",
}
_ANSI_RESET = "\x1b[0m"
_ANSI_STRIKE = "\x1b[9m"

_TABLE_STYLE = (
    "border-collapse: collapse; font-family: monospace; "
    "font-size: 0.85em; margin: 0.25em 0;"
)
_CELL_STYLE = "border: 1px solid #d0d0d0; padding: 2px 6px; text-align: left;"
_HEADER_CELL_STYLE = (
    f"{_CELL_STYLE} background: #f5f5f5; font-weight: bold;"
)


def _esc(text: Any) -> str:
    """HTML-escape *text*, coercing to ``str`` first."""
    return _html.escape(str(text), quote=True)


def _access_html(access: str) -> str:
    """Render an access mode as a coloured (and possibly strikethrough) span."""
    mode = (access or "rw").lower()
    color = _ACCESS_COLORS.get(mode, "#1565c0")
    style = f"color: {color};"
    if mode == "na":
        style += " text-decoration: line-through;"
    return f'<span style="{style}">{_esc(mode)}</span>'


def _access_ansi(access: str) -> str:
    """Render an access mode for an ANSI-capable terminal."""
    mode = (access or "rw").lower()
    color = _ANSI_ACCESS_COLORS.get(mode, "")
    if mode == "na":
        return f"{color}{_ANSI_STRIKE}{mode}{_ANSI_RESET}"
    return f"{color}{mode}{_ANSI_RESET}" if color else mode


def _badges_for(field: Any) -> list[str]:
    """Pick the side-effect badge glyphs that apply to *field*.

    The function understands two metadata shapes:

    * ``field.info.on_read``/``on_write`` etc. (target API in §5)
    * raw RDL-style attributes left over from older descriptor surfaces
      (``rclr``, ``singlepulse``, ...).
    """
    badges: list[str] = []
    info = getattr(field, "info", None)

    def _truthy(*paths: tuple[str, ...]) -> bool:
        for path in paths:
            target: Any = info if info is not None else field
            for attr in path:
                target = getattr(target, attr, None)
                if target is None:
                    break
            if target:
                # Consider both bool truthiness *and* enum/string equality
                # to the canonical name. A string-valued attribute like
                # ``on_read = "rclr"`` should match ``rclr`` here.
                value = str(target).lower()
                if value not in {"none", "no", "false", "0", ""}:
                    return True
        return False

    if _truthy(("on_read",), ("rclr",)):
        on_read = getattr(info or field, "on_read", "")
        if "rclr" in str(on_read).lower() or getattr(field, "rclr", False):
            badges.append(SIDE_EFFECT_BADGES["rclr"])

    if _truthy(("singlepulse",)):
        badges.append(SIDE_EFFECT_BADGES["singlepulse"])

    if _truthy(("sticky",), ("stickybit",)):
        badges.append(SIDE_EFFECT_BADGES["sticky"])

    if _truthy(("is_volatile",), ("volatile",)):
        badges.append(SIDE_EFFECT_BADGES["volatile"])

    return badges


def _badge_html(field: Any) -> str:
    """Inline-render the side-effect badges for *field*."""
    glyphs = _badges_for(field)
    if not glyphs:
        return ""
    return ' <span title="side-effects">' + " ".join(_esc(g) for g in glyphs) + "</span>"


# ---------------------------------------------------------------------------
# Field metadata helpers. They look at ``field.info.<attr>`` first, then
# fall back to the raw attribute on the field object. Everything is
# wrapped in try/except so a partly-built mock can still be rendered.
# ---------------------------------------------------------------------------
def _field_attr(field: Any, *names: str, default: Any = None) -> Any:
    info = getattr(field, "info", None)
    for name in names:
        if info is not None:
            value = getattr(info, name, None)
            if value is not None:
                return value
        value = getattr(field, name, None)
        if value is not None:
            return value
    return default


def _has_rclr(field: Any) -> bool:
    """``True`` iff *field* has ``onread = rclr`` -- i.e. reads destroy state."""
    return "rclr" in str(_field_attr(field, "on_read", default="")).lower()


def _field_bits(field: Any) -> str:
    """Render the bit range as ``[msb:lsb]`` or ``[bit]``."""
    lsb = _field_attr(field, "lsb", default=0)
    width = _field_attr(field, "width", default=1)
    msb = _field_attr(field, "msb", default=lsb + width - 1)
    if msb == lsb:
        return f"[{lsb}]"
    return f"[{msb}:{lsb}]"


def _field_access(field: Any) -> str:
    """Approximate the textual access mode of *field*."""
    raw = _field_attr(field, "access")
    if raw is not None:
        return str(raw).lower().replace("accessmode.", "").replace(".", "")
    readable = _field_attr(field, "is_readable", "readable", default=True)
    writable = _field_attr(field, "is_writable", "writable", default=True)
    if readable and writable:
        return "rw"
    if readable:
        return "ro"
    if writable:
        return "wo"
    return "na"


def _field_value_repr(field: Any) -> str:
    """Best-effort value rendering for a field. Reads ONLY when safe.

    Rendering never issues a *destructive* read -- the canonical case is
    ``onread = rclr``. Volatile/sticky/singlepulse fields can be read
    safely; the value may simply be racy.
    """
    if _has_rclr(field):
        return "-"
    reader = getattr(field, "read", None)
    if not callable(reader):
        return "-"
    try:
        value = reader()
    except Exception:
        return "-"
    return _format_value(value)


def _format_value(value: Any) -> str:
    """Render a read-back value in a notebook-friendly form."""
    # Enum members render as ``Cls.MEMBER (n)``.
    name = getattr(value, "name", None)
    if name is not None and hasattr(value, "value"):
        return f"{type(value).__name__}.{name} ({int(value)})"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return f"{value} (0x{int(value):x})" if value > 9 else str(int(value))
    return str(value)


def _node_kind(node: Any) -> str:
    """Return a short kind tag for *node*: 'reg', 'field', 'mem', 'regfile', 'addrmap'."""
    explicit = getattr(node, "_node_kind", None)
    if explicit is not None:
        return str(explicit)
    cls = type(node).__name__.lower()
    if "field" in cls:
        return "field"
    if "memory" in cls or "mem" == cls or cls.endswith("_mem"):
        return "mem"
    if "regfile" in cls:
        return "regfile"
    if "addrmap" in cls:
        return "addrmap"
    if "reg" in cls:
        return "reg"
    return "node"


def _list_fields(reg: Any) -> list[Any]:
    """Best-effort enumeration of the field children of *reg*."""
    info = getattr(reg, "info", None)
    if info is not None:
        fields = getattr(info, "fields", None)
        if fields is not None:
            try:
                return list(fields.values())
            except AttributeError:
                return list(fields)
    explicit = getattr(reg, "fields", None)
    if explicit is None:
        return []
    if callable(explicit):
        try:
            return list(explicit())
        except TypeError:
            return []
    if isinstance(explicit, dict):
        return list(explicit.values())
    try:
        return list(explicit)
    except TypeError:
        return []


def _list_children(node: Any) -> list[Any]:
    """List the direct children of an addrmap/regfile node."""
    children: list[Any] = []
    seen: set[int] = set()
    for attr in dir(node):
        if attr.startswith("_"):
            continue
        try:
            value = getattr(node, attr)
        except Exception:
            continue
        if value is node or callable(value) or id(value) in seen:
            continue
        kind = _node_kind(value)
        if kind in {"reg", "regfile", "mem", "addrmap"}:
            seen.add(id(value))
            children.append(value)
    return children


def _node_address(node: Any) -> int | None:
    """Return the absolute address of *node*, or ``None`` if unknown."""
    info = getattr(node, "info", None)
    if info is not None:
        addr = getattr(info, "address", None)
        if isinstance(addr, int):
            return addr
    for name in ("address", "absolute_address", "offset"):
        value = getattr(node, name, None)
        if isinstance(value, int):
            return value
        if callable(value):
            try:
                value = value()
            except TypeError:
                continue
            if isinstance(value, int):
                return value
    return None


def _node_path(node: Any) -> str:
    """Pretty path/name for *node*."""
    info = getattr(node, "info", None)
    if info is not None:
        path = getattr(info, "path", None)
        if path:
            return str(path)
    for name in ("path", "name", "inst_name"):
        value = getattr(node, name, None)
        if isinstance(value, str):
            return value
    return type(node).__name__


def _node_description(node: Any) -> str:
    info = getattr(node, "info", None)
    if info is not None:
        for attr in ("desc", "description"):
            value = getattr(info, attr, None)
            if value:
                return str(value)
    for attr in ("desc", "description", "__doc__"):
        value = getattr(node, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0]
    return ""


def _node_access(node: Any) -> str:
    info = getattr(node, "info", None)
    if info is not None:
        value = getattr(info, "access", None)
        if value is not None:
            return str(value).lower().replace("accessmode.", "").replace(".", "")
    fields = _list_fields(node)
    if not fields:
        return "rw"
    has_read = any(_field_attr(f, "is_readable", "readable", default=True) for f in fields)
    has_write = any(_field_attr(f, "is_writable", "writable", default=True) for f in fields)
    if has_read and has_write:
        return "rw"
    if has_read:
        return "ro"
    if has_write:
        return "wo"
    return "na"


def _format_address(addr: int | None) -> str:
    return f"0x{addr:08x}" if addr is not None else "?"


# ---------------------------------------------------------------------------
# HTML renderers
# ---------------------------------------------------------------------------
def _render_field_row_html(field: Any) -> str:
    """One ``<tr>`` for a register's HTML table."""
    name = getattr(field, "name", None) or _field_attr(field, "name", "inst_name", default="?")
    bits = _field_bits(field)
    access = _field_access(field)
    on_read = _field_attr(field, "on_read", default="-") or "-"
    on_write = _field_attr(field, "on_write", default="-") or "-"
    desc = _node_description(field) or ""
    value_repr = _field_value_repr(field)
    badges = _badge_html(field)
    cells = [
        f'<td style="{_CELL_STYLE}">{_esc(bits)}</td>',
        f'<td style="{_CELL_STYLE}">{_esc(name)}{badges}</td>',
        f'<td style="{_CELL_STYLE}">{_esc(value_repr)}</td>',
        f'<td style="{_CELL_STYLE}">{_access_html(access)}</td>',
        f'<td style="{_CELL_STYLE}">{_esc(on_read)}</td>',
        f'<td style="{_CELL_STYLE}">{_esc(on_write)}</td>',
        f'<td style="{_CELL_STYLE}">{_esc(desc)}</td>',
    ]
    return "<tr>" + "".join(cells) + "</tr>"


_FIELD_HEADERS: tuple[str, ...] = (
    "Bits",
    "Field",
    "Value (decoded)",
    "Access",
    "On-read",
    "On-write",
    "Description",
)


def _render_register_html(reg: Any) -> str:
    fields = _list_fields(reg)
    addr = _node_address(reg)
    access = _node_access(reg)
    title = (
        f"<b>{_esc(_node_path(reg))}</b> @ {_esc(_format_address(addr))} "
        f"{_access_html(access)}"
    )
    header_cells = "".join(
        f'<th style="{_HEADER_CELL_STYLE}">{_esc(h)}</th>' for h in _FIELD_HEADERS
    )
    rows = [_render_field_row_html(f) for f in fields]
    if not rows:
        rows.append(
            f'<tr><td colspan="{len(_FIELD_HEADERS)}" style="{_CELL_STYLE}">'
            "<em>no fields</em></td></tr>"
        )
    return (
        f"<div>{title}"
        f'<table style="{_TABLE_STYLE}">'
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
    )


def _render_field_html(field: Any) -> str:
    """A field renders as a single-row table identical to its register row."""
    title = (
        f"<b>{_esc(_node_path(field))}</b> "
        f"{_esc(_field_bits(field))} "
        f"{_access_html(_field_access(field))}"
    )
    header_cells = "".join(
        f'<th style="{_HEADER_CELL_STYLE}">{_esc(h)}</th>' for h in _FIELD_HEADERS
    )
    return (
        f"<div>{title}"
        f'<table style="{_TABLE_STYLE}">'
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{_render_field_row_html(field)}</tbody>"
        "</table></div>"
    )


def _render_container_html(node: Any, label: str) -> str:
    """RegFile / AddrMap renderer: list child nodes with their addresses."""
    addr = _node_address(node)
    title = f"<b>{_esc(label)}: {_esc(_node_path(node))}</b> @ {_esc(_format_address(addr))}"
    children = _list_children(node)
    if not children:
        return f'<div>{title}<table style="{_TABLE_STYLE}"></table></div>'

    header_cells = "".join(
        f'<th style="{_HEADER_CELL_STYLE}">{_esc(h)}</th>'
        for h in ("Address", "Kind", "Name", "Access", "Description")
    )
    rows = []
    for child in children:
        rows.append(
            "<tr>"
            f'<td style="{_CELL_STYLE}">{_esc(_format_address(_node_address(child)))}</td>'
            f'<td style="{_CELL_STYLE}">{_esc(_node_kind(child))}</td>'
            f'<td style="{_CELL_STYLE}">{_esc(_node_path(child))}</td>'
            f'<td style="{_CELL_STYLE}">{_access_html(_node_access(child))}</td>'
            f'<td style="{_CELL_STYLE}">{_esc(_node_description(child))}</td>'
            "</tr>"
        )
    return (
        f"<div>{title}"
        f'<table style="{_TABLE_STYLE}">'
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
    )


# ---------------------------------------------------------------------------
# MemView -- hex/ASCII dump
# ---------------------------------------------------------------------------
def _coerce_bytes(view: Any) -> bytes:
    """Try every reasonable way to produce a ``bytes`` payload from *view*."""
    for attr in ("to_bytes", "tobytes", "as_bytes"):
        meth = getattr(view, attr, None)
        if callable(meth):
            try:
                data = meth()
                if isinstance(data, (bytes, bytearray, memoryview)):
                    return bytes(data)
            except Exception:
                pass

    # Fall back to ``__iter__`` of int-like values.
    try:
        return bytes(int(b) & 0xFF for b in view)
    except TypeError:
        pass

    if isinstance(view, (bytes, bytearray, memoryview)):
        return bytes(view)
    raise TypeError("Cannot coerce MemView to bytes for rendering")


def _ascii_for(byte: int) -> str:
    return chr(byte) if 0x20 <= byte < 0x7F else "."


def _render_memview_html(view: Any) -> str:
    """Hex/ASCII dump table for a memory view, 16 bytes per row."""
    base = getattr(view, "base_address", None)
    if base is None:
        base = getattr(view, "address", 0) or 0
    try:
        data = _coerce_bytes(view)
    except TypeError as exc:
        return (
            "<div><b>MemView</b> "
            f"<em>{_esc(exc)}</em></div>"
        )

    title = (
        f"<b>{_esc(_node_path(view))}</b> "
        f"@ {_esc(_format_address(int(base)))} "
        f"({len(data)} bytes)"
    )
    header_cells = "".join(
        f'<th style="{_HEADER_CELL_STYLE}">{_esc(h)}</th>'
        for h in ("Address", "Hex", "ASCII")
    )
    rows: list[str] = []
    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        if len(chunk) < 16:
            hex_part = hex_part.ljust(16 * 3 - 1)
        ascii_part = "".join(_ascii_for(b) for b in chunk)
        addr_label = _format_address(int(base) + offset)
        rows.append(
            "<tr>"
            f'<td style="{_CELL_STYLE}">{_esc(addr_label)}</td>'
            f'<td style="{_CELL_STYLE}; white-space: pre;">{_esc(hex_part)}</td>'
            f'<td style="{_CELL_STYLE}; white-space: pre;">{_esc(ascii_part)}</td>'
            "</tr>"
        )
    if not rows:
        rows.append(
            f'<tr><td colspan="3" style="{_CELL_STYLE}"><em>empty view</em></td></tr>'
        )
    return (
        f"<div>{title}"
        f'<table style="{_TABLE_STYLE}">'
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
    )


# ---------------------------------------------------------------------------
# SnapshotDiff -- side-by-side comparison
# ---------------------------------------------------------------------------
def _diff_entries(diff: Any) -> Sequence[tuple[str, Any, Any]]:
    """Best-effort: yield ``(path, before, after)`` triples for a diff."""
    raw = getattr(diff, "entries", None)
    if raw is None:
        raw = getattr(diff, "changes", None)
    if raw is None:
        raw = getattr(diff, "items", None)
    if callable(raw):
        try:
            raw = raw()
        except TypeError:
            raw = None
    if raw is None:
        return []
    out: list[tuple[str, Any, Any]] = []
    try:
        iterator = iter(raw)
    except TypeError:
        return []
    for entry in iterator:
        if isinstance(entry, tuple) and len(entry) == 3:
            out.append(entry)  # type: ignore[arg-type]
            continue
        path = getattr(entry, "path", None) or getattr(entry, "name", "?")
        before = getattr(entry, "before", None)
        after = getattr(entry, "after", None)
        if before is None and after is None and isinstance(entry, dict):
            path = entry.get("path", path)
            before = entry.get("before")
            after = entry.get("after")
        out.append((str(path), before, after))
    return out


def _render_snapshotdiff_html(diff: Any) -> str:
    title = "<b>SnapshotDiff</b>"
    entries = _diff_entries(diff)
    header_cells = "".join(
        f'<th style="{_HEADER_CELL_STYLE}">{_esc(h)}</th>'
        for h in ("Path", "Before", "After")
    )
    if not entries:
        body = (
            f'<tr><td colspan="3" style="{_CELL_STYLE}">'
            "<em>no differences</em></td></tr>"
        )
    else:
        rows = []
        for path, before, after in entries:
            changed = before != after
            highlight = "background: #fff8c4;" if changed else ""
            rows.append(
                "<tr>"
                f'<td style="{_CELL_STYLE}">{_esc(path)}</td>'
                f'<td style="{_CELL_STYLE} {highlight}">{_esc(_format_value(before))}</td>'
                f'<td style="{_CELL_STYLE} {highlight}">{_esc(_format_value(after))}</td>'
                "</tr>"
            )
        body = "".join(rows)
    return (
        f"<div>{title}"
        f'<table style="{_TABLE_STYLE}">'
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table></div>"
    )


# ---------------------------------------------------------------------------
# InterruptGroup -- matrix view (rows = sources, cols = State/Enable/Test/Pending)
# ---------------------------------------------------------------------------
_IRQ_COLUMNS: tuple[str, ...] = ("State", "Enable", "Test", "Pending")


def _safe_call(fn: Any) -> Any:
    if not callable(fn):
        return fn
    try:
        return fn()
    except Exception:
        return None


def _irq_sources(group: Any) -> list[Any]:
    sources = getattr(group, "sources", None)
    if sources is None:
        sources = getattr(group, "_sources", None)
    if callable(sources):
        try:
            sources = sources()
        except TypeError:
            sources = None
    if sources is None:
        return []
    if isinstance(sources, dict):
        return list(sources.values())
    try:
        return list(sources)
    except TypeError:
        return []


def _irq_cell_value(source: Any, column: str) -> str:
    state_val = _safe_call(getattr(source, "is_pending", None))
    enable_val = _safe_call(getattr(source, "is_enabled", None))
    test_val = getattr(source, "test", None)
    if column == "State":
        return _format_value(bool(state_val)) if state_val is not None else "-"
    if column == "Enable":
        return _format_value(bool(enable_val)) if enable_val is not None else "-"
    if column == "Test":
        if test_val is None:
            return "-"
        return _format_value(_safe_call(test_val) if callable(test_val) else test_val)
    if column == "Pending":
        if state_val is None or enable_val is None:
            return "-"
        return _format_value(bool(state_val) and bool(enable_val))
    return "-"


def _render_interruptgroup_html(group: Any) -> str:
    title = f"<b>{_esc(_node_path(group))}</b> (interrupts)"
    sources = _irq_sources(group)
    header_cells = "".join(
        f'<th style="{_HEADER_CELL_STYLE}">{_esc(h)}</th>'
        for h in ("Source", *_IRQ_COLUMNS)
    )
    if not sources:
        body = (
            f'<tr><td colspan="{len(_IRQ_COLUMNS) + 1}" style="{_CELL_STYLE}">'
            "<em>no interrupt sources detected</em></td></tr>"
        )
    else:
        rows = []
        for source in sources:
            cells = [
                f'<td style="{_CELL_STYLE}">{_esc(_node_path(source))}</td>'
            ]
            pending = (
                _safe_call(getattr(source, "is_pending", None))
                and _safe_call(getattr(source, "is_enabled", None))
            )
            highlight = "background: #ffe8b3;" if pending else ""
            for column in _IRQ_COLUMNS:
                cells.append(
                    f'<td style="{_CELL_STYLE} {highlight}">'
                    f'{_esc(_irq_cell_value(source, column))}</td>'
                )
            rows.append("<tr>" + "".join(cells) + "</tr>")
        body = "".join(rows)
    return (
        f"<div>{title}"
        f'<table style="{_TABLE_STYLE}">'
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table></div>"
    )


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------
_SPECIAL_RENDERERS: dict[str, Callable[[Any], str]] = {
    "memview": _render_memview_html,
    "snapshotdiff": _render_snapshotdiff_html,
    "interruptgroup": _render_interruptgroup_html,
}

_KIND_RENDERERS: dict[str, Callable[[Any], str]] = {
    "field": _render_field_html,
    "reg": _render_register_html,
    "mem": _render_memview_html,
    "regfile": lambda n: _render_container_html(n, "RegFile"),
    "addrmap": lambda n: _render_container_html(n, "AddrMap"),
}


def render_html(node: Any) -> str:
    """Render *node* to an HTML string. Public entry point."""
    explicit = getattr(node, "_widgets_kind", None)
    if explicit in _SPECIAL_RENDERERS:
        return _SPECIAL_RENDERERS[explicit](node)

    cls_name = type(node).__name__.lower()
    for tag, renderer in _SPECIAL_RENDERERS.items():
        if tag in cls_name:
            return renderer(node)

    renderer = _KIND_RENDERERS.get(_node_kind(node))
    if renderer is not None:
        return renderer(node)
    return f"<div><pre>{_esc(repr(node))}</pre></div>"


# ---------------------------------------------------------------------------
# IPython terminal renderer (`_repr_pretty_`)
# ---------------------------------------------------------------------------
def _pretty_field_line(field: Any) -> str:
    bits = _field_bits(field)
    name = getattr(field, "name", None) or _field_attr(field, "name", "inst_name", default="?")
    access = _access_ansi(_field_access(field))
    value = _field_value_repr(field)
    badges = _badges_for(field)
    badge_str = (" " + " ".join(badges)) if badges else ""
    desc = _node_description(field)
    desc_str = f"  -- {desc}" if desc else ""
    return f"  {bits:<8} {name:<14} {access:<3} {value:<28}{badge_str}{desc_str}"


def render_pretty(node: Any) -> str:
    """Plain-text rendering with ANSI colours, suitable for a terminal."""
    kind = _node_kind(node)
    if kind == "reg":
        addr = _format_address(_node_address(node))
        access = _access_ansi(_node_access(node))
        header = f"<Reg {_node_path(node)} @ {addr} {access}>"
        lines = [header]
        for field in _list_fields(node):
            lines.append(_pretty_field_line(field))
        return "\n".join(lines)
    if kind == "field":
        return _pretty_field_line(node).strip()
    if kind in {"regfile", "addrmap"}:
        addr = _format_address(_node_address(node))
        header = f"<{'AddrMap' if kind == 'addrmap' else 'RegFile'} {_node_path(node)} @ {addr}>"
        children = _list_children(node)
        lines = [header]
        for child in children:
            lines.append(
                f"  {_format_address(_node_address(child))}  "
                f"{_node_kind(child):<8} {_node_path(child)}"
            )
        return "\n".join(lines)
    if kind == "mem":
        return f"<Memory {_node_path(node)} @ {_format_address(_node_address(node))}>"
    return repr(node)


def _ipython_pretty(node: Any, p: Any, cycle: bool) -> None:
    """Glue compatible with IPython's ``_repr_pretty_`` protocol."""
    if cycle:
        p.text(repr(node))
        return
    p.text(render_pretty(node))


# ---------------------------------------------------------------------------
# watch() -- live monitor backed by ipywidgets
# ---------------------------------------------------------------------------
class Watcher:
    """Handle returned by :func:`watch`. Call :meth:`stop` to halt polling.

    The watcher owns a daemon thread that ticks every ``period`` seconds
    and re-renders the target node into its widget. The thread terminates
    the next time it finishes a render cycle after :meth:`stop` is
    invoked, or implicitly when the Python interpreter shuts down (it is
    a daemon).
    """

    def __init__(
        self,
        node: Any,
        widget: Any,
        period: float,
        renderer: Callable[[Any], str],
    ) -> None:
        self._node = node
        self._widget = widget
        self._period = float(period)
        self._renderer = renderer
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def widget(self) -> Any:
        """The underlying ipywidgets widget (so the user can ``display(w)``)."""
        return self._widget

    @property
    def period(self) -> float:
        """Polling interval, seconds."""
        return self._period

    @property
    def is_running(self) -> bool:
        """True while the polling thread is active."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> Watcher:
        """Begin polling. Returns *self* for chaining."""
        if self.is_running:
            return self
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                try:
                    rendered = self._renderer(self._node)
                except Exception as exc:
                    rendered = f"<div><pre>watch error: {_esc(exc)}</pre></div>"
                # ipywidgets HTML widget exposes ``value``; assigning it
                # is the documented "update in place" idiom.
                try:
                    self._widget.value = rendered
                except AttributeError:  # widget might not support ``value``
                    pass
                # Wait on the event so .stop() interrupts immediately.
                if self._stop_event.wait(self._period):
                    break

        self._thread = threading.Thread(target=_loop, daemon=True, name="peakrdl-watch")
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the polling thread. Safe to call multiple times."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(self._period * 2.0, 0.5))
        self._thread = None

    def _ipython_display_(self) -> None:
        """Show the live widget when the watcher is evaluated in a notebook."""
        try:  # pragma: no cover - IPython-only path
            from IPython.display import display

            display(self._widget)
        except ImportError:
            pass

    def _repr_html_(self) -> str:
        """Static fallback for environments that don't speak ``_ipython_display_``."""
        return render_html(self._node)


def _is_destructive(node: Any) -> bool:
    """Return ``True`` if polling *node* would silently destroy state.

    A field is destructive iff its ``on_read`` is ``rclr``; a register is
    destructive iff any of its fields is. Snapshots and other composite
    nodes are checked through ``_list_fields`` which already walks
    children for any node that exposes a ``fields`` attribute.
    """
    if _node_kind(node) == "field":
        return _has_rclr(node)
    return any(_has_rclr(f) for f in _list_fields(node))


def watch(
    node: Any,
    period: float = 0.1,
    where: Any | None = None,
    *,
    allow_destructive: bool = False,
    autostart: bool = True,
) -> Watcher:
    """Return a live, refreshing widget for *node*.

    Args:
        node: A register, snapshot, mem view, or any node that
            ``render_html`` can render.
        period: Polling interval in seconds. Defaults to 100 ms.
        where: Optional sub-selector (glob string or callable) applied
            inside snapshot watchers. Forwarded as-is to the renderer
            when supported.
        allow_destructive: If ``True``, silence the side-effect guard
            for ``rclr`` registers/fields. The default is ``False``
            because periodic polling of a read-clear field is a footgun.
        autostart: If ``True`` (default), start the polling thread
            before returning. Set to ``False`` if the caller wants to
            ``.start()`` manually.

    Raises:
        NotSupportedError: If ``ipywidgets`` is not installed, or if
            ``node`` requires destructive reads and ``allow_destructive``
            is ``False``.
    """
    if ipywidgets is None:
        raise NotSupportedError(
            "install peakrdl-pybind11[notebook] for watch() (ipywidgets is required)"
        )

    if not allow_destructive and _is_destructive(node):
        raise NotSupportedError(
            "refusing to watch() a register with rclr fields without "
            "allow_destructive=True (periodic polling would clear hardware state)"
        )

    period = float(period)
    if period <= 0:
        raise ValueError("watch(period=...) must be positive")

    # ``where`` is in the public signature for forward-compatibility with
    # snapshot.watch(...) once Unit 8 lands. Renderers ignore it today.
    widget = ipywidgets.HTML(value=render_html(node))  # type: ignore[union-attr]
    watcher = Watcher(node=node, widget=widget, period=period, renderer=render_html)
    if autostart:
        watcher.start()
    return watcher


# ---------------------------------------------------------------------------
# Wiring -- attach _repr_html_/_repr_pretty_/watch onto generated classes.
# ---------------------------------------------------------------------------
def attach_widgets(cls: type) -> None:
    """Attach the rich-display surface to *cls*.

    Idempotent: a class that already has ``_repr_html_`` set by us is
    not double-decorated. Manually defined ``_repr_html_`` on user
    subclasses is preserved.
    """
    if getattr(cls.__dict__.get("_repr_html_"), "__peakrdl_widget__", False):
        return

    def _repr_html_(self: Any) -> str:
        return render_html(self)

    _repr_html_.__peakrdl_widget__ = True  # type: ignore[attr-defined]
    cls._repr_html_ = _repr_html_  # type: ignore[attr-defined]

    def _repr_pretty_(self: Any, p: Any, cycle: bool) -> None:
        _ipython_pretty(self, p, cycle)

    _repr_pretty_.__peakrdl_widget__ = True  # type: ignore[attr-defined]
    cls._repr_pretty_ = _repr_pretty_  # type: ignore[attr-defined]

    if not hasattr(cls, "watch"):
        def _watch(self: Any, period: float = 0.1, **kwargs: Any) -> Watcher:
            return watch(self, period=period, **kwargs)

        cls.watch = _watch  # type: ignore[attr-defined]


@register_register_enhancement
def _attach_to_class(cls: type) -> None:
    """Hook executed by Unit 1 for every Reg/Field/Mem/RegFile/AddrMap class."""
    attach_widgets(cls)


@register_master_extension
def _attach_to_soc(cls: type) -> None:
    """Hook executed by Unit 1 for every top-level SoC class."""
    attach_widgets(cls)


__all__ = [
    "NotSupportedError",
    "Watcher",
    "attach_widgets",
    "render_html",
    "render_pretty",
    "watch",
]
