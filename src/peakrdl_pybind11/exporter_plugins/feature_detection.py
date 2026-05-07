"""Feature-detection exporter plugin.

Bundles four post-export passes that consume the elaborated RDL tree and
write supplementary artifacts next to the generated module:

* **Interrupt detection** -> ``interrupts_detected.py`` -- pairs a state
  register (``INTR_STATE`` / ``intr_status`` / ``*_INT_STATUS``) with its
  ``_ENABLE`` / ``_MASK`` / ``_TEST`` / ``_RAW`` siblings and matches
  fields by name across the trio. (Sketch §9.4)

* **Alias detection** -> ``aliases.py`` -- mapping of every aliased
  register's path to its primary's path. (Sketch §10)

* **schema.json emission** -> a JSON serialisation of the full node tree,
  the canonical artifact other tooling (web register browser, search,
  GUIs) consumes without reparsing RDL. (Sketch §20)

* **Stubs enrichment** -> rewrites the generated ``__init__.pyi`` to add
  ``Unpack[TypedDict]`` overloads for ``write_fields(**fields)`` per
  register, ``Literal[...]`` for enum-encoded fields, and
  ``Annotated[int, Range(0, max)]`` for bounded fields. The Jinja
  template is left untouched -- this is a string post-process so the
  template stays small. (Sketch §17)

The plugin is configured via attributes on the :class:`Pybind11Exporter`
instance (``interrupt_pattern``); the CLI in ``__peakrdl__.py`` plumbs
the ``--interrupt-pattern`` flag through.
"""

from __future__ import annotations

import json
import logging
import pprint
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from systemrdl.node import (
    AddrmapNode,
    FieldNode,
    MemNode,
    Node,
    RegfileNode,
    RegNode,
)

if TYPE_CHECKING:
    from peakrdl_pybind11.exporter_plugins import PluginContext

LOGGER = logging.getLogger(__name__)

# Default state-register matcher: matches names like ``INTR_STATE``,
# ``intr_status``, ``*_INT_STATUS``. Anchored to the full inst_name; case
# is normalised to upper before the check to keep the mental model simple.
DEFAULT_STATE_PATTERN = re.compile(r"(?:INTR[_]?STAT(?:E|US)|.+_INT_STATUS)\Z")

# Suffixes we look for to identify the partner registers in a trio. The
# order matters: a state register named ``INTR_STATE`` -> partners are
# ``INTR_ENABLE`` / ``INTR_MASK`` / ``INTR_TEST`` / ``INTR_RAW``. We match
# them with the same case-insensitive comparison the state regex uses.
PARTNER_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("enable_reg", "ENABLE"),
    ("mask_reg", "MASK"),
    ("test_reg", "TEST"),
    ("raw_reg", "RAW"),
)


@dataclass(frozen=True)
class InterruptGroup:
    """Detected interrupt trio. Serialised into ``interrupts_detected.py``."""

    path: str
    state_reg: str
    enable_reg: str | None
    test_reg: str | None
    mask_reg: str | None = None
    raw_reg: str | None = None
    sources: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "state_reg": self.state_reg,
            "enable_reg": self.enable_reg,
            "test_reg": self.test_reg,
            "mask_reg": self.mask_reg,
            "raw_reg": self.raw_reg,
            "sources": list(self.sources),
        }


# ---------------------------------------------------------------------------
# Interrupt detection
# ---------------------------------------------------------------------------


def _normalise_pattern(
    pattern: object | None,
) -> Callable[[str], bool]:
    """Coerce the user-supplied ``--interrupt-pattern`` into a predicate."""
    if pattern is None:
        return _default_state_predicate
    if callable(pattern) and not isinstance(pattern, re.Pattern):
        return pattern  # type: ignore[return-value]
    if isinstance(pattern, re.Pattern):
        return lambda name, _p=pattern: bool(_p.fullmatch(name))
    if isinstance(pattern, str):
        compiled = re.compile(pattern)
        return lambda name, _p=compiled: bool(_p.fullmatch(name))
    raise TypeError(f"interrupt_pattern must be a regex/str/callable, got {type(pattern).__name__}")


def _default_state_predicate(name: str) -> bool:
    upper = name.upper()
    return bool(DEFAULT_STATE_PATTERN.fullmatch(upper))


def _state_stem(name: str) -> str | None:
    """Strip a trailing ``STATE`` / ``STATUS`` token. Returns the stem."""
    upper = name.upper()
    for tail in ("_STATE", "_STATUS", "STATE", "STATUS"):
        if upper.endswith(tail):
            stem = upper[: -len(tail)]
            return stem.rstrip("_")
    return None


def _all_fields_intr(reg: RegNode) -> bool:
    """A register qualifies for trio search only if every field carries
    the ``intr`` builtin property. Intentionally strict: a status reg with
    a mix of intr and non-intr fields probably isn't an interrupt status
    in the §9.4 sense."""
    fields = list(reg.fields())
    if not fields:
        return False
    for f in fields:
        try:
            if not f.get_property("intr"):
                return False
        except LookupError:
            return False
    return True


def _siblings(state: RegNode) -> dict[str, RegNode]:
    """Return ``{upper_inst_name: RegNode}`` for every sibling of ``state``
    that lives in the same parent."""
    parent = state.parent
    out: dict[str, RegNode] = {}
    if parent is None:
        return out
    for child in parent.children():
        if isinstance(child, RegNode) and child is not state:
            out[child.inst_name.upper()] = child
    return out


def _match_partner(
    siblings: Mapping[str, RegNode],
    state_name: str,
    suffix: str,
) -> RegNode | None:
    """Look for ``<stem>_<SUFFIX>`` or ``<stem><SUFFIX>`` in ``siblings``."""
    stem = _state_stem(state_name)
    if stem is None:
        # No recognisable suffix on the state name; fall back to swapping
        # the trailing token wholesale (e.g. ``INTR_STATE`` -> ``INTR_<S>``).
        upper = state_name.upper()
        for tail in ("_STATE", "_STATUS"):
            if upper.endswith(tail):
                candidate = upper[: -len(tail)] + "_" + suffix
                if candidate in siblings:
                    return siblings[candidate]
        return None
    candidates = (
        f"{stem}_{suffix}" if stem else suffix,
        f"{stem}{suffix}",
    )
    for cand in candidates:
        if cand in siblings:
            return siblings[cand]
    return None


def _shared_field_names(*regs: RegNode | None) -> list[str]:
    """Field names present (case-sensitive) on every supplied register.

    Iteration order follows the state register so the JSON output is
    stable.
    """
    state, *partners = [r for r in regs if r is not None]
    state_fields = [f.inst_name for f in state.fields()]
    common: set[str] = set(state_fields)
    for partner in partners:
        common &= {f.inst_name for f in partner.fields()}
    return [name for name in state_fields if name in common]


def detect_interrupt_groups(
    top_node: AddrmapNode,
    pattern: object | None = None,
) -> list[InterruptGroup]:
    """Walk ``top_node`` and return every detected interrupt trio."""
    predicate = _normalise_pattern(pattern)
    groups: list[InterruptGroup] = []
    for node in top_node.descendants():
        if not isinstance(node, RegNode):
            continue
        if not predicate(node.inst_name):
            continue
        if not _all_fields_intr(node):
            continue

        siblings = _siblings(node)
        partners: dict[str, RegNode | None] = {}
        for attr, suffix in PARTNER_SUFFIXES:
            partners[attr] = _match_partner(siblings, node.inst_name, suffix)

        sources = _shared_field_names(
            node,
            partners.get("enable_reg"),
            partners.get("test_reg"),
        )
        if not sources:
            # No fields shared with any partner -- treat as the state
            # register on its own, with field names from the state.
            sources = [f.inst_name for f in node.fields()]

        groups.append(
            InterruptGroup(
                path=node.parent.get_path() if node.parent is not None else node.get_path(),
                state_reg=node.get_path(),
                enable_reg=_path_or_none(partners.get("enable_reg")),
                test_reg=_path_or_none(partners.get("test_reg")),
                mask_reg=_path_or_none(partners.get("mask_reg")),
                raw_reg=_path_or_none(partners.get("raw_reg")),
                sources=tuple(sources),
            )
        )
    return groups


def _path_or_none(reg: RegNode | None) -> str | None:
    return reg.get_path() if reg is not None else None


# ---------------------------------------------------------------------------
# Alias detection
# ---------------------------------------------------------------------------


def detect_aliases(top_node: AddrmapNode) -> dict[str, str]:
    """Return ``{alias_path: primary_path}`` for every aliased register."""
    aliases: dict[str, str] = {}
    for node in top_node.descendants():
        if not isinstance(node, RegNode):
            continue
        if not getattr(node, "is_alias", False):
            continue
        try:
            primary = node.alias_primary
        except ValueError:
            continue
        if primary is None:
            continue
        aliases[node.get_path()] = primary.get_path()
    return aliases


# ---------------------------------------------------------------------------
# schema.json
# ---------------------------------------------------------------------------

# RDL property names whose values we serialise. Limited to JSON-safe
# scalars; ``encode``/``onread`` etc. carry enum or class objects that
# don't round-trip through ``json.dumps``.
_REG_PROPS: tuple[str, ...] = (
    "name",
    "desc",
    "regwidth",
    "accesswidth",
    "addressing",
    "alignment",
)

_FIELD_PROPS: tuple[str, ...] = (
    "name",
    "desc",
    "intr",
    "singlepulse",
    "sticky",
    "stickybit",
    "paritycheck",
    "hwclr",
    "hwset",
    "anded",
    "ored",
    "xored",
)

_MEM_PROPS: tuple[str, ...] = (
    "name",
    "desc",
    "mementries",
    "memwidth",
)


def _safe_property(node: Node, name: str) -> Any | None:  # noqa: ANN401 - RDL props are heterogeneous
    try:
        value = node.get_property(name)
    except LookupError:
        return None
    return _to_jsonable(value)


def _to_jsonable(value: Any) -> Any:  # noqa: ANN401 - polymorphic by design
    """Coerce a value into something ``json.dumps`` can handle.

    Falls back to ``str()`` for anything exotic (RDL enum members, class
    references, etc.).
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    name = getattr(value, "__name__", None)
    if name is not None:
        return name
    return str(value)


def _udp_dict(node: Node) -> dict[str, Any]:
    """Dump every UDP attached to ``node`` as JSON-friendly values."""
    udps: dict[str, Any] = {}
    # The systemrdl public API exposes user-defined properties via the
    # node's ``list_properties(only_udp=True)``; fall back gracefully for
    # older versions.
    list_props = getattr(node, "list_properties", None)
    if not callable(list_props):
        return udps
    try:
        names = list_props(only_udp=True)
    except TypeError:  # pragma: no cover - older systemrdl versions
        names = ()
    for name in names:
        udps[name] = _safe_property(node, name)
    return udps


def _field_to_dict(field_node: FieldNode) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "field",
        "inst_name": field_node.inst_name,
        "path": field_node.get_path(),
        "lsb": field_node.lsb,
        "msb": field_node.msb,
        "low": field_node.low,
        "high": field_node.high,
        "width": field_node.width,
        "is_sw_readable": field_node.is_sw_readable,
        "is_sw_writable": field_node.is_sw_writable,
        "is_hw_readable": field_node.is_hw_readable,
        "is_hw_writable": field_node.is_hw_writable,
        "reset": _safe_property(field_node, "reset"),
    }
    for prop in _FIELD_PROPS:
        out[prop] = _safe_property(field_node, prop)
    udps = _udp_dict(field_node)
    if udps:
        out["udps"] = udps
    return out


def _reg_to_dict(reg: RegNode) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "reg",
        "inst_name": reg.inst_name,
        "path": reg.get_path(),
        "absolute_address": reg.absolute_address,
        "size": reg.size,
        "is_alias": getattr(reg, "is_alias", False),
        "fields": [_field_to_dict(f) for f in reg.fields()],
    }
    if out["is_alias"]:
        try:
            primary = reg.alias_primary
        except ValueError:
            primary = None
        out["alias_primary"] = primary.get_path() if primary is not None else None
    for prop in _REG_PROPS:
        out[prop] = _safe_property(reg, prop)
    udps = _udp_dict(reg)
    if udps:
        out["udps"] = udps
    return out


def _mem_to_dict(mem: MemNode) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "mem",
        "inst_name": mem.inst_name,
        "path": mem.get_path(),
        "absolute_address": mem.absolute_address,
        "size": mem.size,
        "children": [_node_to_dict(c) for c in mem.children()],
    }
    for prop in _MEM_PROPS:
        out[prop] = _safe_property(mem, prop)
    return out


def _container_to_dict(node: AddrmapNode | RegfileNode) -> dict[str, Any]:
    return {
        "kind": "addrmap" if isinstance(node, AddrmapNode) else "regfile",
        "inst_name": node.inst_name,
        "path": node.get_path(),
        "absolute_address": node.absolute_address,
        "size": node.size,
        "name": _safe_property(node, "name"),
        "desc": _safe_property(node, "desc"),
        "children": [_node_to_dict(c) for c in node.children()],
    }


def _node_to_dict(node: Node) -> dict[str, Any]:
    if isinstance(node, FieldNode):
        return _field_to_dict(node)
    if isinstance(node, RegNode):
        return _reg_to_dict(node)
    if isinstance(node, MemNode):
        return _mem_to_dict(node)
    if isinstance(node, (AddrmapNode, RegfileNode)):
        return _container_to_dict(node)
    return {
        "kind": type(node).__name__,
        "inst_name": getattr(node, "inst_name", None),
        "path": node.get_path() if hasattr(node, "get_path") else None,
    }


def build_schema(top_node: AddrmapNode) -> dict[str, Any]:
    """Return the JSON-serialisable schema for ``top_node``."""
    return {
        "version": 1,
        "soc": _container_to_dict(top_node),
    }


# ---------------------------------------------------------------------------
# Stubs enrichment
# ---------------------------------------------------------------------------

# Marker we splice into the generated stubs file so a second run replaces
# our additions instead of stacking duplicates.
_STUBS_BEGIN = "# --- BEGIN feature_detection ----------------------------------------"
_STUBS_END = "# --- END   feature_detection ----------------------------------------"


def _typed_dict_block_for_reg(reg: RegNode, pybind_name: str) -> str:
    """Generate the ``TypedDict`` + ``write_fields`` overload for one reg."""
    fields = list(reg.fields())
    td_name = f"_{pybind_name}_Fields"
    lines: list[str] = [f"class {td_name}(TypedDict, total=False):"]
    if not fields:
        lines.append("    pass")
    for fld in fields:
        lines.append(f"    {fld.inst_name}: {_field_annotation(fld)}")
    lines.append("")
    return "\n".join(lines)


def _field_annotation(fld: FieldNode) -> str:
    """Pick the most specific type annotation for ``fld``.

    Ordering: ``Literal[...]`` for enum-encoded fields; ``Annotated[int,
    Range(0, max)]`` for plain bounded fields.
    """
    try:
        encode = fld.get_property("encode")
    except LookupError:
        encode = None
    if encode is not None:
        members = getattr(encode, "__members__", None)
        if members:
            literal_values = ", ".join(str(int(v)) for v in members.values())
            if literal_values:
                return f"Literal[{literal_values}]"

    width = fld.width
    if width <= 0:
        return "int"
    max_value = (1 << width) - 1
    return f"Annotated[int, Range(0, {max_value})]"


def enrich_stubs(stubs_path: Path, regs: Iterable[RegNode], pybind_name_for: Callable[[Node], str]) -> None:
    """Rewrite ``__init__.pyi`` to add typed ``write_fields`` overloads.

    Idempotent: a previous block delimited by ``_STUBS_BEGIN`` /
    ``_STUBS_END`` is removed before the new one is appended.
    """
    if not stubs_path.exists():
        return

    text = stubs_path.read_text(encoding="utf-8")
    text = _strip_existing_block(text)

    # Build the block: imports + per-register TypedDicts + injected
    # overloads. We add the overloads as standalone classes that subclass
    # the existing register class -- a "view" type alias would be cleaner,
    # but mypy/pyright accept the subclass form for autocomplete on
    # ``reg.write_fields(...)``.
    typed_dicts: list[str] = []
    overloads: list[str] = []
    reg_list = list(regs)
    if not reg_list:
        return

    for reg in reg_list:
        py_name = pybind_name_for(reg)
        typed_dicts.append(_typed_dict_block_for_reg(reg, py_name))

    # The runtime ``write_fields`` exists on RegisterBase already; we
    # republish a typed signature per register class so IDEs surface
    # field-name autocomplete.
    for reg in reg_list:
        py_name = pybind_name_for(reg)
        td_name = f"_{py_name}_Fields"
        overloads.append(
            "\n".join(
                [
                    f"class {py_name}_typed({py_name}_t):",
                    f"    def write_fields(self, **fields: Unpack[{td_name}]) -> None: ...",
                    "",
                ]
            )
        )

    block_lines: list[str] = [
        _STUBS_BEGIN,
        "from typing import Annotated, Literal, TypedDict",
        "try:",
        "    from typing import Unpack  # type: ignore[attr-defined]",
        "except ImportError:  # pragma: no cover - Python < 3.11",
        "    from typing_extensions import Unpack",
        "",
        "class Range:",
        '    """Marker used inside ``Annotated[int, Range(low, high)]``."""',
        "    def __init__(self, low: int, high: int) -> None: ...",
        "",
    ]
    block_lines.extend(typed_dicts)
    block_lines.extend(overloads)
    block_lines.append(_STUBS_END)

    new_text = text.rstrip() + "\n\n" + "\n".join(block_lines) + "\n"
    stubs_path.write_text(new_text, encoding="utf-8")


def _strip_existing_block(text: str) -> str:
    if _STUBS_BEGIN not in text or _STUBS_END not in text:
        return text
    pre, _, rest = text.partition(_STUBS_BEGIN)
    _, _, post = rest.partition(_STUBS_END)
    return pre.rstrip() + "\n" + post.lstrip()


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _format_python_literal(value: Any) -> str:  # noqa: ANN401 - emitted JSON-shaped literal
    """Render ``value`` as Python source. ``json.dumps`` would emit
    ``null`` / ``true`` / ``false`` which aren't valid Python."""
    return pprint.pformat(value, indent=2, width=88, sort_dicts=False)


def _write_interrupts(path: Path, groups: Iterable[InterruptGroup]) -> None:
    payload = [g.to_dict() for g in groups]
    text = (
        '"""Auto-generated by peakrdl_pybind11.exporter_plugins.feature_detection."""\n\n'
        "from __future__ import annotations\n\n"
        f"interrupt_groups: list[dict] = {_format_python_literal(payload)}\n"
    )
    path.write_text(text, encoding="utf-8")


def _write_aliases(path: Path, aliases: Mapping[str, str]) -> None:
    text = (
        '"""Auto-generated by peakrdl_pybind11.exporter_plugins.feature_detection."""\n\n'
        "from __future__ import annotations\n\n"
        f"aliases: dict[str, str] = {_format_python_literal(dict(aliases))}\n"
    )
    path.write_text(text, encoding="utf-8")


def _write_schema(path: Path, schema: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(schema, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Plugin entry points
# ---------------------------------------------------------------------------


class FeatureDetectionPlugin:
    """The actual plugin object registered with the exporter."""

    def post_export(self, ctx: PluginContext) -> None:
        top = ctx.top_node
        # The exporter writes the runtime + stubs into both
        # ``output_dir/`` and ``output_dir/<soc_name>/``; we mirror that
        # so consumers (and the E2E ls check) can find these files at
        # the package root.
        targets = _output_targets(ctx.output_dir, ctx.soc_name)

        pattern = ctx.options.get("interrupt_pattern")
        groups = detect_interrupt_groups(top, pattern)
        aliases = detect_aliases(top)
        schema = build_schema(top)

        for target in targets:
            target.mkdir(parents=True, exist_ok=True)
            _write_interrupts(target / "interrupts_detected.py", groups)
            _write_aliases(target / "aliases.py", aliases)
            _write_schema(target / "schema.json", schema)

        # Stubs enrichment: rewrite both copies of ``__init__.pyi``.
        try:
            regs = ctx.nodes["regs"]
        except (KeyError, TypeError):
            regs = []
        if regs:
            for stubs_dir in targets:
                enrich_stubs(
                    stubs_dir / "__init__.pyi",
                    regs,
                    pybind_name_for=ctx.exporter._pybind_name_from_node,  # type: ignore[arg-type]
                )

        LOGGER.info(
            "feature_detection: %d interrupt group(s), %d alias(es), schema written to %s",
            len(groups),
            len(aliases),
            ctx.output_dir,
        )


def _output_targets(output_dir: Path, soc_name: str) -> list[Path]:
    return [output_dir, output_dir / soc_name]


def register(_module: object) -> FeatureDetectionPlugin:
    """Discovery entry point: invoked by ``exporter_plugins.discover_plugins``."""
    return FeatureDetectionPlugin()


__all__ = [
    "FeatureDetectionPlugin",
    "InterruptGroup",
    "build_schema",
    "detect_aliases",
    "detect_interrupt_groups",
    "enrich_stubs",
    "register",
]
