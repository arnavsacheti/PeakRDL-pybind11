"""Uniform ``.info`` metadata namespace.

Every node kind (Reg, Field, RegFile, AddrMap, Mem, Alias) exposes an
immutable :class:`Info` instance via ``node.info``. Sibling units
(interrupts, side effects, observers) consume ``node.info.<attr>`` rather
than parsing RDL themselves.

The :class:`Info` class is the canonical metadata API:

    >>> i = Info(name="control", address=0x4000_1000, offset=0x000, path="uart.control")
    >>> i.name
    'control'
    >>> i.address
    1073745920

For generated bindings the helper :func:`from_rdl_node` extracts metadata
from a ``systemrdl.node.Node`` if available; otherwise it gracefully
degrades to a defaulted :class:`Info` (useful for stubs and tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace


class TagsNamespace(SimpleNamespace):
    """Permissive attribute namespace for user-defined properties (UDPs).

    Behaves like :class:`types.SimpleNamespace` for set attributes, but
    returns ``None`` for unset attribute access instead of raising
    :class:`AttributeError`. This matches the spec: ``info.tags`` should
    let consumers probe for arbitrary UDP names without try/except.
    """

    def __getattr__(self, name: str) -> object | None:
        # Dunder lookups must keep raising so copy/pickle/etc. work normally.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return None


_ACCESS_VALUES = frozenset({"rw", "r", "w", "na"})
_PRECEDENCE_VALUES = frozenset({"sw", "hw"})
_ON_READ_VALUES = frozenset({"rclr", "rset", "ruser"})
_ON_WRITE_VALUES = frozenset({"woclr", "woset", "wzc", "wzs", "wclr", "wset", "wuser"})
_ALIAS_KIND_VALUES = frozenset({"full", "sw_view", "hw_view", "scrambled"})

# Aliases for systemrdl AccessType names that don't already match our token set.
_ACCESS_ALIASES: dict[str, str] = {
    "ro": "r",
    "wo": "w",
    "rw1": "rw",
    "w1": "w",
    "rclr": "r",
    "rset": "r",
}


@dataclass(frozen=True, slots=True)
class Info:
    """Immutable metadata snapshot exposed via ``node.info``.

    All fields have safe defaults so :class:`Info` can be instantiated
    bare (``Info()``) for stubs / fallbacks.

    Common fields are populated for every node kind. Field-only
    attributes default to ``None``/``False`` on non-field nodes, which
    keeps consumer code uniform (``if node.info.is_volatile: ...``
    works even for registers).
    """

    # Common to every node kind.
    name: str = ""
    desc: str | None = None
    address: int = 0
    offset: int = 0
    regwidth: int | None = None
    access: str | None = None  # "rw" | "r" | "w" | "na"
    reset: int | None = None
    fields: dict[str, Info] = field(default_factory=dict)
    path: str = ""
    rdl_node: object | None = None
    source: tuple[str, int] | None = None
    tags: SimpleNamespace = field(default_factory=TagsNamespace)

    # Field-only attributes. Always present; safely null on non-fields.
    precedence: str | None = None  # "sw" | "hw"
    paritycheck: bool = False
    is_volatile: bool = False  # hwclr/hwset/sticky/counter
    is_interrupt_source: bool = False  # has the `intr` UDP
    on_read: str | None = None  # "rclr" | "rset" | "ruser" | None
    on_write: str | None = None  # "woclr" | "woset" | "wzc" | "wzs" | "wclr" | "wset" | "wuser" | None
    alias_kind: str | None = None  # "full" | "sw_view" | "hw_view" | "scrambled" | None

    def __repr__(self) -> str:  # pragma: no cover - trivial formatting
        addr = f"0x{self.address:x}" if self.address else "0x0"
        path = self.path or self.name or "<anon>"
        access = self.access or "?"
        bits: list[str] = [f"path={path!r}", f"@{addr}", f"access={access}"]
        if self.regwidth is not None:
            bits.append(f"regwidth={self.regwidth}")
        if self.reset is not None:
            bits.append(f"reset=0x{self.reset:x}")
        return "Info(" + ", ".join(bits) + ")"


# ---------------------------------------------------------------------------
# Extraction from systemrdl nodes
# ---------------------------------------------------------------------------


def _safe_get_property(node: object, prop: str, default: object = None) -> object:
    """Return ``node.get_property(prop)`` or ``default`` on failure.

    SystemRDL nodes raise :class:`LookupError` (or similar) for properties
    not declared on a particular component. We don't want metadata
    extraction to throw for any of them.
    """
    getter = getattr(node, "get_property", None)
    if getter is None:
        return default
    try:
        return getter(prop, default=default)
    except TypeError:
        # Some property accessors don't accept a default kwarg.
        try:
            return getter(prop)
        except Exception:
            return default
    except Exception:
        return default


def _coerce_str(value: object) -> str | None:
    """Coerce an RDL property to a lowercase token, or ``None``."""
    if value is None:
        return None
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.lower()
    if isinstance(value, str):
        return value.lower()
    return str(value).lower()


def _extract_access(node: object) -> str | None:
    """Map RDL ``sw`` access mode onto our token set."""
    token = _coerce_str(_safe_get_property(node, "sw"))
    if token is None:
        return None
    if token in _ACCESS_VALUES:
        return token
    return _ACCESS_ALIASES.get(token)


def _extract_precedence(node: object) -> str | None:
    val = _safe_get_property(node, "precedence")
    token = _coerce_str(val)
    return token if token in _PRECEDENCE_VALUES else None


def _extract_on_read(node: object) -> str | None:
    val = _safe_get_property(node, "onread")
    token = _coerce_str(val)
    return token if token in _ON_READ_VALUES else None


def _extract_on_write(node: object) -> str | None:
    val = _safe_get_property(node, "onwrite")
    token = _coerce_str(val)
    return token if token in _ON_WRITE_VALUES else None


def _extract_is_volatile(node: object) -> bool:
    """Field is volatile if any of hwclr/hwset/sticky/stickybit/counter is set."""
    for prop in ("hwclr", "hwset", "sticky", "stickybit", "counter"):
        if bool(_safe_get_property(node, prop, False)):
            return True
    return False


def _extract_is_interrupt(node: object) -> bool:
    return bool(_safe_get_property(node, "intr", False))


def _extract_paritycheck(node: object) -> bool:
    return bool(_safe_get_property(node, "paritycheck", False))


def _extract_alias_kind(node: object) -> str | None:
    """Best-effort detection of alias kind from an RDL node."""
    # SystemRDL exposes alias info via ``is_alias``/``alias_primary_inst`` on
    # AddressableNode; the kind itself is encoded in UDPs. We default to
    # "full" when an alias relationship exists but no explicit kind UDP.
    if not bool(getattr(node, "is_alias", False)):
        return None
    kind = _safe_get_property(node, "alias_kind")
    token = _coerce_str(kind)
    if token in _ALIAS_KIND_VALUES:
        return token
    return "full"


def _extract_address(node: object) -> int:
    for attr in ("absolute_address", "address"):
        v = getattr(node, attr, None)
        if isinstance(v, int):
            return v
    return 0


def _extract_offset(node: object) -> int:
    for attr in ("address_offset", "offset", "low"):
        v = getattr(node, attr, None)
        if isinstance(v, int):
            return v
    return 0


def _extract_regwidth(node: object) -> int | None:
    val = _safe_get_property(node, "regwidth")
    if isinstance(val, int):
        return val
    # FieldNode-style: width attr
    width = getattr(node, "width", None)
    return width if isinstance(width, int) else None


def _extract_reset(node: object) -> int | None:
    val = _safe_get_property(node, "reset")
    if isinstance(val, int):
        return val
    return None


def _extract_source(node: object) -> tuple[str, int] | None:
    """Extract ``(file, line)`` from a node's RDL source reference."""
    ref = getattr(node, "inst_src_ref", None) or getattr(node, "def_src_ref", None)
    if ref is None:
        return None
    path = getattr(ref, "filename", None) or getattr(ref, "path", None)
    line = getattr(ref, "line", None)
    if isinstance(path, str) and isinstance(line, int):
        return (path, line)
    return None


def _extract_path(node: object) -> str:
    fn = getattr(node, "get_path", None)
    if callable(fn):
        try:
            result = fn()
            if isinstance(result, str):
                return result
        except Exception:
            pass
    name = getattr(node, "inst_name", None)
    return name if isinstance(name, str) else ""


def _extract_name(node: object) -> str:
    name_prop = _safe_get_property(node, "name")
    if isinstance(name_prop, str) and name_prop:
        return name_prop
    inst = getattr(node, "inst_name", None)
    return inst if isinstance(inst, str) else ""


def _extract_desc(node: object) -> str | None:
    val = _safe_get_property(node, "desc")
    if isinstance(val, str):
        return val
    return None


def _extract_tags(node: object) -> SimpleNamespace:
    """Pull user-defined properties (UDPs) into a permissive namespace."""
    ns = TagsNamespace()
    list_props = getattr(node, "list_properties", None)
    if not callable(list_props):
        return ns
    try:
        names = list(list_props(include_native=False, include_udp=True))
    except TypeError:
        try:
            names = list(list_props())
        except Exception:
            names = []
    except Exception:
        names = []
    for prop in names:
        if not isinstance(prop, str) or not prop.isidentifier():
            continue
        value = _safe_get_property(node, prop)
        if value is not None:
            setattr(ns, prop, value)
    return ns


def _extract_fields(node: object) -> dict[str, Info]:
    """Build a mapping of child field name -> ``Info`` for register nodes."""
    children: dict[str, Info] = {}
    fn = getattr(node, "fields", None)
    if not callable(fn):
        return children
    try:
        for child in fn():
            child_name = getattr(child, "inst_name", None)
            if isinstance(child_name, str) and child_name:
                children[child_name] = from_rdl_node(child)
    except Exception:
        return {}
    return children


def from_rdl_node(rdl_node: object | None) -> Info:
    """Build an :class:`Info` from a ``systemrdl.node.Node`` (or ``None``).

    When ``rdl_node`` is ``None`` (or a stub object missing the usual
    accessors), a fully-defaulted :class:`Info` is returned. This is the
    "graceful degrade" path used by stubs and tests.
    """
    if rdl_node is None:
        return Info()

    return Info(
        name=_extract_name(rdl_node),
        desc=_extract_desc(rdl_node),
        address=_extract_address(rdl_node),
        offset=_extract_offset(rdl_node),
        regwidth=_extract_regwidth(rdl_node),
        access=_extract_access(rdl_node),
        reset=_extract_reset(rdl_node),
        fields=_extract_fields(rdl_node),
        path=_extract_path(rdl_node),
        rdl_node=rdl_node,
        source=_extract_source(rdl_node),
        tags=_extract_tags(rdl_node),
        precedence=_extract_precedence(rdl_node),
        paritycheck=_extract_paritycheck(rdl_node),
        is_volatile=_extract_is_volatile(rdl_node),
        is_interrupt_source=_extract_is_interrupt(rdl_node),
        on_read=_extract_on_read(rdl_node),
        on_write=_extract_on_write(rdl_node),
        alias_kind=_extract_alias_kind(rdl_node),
    )


# ---------------------------------------------------------------------------
# Node-class wiring
# ---------------------------------------------------------------------------


def attach_info(node_class: type, info: Info) -> None:
    """Attach an :class:`Info` snapshot to a generated node class.

    Generated bindings call this once per class at import time so users
    can simply do ``soc.uart.control.info.address``. The attribute is
    set on the class (not the instance), making it shared metadata.
    """
    node_class.info = info  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Optional registration with the runtime registry (Unit 1's seam).
# When Unit 1 is not yet merged the import fails and we silently skip;
# this keeps the file importable in isolation.
# ---------------------------------------------------------------------------

try:
    from peakrdl_pybind11.runtime._registry import register_node_attribute  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - sibling unit may not exist yet
    pass
else:

    @register_node_attribute("info")
    def _info_factory(node_class: type, metadata: dict[str, object]) -> Info:  # pragma: no cover
        """Registry hook: build an :class:`Info` from collected metadata."""
        return from_rdl_node(metadata.get("rdl_node"))
