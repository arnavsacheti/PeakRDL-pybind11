"""Reflective ``schema.json`` export of the SoC register map.

Implements §17 of ``docs/IDEAL_API_SKETCH.md``: a stable, machine-readable
description of the entire SoC hierarchy that downstream tools (web
register browsers, golden-trace diffing utilities, documentation
generators, etc.) can consume **without importing**
``peakrdl_pybind11``.

The two public entry points are:

* :func:`to_dict` — return the schema as a Python ``dict``.
* :func:`to_json` — return the schema serialized to a JSON string.

Both are also wired onto every generated SoC via the registry's
``post_create`` seam — call ``soc.schema()`` to get the dict.

Schema shape (top-level)::

    {
        "schema_version": 1,
        "kind": "soc",
        "name": "...",
        "path": "...",
        "description": "...",          # optional, only if present
        "children": [ <child node>, ... ],
    }

Per-node fields:

* ``kind`` — one of ``"soc"`` / ``"addrmap"`` / ``"regfile"`` / ``"reg"``
  / ``"field"`` / ``"mem"``.
* ``name`` — the inst name.
* ``path`` — full dotted path.
* ``address`` — absolute address for ``reg`` / ``mem`` (emitted as a
  Python ``int``; JSON serializers render this as decimal — consumers
  can format hex however they like).
* ``width`` — register / memory word width in bits (``reg`` / ``mem``).
* ``description`` — from ``info.description`` (a.k.a. ``info.desc``)
  when present.
* ``fields`` — list of field dicts on ``reg`` nodes.
* ``children`` — list of child node dicts on container nodes
  (``soc`` / ``addrmap`` / ``regfile``).

Per-field dict::

    {
        "kind": "field",
        "name": "...",
        "path": "...",
        "lsb": int,
        "width": int,
        "is_readable":   bool,
        "is_writable":   bool,
        "is_hw_readable": bool,
        "is_hw_writable": bool,
        "access":  str | None,
        "reset":   int | None,
        "description": str | None,
        "encode": { ... }      # optional — only when encode/enum metadata exists
    }

Determinism
-----------
The walk uses ``vars(node)`` so children appear in **declaration order**
(dict insertion order on CPython 3.7+), not alphabetical. This matches
the conventions established by :mod:`runtime.widgets` and
:mod:`runtime.routing`.
"""

from __future__ import annotations

import json
from typing import Any

# ----------------------------------------------------------------------
# Kind discrimination
# ----------------------------------------------------------------------

# Kinds we render as their own subtree. ``soc`` is the positional kind
# assigned to whatever node ``to_dict`` is called on; everything else is
# inferred from the class name (the same convention :mod:`runtime.widgets`
# uses for ``_node_kind``).
_CONTAINER_KINDS = frozenset({"soc", "addrmap", "regfile"})
_LEAF_KINDS = frozenset({"reg", "mem", "field"})
_NODE_KINDS = _CONTAINER_KINDS | _LEAF_KINDS


def _node_kind(node: Any) -> str:
    """Classify a node into the kind taxonomy from §17.

    Probe order:

    1. An explicit ``_node_kind`` attribute (the escape hatch used by
       widget fakes and any node that wants to lie about its class).
    2. Class-name substring matches, longest first to disambiguate
       ``regfile`` from ``reg`` and ``memory`` from ``mem``.
    3. Fallback: ``"node"`` (rendered as a generic addrmap-like
       container with no special attributes).
    """
    explicit = getattr(node, "_node_kind", None)
    if isinstance(explicit, str):
        return explicit

    cls = type(node).__name__.lower()
    # Order matters: ``regfile`` must be tested before ``reg``.
    for tag in ("regfile", "addrmap", "memory", "field"):
        if tag in cls:
            return "regfile" if tag == "regfile" else ("mem" if tag == "memory" else tag)
    # ``mem`` substring alone, then ``reg`` last.
    if "mem" in cls:
        return "mem"
    if "reg" in cls:
        return "reg"
    return "node"


# ----------------------------------------------------------------------
# Info accessor helpers — best-effort, fall back to direct attrs.
# ----------------------------------------------------------------------


def _info(node: Any) -> Any:
    return getattr(node, "info", None)


def _attr(node: Any, *names: str, default: Any = None) -> Any:
    """Return the first non-``None`` attribute resolved from info or node.

    Mirrors :func:`runtime.widgets._field_attr`: tries ``node.info.<name>``
    first, then falls back to the bare attribute on ``node``.
    """
    info = _info(node)
    for name in names:
        if info is not None:
            value = getattr(info, name, None)
            if value is not None:
                return value
        value = getattr(node, name, None)
        if value is not None:
            return value
    return default


def _description(node: Any) -> str | None:
    """Pull the human-readable description from info or the node itself."""
    info = _info(node)
    if info is not None:
        # ``info.desc`` is canonical (see :mod:`runtime.info`); some
        # consumers spell it ``description`` so we honour both.
        for attr in ("description", "desc"):
            value = getattr(info, attr, None)
            if value:
                return str(value)
    for attr in ("description", "desc"):
        value = getattr(node, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _path(node: Any, default: str = "") -> str:
    info = _info(node)
    if info is not None:
        path = getattr(info, "path", None)
        if isinstance(path, str) and path:
            return path
    for name in ("path", "name", "inst_name"):
        value = getattr(node, name, None)
        if isinstance(value, str) and value:
            return value
    return default


def _name(node: Any, default: str = "") -> str:
    info = _info(node)
    if info is not None:
        value = getattr(info, "name", None)
        if isinstance(value, str) and value:
            return value
    for attr in ("name", "inst_name"):
        value = getattr(node, attr, None)
        if isinstance(value, str) and value:
            return value
    return default


def _address(node: Any) -> int | None:
    info = _info(node)
    if info is not None:
        addr = getattr(info, "address", None)
        if isinstance(addr, int):
            return addr
    for attr in ("address", "absolute_address", "offset"):
        value = getattr(node, attr, None)
        if isinstance(value, int):
            return value
    return None


def _width_for_reg(node: Any) -> int | None:
    """Register / memory word width in bits."""
    return _attr(node, "regwidth", "width", default=None)


def _access_str(value: Any) -> str | None:
    """Coerce an ``AccessMode``/``str``/``None`` into a plain lowercase token.

    ``AccessMode`` already subclasses :class:`str`, but using ``str(...)``
    explicitly drops the ``AccessMode.RW`` prefix that ``repr`` would
    emit. Callers can keep comparing against ``"rw"`` etc.
    """
    if value is None:
        return None
    return str(value).lower()


def _encode_metadata(field_node: Any) -> dict[str, Any] | None:
    """Extract field-decode metadata (``encode``/``enum``) when present.

    The runtime ``Info`` does not yet expose this directly, so we probe
    the conventional attribute shapes documented in §8 and §17:

    * ``field.encode`` / ``field.info.encode`` — a class (typically an
      ``IntEnum``) or a dict-like mapping.
    * ``field.enum`` / ``field.info.enum`` — same shape, older name.
    * ``field.info.tags.encode`` — UDP-style fallback.

    The result is a nested ``dict`` with a stable ``type`` / ``members``
    shape so consumers can rebuild the enum without importing the
    generated module. Returns ``None`` when no metadata is found, so
    callers can omit the key entirely.
    """
    info = _info(field_node)
    candidates: list[Any] = []
    for source in (info, field_node):
        if source is None:
            continue
        for attr in ("encode", "enum"):
            value = getattr(source, attr, None)
            if value is not None:
                candidates.append(value)
        tags = getattr(source, "tags", None)
        if tags is not None:
            for attr in ("encode", "enum"):
                value = getattr(tags, attr, None)
                if value is not None:
                    candidates.append(value)

    for candidate in candidates:
        members = _encode_members(candidate)
        if members is not None:
            type_name = getattr(candidate, "__name__", None) or type(candidate).__name__
            return {"type": str(type_name), "members": members}
    return None


def _encode_members(value: Any) -> dict[str, int] | None:
    """Best-effort extraction of ``{member_name: int_value}`` from ``value``.

    Recognises three shapes:

    1. An ``enum.Enum`` subclass — iterate it, ``int()`` each member.
    2. A plain dict / mapping — coerce values to ``int`` when possible.
    3. Anything else — return ``None`` so the caller skips it.
    """
    # Enum subclass: iterating it yields members.
    try:
        # ``__members__`` is the safest cross-version handle for enum
        # classes and survives mocking with ``types.SimpleNamespace``
        # via ``getattr`` (which we handle below).
        members_map = getattr(value, "__members__", None)
        if members_map is not None:
            out: dict[str, int] = {}
            for name, member in members_map.items():
                try:
                    out[str(name)] = int(member)
                except (TypeError, ValueError):
                    continue
            if out:
                return out
    except Exception:
        pass

    # Dict-like fallback.
    if isinstance(value, dict):
        out2: dict[str, int] = {}
        for k, v in value.items():
            try:
                out2[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        if out2:
            return out2

    return None


# ----------------------------------------------------------------------
# Per-node serializers
# ----------------------------------------------------------------------


def _is_readable_from_access(access: str | None) -> bool:
    """Return ``True`` iff ``access`` permits software reads."""
    if not access:
        return True  # default to readable when unspecified
    token = access.lower()
    if token == "na":
        return False
    return "r" in token


def _is_writable_from_access(access: str | None) -> bool:
    """Return ``True`` iff ``access`` permits software writes."""
    if not access:
        return True  # default to writable when unspecified
    token = access.lower()
    if token == "na":
        return False
    return "w" in token


def _field_dict(field: Any) -> dict[str, Any]:
    """Render one field as a schema dict.

    ``lsb`` is sourced from ``info.offset`` (the convention used by
    :func:`runtime.info._info_factory`, where field metadata is stashed
    as ``(lsb, width)`` and the lsb shows up under ``offset``). When the
    field exposes a direct ``lsb`` attr we honour that first.
    """
    info = _info(field)

    lsb = _attr(field, "lsb", default=None)
    if lsb is None and info is not None:
        # Per ``info._info_factory``, fields carry their lsb in ``offset``.
        offset = getattr(info, "offset", None)
        if isinstance(offset, int):
            lsb = offset
    if lsb is None:
        lsb = 0

    width = _attr(field, "width", "regwidth", default=1)

    access_value = _attr(field, "access", default=None)
    access = _access_str(access_value)

    # Some test fakes expose ``is_readable`` / ``is_writable`` directly.
    is_readable = _attr(field, "is_readable")
    if is_readable is None:
        is_readable = _is_readable_from_access(access)
    is_writable = _attr(field, "is_writable")
    if is_writable is None:
        is_writable = _is_writable_from_access(access)

    out: dict[str, Any] = {
        "kind": "field",
        "name": _name(field),
        "path": _path(field),
        "lsb": int(lsb),
        "width": int(width),
        "is_readable": bool(is_readable),
        "is_writable": bool(is_writable),
        "is_hw_readable": bool(_attr(field, "is_hw_readable", default=False)),
        "is_hw_writable": bool(_attr(field, "is_hw_writable", default=False)),
        "access": access,
        "reset": _attr(field, "reset", default=None),
        "description": _description(field),
    }

    encode = _encode_metadata(field)
    if encode is not None:
        out["encode"] = encode
    return out


def _iter_fields(reg: Any) -> list[Any]:
    """List the field children of a register in declaration order.

    Three shapes are recognised:

    1. ``reg.fields`` — a callable, dict, or iterable on the register.
       Test fakes typically expose a callable; generated bindings expose
       a tuple. Both round-trip into a list of field nodes.
    2. ``reg.info.fields`` — populated by :mod:`runtime.info`. This is
       a dict of ``{name: Info}`` — *not* field node objects — but the
       :func:`_field_dict` serializer is duck-typed against ``Info``
       too, so we can pass these straight through.
    3. ``vars(reg)`` — last-ditch fallback. We scan the instance dict in
       declaration order and pick out anything that quacks like a field
       (``lsb`` attribute, or class name containing "field").
    """
    fields_attr = getattr(reg, "fields", None)
    if fields_attr is not None:
        candidates: Any = fields_attr
        if callable(candidates):
            try:
                candidates = candidates()
            except TypeError:
                candidates = None
        if candidates is not None:
            if isinstance(candidates, dict):
                # Dict insertion order matches declaration order.
                return list(candidates.values())
            try:
                listed = list(candidates)
                if listed:
                    return listed
            except TypeError:
                pass

    info = _info(reg)
    if info is not None:
        info_fields = getattr(info, "fields", None)
        if isinstance(info_fields, dict) and info_fields:
            return list(info_fields.values())

    # Final fallback: walk ``vars()`` looking for field-shaped attributes.
    out: list[Any] = []
    try:
        items = vars(reg).items()
    except TypeError:
        return out
    for name, value in items:
        if name.startswith("_"):
            continue
        if value is reg:
            continue
        if _node_kind(value) == "field" or hasattr(value, "lsb"):
            out.append(value)
    return out


def _iter_children(node: Any) -> list[Any]:
    """List direct child nodes (reg/regfile/addrmap/mem) in declaration order.

    Uses ``vars(node)`` so the order matches dict insertion (which on
    CPython 3.7+ is declaration order for kwarg-built objects). Children
    are anything whose ``_node_kind`` classification is in
    :data:`_NODE_KINDS` (excluding plain ``"field"`` — fields are nested
    inside their parent register, not direct children of containers).
    """
    out: list[Any] = []
    seen: set[int] = set()
    try:
        items = vars(node).items()
    except TypeError:
        return out

    for attr, value in items:
        if attr.startswith("_") or value is node or callable(value):
            continue
        if id(value) in seen:
            continue
        kind = _node_kind(value)
        if kind in ("reg", "regfile", "addrmap", "mem"):
            seen.add(id(value))
            out.append(value)
    return out


def _container_dict(node: Any, kind: str) -> dict[str, Any]:
    """Render a container node (soc / addrmap / regfile)."""
    out: dict[str, Any] = {
        "kind": kind,
        "name": _name(node),
        "path": _path(node),
    }
    address = _address(node)
    if address is not None:
        out["address"] = int(address)
    description = _description(node)
    if description is not None:
        out["description"] = description
    out["children"] = [_node_dict(child) for child in _iter_children(node)]
    return out


def _reg_dict(node: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "reg",
        "name": _name(node),
        "path": _path(node),
    }
    address = _address(node)
    if address is not None:
        out["address"] = int(address)
    width = _width_for_reg(node)
    if width is not None:
        out["width"] = int(width)
    description = _description(node)
    if description is not None:
        out["description"] = description
    out["fields"] = [_field_dict(f) for f in _iter_fields(node)]
    return out


def _mem_dict(node: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "mem",
        "name": _name(node),
        "path": _path(node),
    }
    address = _address(node)
    if address is not None:
        out["address"] = int(address)
    width = _width_for_reg(node)
    if width is not None:
        out["width"] = int(width)
    description = _description(node)
    if description is not None:
        out["description"] = description
    return out


def _node_dict(node: Any, *, root_kind: str | None = None) -> dict[str, Any]:
    """Dispatch a node to the appropriate serializer.

    ``root_kind`` overrides the auto-classification — used for the entry
    point ``to_dict(soc)`` where the caller wants the root labelled
    ``"soc"`` regardless of the underlying class name.
    """
    if root_kind is not None:
        kind = root_kind
    else:
        kind = _node_kind(node)

    if kind == "field":
        return _field_dict(node)
    if kind == "reg":
        return _reg_dict(node)
    if kind == "mem":
        return _mem_dict(node)
    if kind in _CONTAINER_KINDS:
        return _container_dict(node, kind)
    # Unknown kinds fall back to addrmap-style traversal so a partly-
    # populated tree still renders something rather than crashing.
    return _container_dict(node, "addrmap")


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

#: Format version stamped on every schema dict. Increment when the shape
#: changes in a non-backwards-compatible way.
SCHEMA_VERSION = 1


def to_dict(soc: Any) -> dict[str, Any]:
    """Return a reflective schema dict for ``soc``.

    The root is always labelled ``kind="soc"`` regardless of the actual
    class name — generated SoC classes are usually ``addrmap``-like, but
    §17 fixes the root tag at "soc". A top-level ``schema_version`` key
    tags the format version so consumers can detect future shape changes.
    """
    out = _node_dict(soc, root_kind="soc")
    return {"schema_version": SCHEMA_VERSION, **out}


def to_json(soc: Any, indent: int | None = 2) -> str:
    """Return :func:`to_dict` as a JSON string.

    Addresses round-trip as decimal integers; consumers that want
    hexadecimal can format them on their side. ``indent`` follows
    :func:`json.dumps` semantics (``None`` for compact output).
    """
    return json.dumps(to_dict(soc), indent=indent, sort_keys=False)


# ----------------------------------------------------------------------
# Registry wiring — attach ``soc.schema()`` after creation.
# ----------------------------------------------------------------------

try:  # pragma: no cover - depends on Unit 1 landing order
    from . import _registry  # type: ignore[attr-defined]
except ImportError:
    _registry = None  # type: ignore[assignment]


def _attach_schema_to_soc(soc: Any) -> None:
    """Post-create hook: bind ``soc.schema()`` returning the schema dict.

    Mirrors the ``setattr``-with-swallowed-AttributeError pattern used by
    :func:`runtime.widgets._attach_tree_dump_to_soc` and
    :func:`runtime.routing._try_setattr`. pybind11 classes without
    ``py::dynamic_attr()`` reject the ``setattr`` silently — that's the
    correct behaviour, since generated bindings can ship their own
    ``schema()`` implementation.
    """
    if hasattr(soc, "schema") and callable(getattr(soc, "schema", None)):
        return

    def _bound_schema() -> dict[str, Any]:
        return to_dict(soc)

    try:
        soc.schema = _bound_schema  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass


if _registry is not None and hasattr(_registry, "register_post_create"):
    _registry.register_post_create(_attach_schema_to_soc)


__all__ = [
    "SCHEMA_VERSION",
    "to_dict",
    "to_json",
]
