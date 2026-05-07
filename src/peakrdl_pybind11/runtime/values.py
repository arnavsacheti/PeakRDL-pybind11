"""
Typed register and field values (sketch §3.2, §22.1).

``RegisterValue`` and ``FieldValue`` are immutable, hashable ``int`` subclasses
that carry decode metadata. They are the values returned by ``reg.read()`` /
``field.read()`` once the runtime is wired in. Mutation goes through
``.replace(**fields)``; never through attribute assignment.

Design notes
------------
* Both types subclass ``int`` so they are naturally immutable, hashable, and
  picklable. Hashing degenerates to ``hash(int(self))``. Note that CPython
  forbids ``__slots__`` on ``int`` subclasses, so metadata is set on the
  instance ``__dict__`` and treated as private.
* ``__getattr__`` short-circuits on dunder/private names so pickle, copy, and
  IPython introspection do not recurse.
* Pickling uses module-level ``_rebuild_*`` functions because ``__new__``
  takes keyword-only metadata. Returning a positional tuple via
  ``__reduce__`` would otherwise require us to bend ``__new__``.
* ``FieldValue`` carries an optional ``encode`` (an ``IntEnum`` class). When
  set, ``__repr__`` decodes the value to its enum member.
* ``RegisterValue.build(register_class, **fields)`` and the module-level
  ``build()`` are the compose-then-write helpers from §3.3.

The ``did_you_mean`` helper is sourced from
``peakrdl_pybind11.runtime.errors`` (Unit 2) when available; we fall back to
``difflib.get_close_matches`` so this module stands alone.
"""

from __future__ import annotations

import importlib
import json
from enum import IntEnum
from typing import TYPE_CHECKING, Any, ClassVar, Self

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

try:  # pragma: no cover - depends on Unit 2 landing
    from .errors import did_you_mean as _did_you_mean
except ImportError:  # pragma: no cover - exercised only when Unit 2 is absent
    from difflib import get_close_matches

    def _did_you_mean(name: str, candidates: Iterable[str]) -> str:
        """Return a ' did you mean: <name>?' suffix or empty string.

        Fallback used until ``runtime/errors.py`` (Unit 2) lands.
        """
        matches = get_close_matches(name, list(candidates), n=1, cutoff=0.6)
        if not matches:
            return ""
        return f"; did you mean {matches[0]!r}?"


__all__ = ["FieldValue", "RegisterValue", "build"]


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #


def _resolve_encode(qualname: str | None) -> type[IntEnum] | None:
    """Best-effort resolve of an ``IntEnum`` qualified name to its class."""
    if not qualname:
        return None
    module_name, _, attr = qualname.rpartition(".")
    if not module_name:
        return None
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    obj: Any = module
    for part in attr.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    if isinstance(obj, type) and issubclass(obj, IntEnum):
        return obj
    return None


def _qualname_of(cls: type[IntEnum] | None) -> str | None:
    """Inverse of :func:`_resolve_encode` — used for serialization."""
    if cls is None:
        return None
    module = getattr(cls, "__module__", None)
    qualname = getattr(cls, "__qualname__", None) or cls.__name__
    if not module:
        return qualname
    return f"{module}.{qualname}"


def _normalize_field_meta(
    fields: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Accept ``{"name": (lsb, width)}`` or richer dict form and normalize.

    The richer form is ``{"name": {"lsb": int, "width": int, "encode": IntEnum,
    "description": str, "reset": int}}`` — only ``lsb`` and ``width`` are
    required.
    """
    if not fields:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, meta in fields.items():
        if isinstance(meta, tuple) and len(meta) == 2:
            lsb, width = meta
            out[name] = {"lsb": int(lsb), "width": int(width)}
            continue
        if not isinstance(meta, dict):
            raise TypeError(
                f"field metadata for {name!r} must be a (lsb, width) tuple or dict, got {type(meta).__name__}"
            )
        if "lsb" not in meta or "width" not in meta:
            raise ValueError(f"field metadata for {name!r} must include 'lsb' and 'width'")
        cleaned: dict[str, Any] = {"lsb": int(meta["lsb"]), "width": int(meta["width"])}
        for key in ("encode", "description", "reset"):
            if key in meta and meta[key] is not None:
                cleaned[key] = meta[key]
        out[name] = cleaned
    return out


def _coerce_field_value(value: Any, *, name: str, width: int) -> int:  # noqa: ANN401
    """Coerce a field write value (int / IntEnum / bool) and bounds-check it."""
    # bool and IntEnum are both int subclasses, so a single isinstance covers them.
    if not isinstance(value, int):
        raise TypeError(f"field {name!r} expects int/IntEnum/bool, got {type(value).__name__}")
    ival = int(value)
    max_val = (1 << width) - 1
    if ival < 0 or ival > max_val:
        raise ValueError(f"field {name!r} value {ival} out of range [0, {max_val}] (width={width})")
    return ival


def _format_grouped(text: str, group: int, sep: str = "_") -> str:
    """Right-aligned underscore grouping for hex/bin string bodies."""
    if group <= 0 or len(text) <= group:
        return text
    # Split from the right so the leftmost group can be short.
    rev = text[::-1]
    chunks = [rev[i : i + group] for i in range(0, len(rev), group)]
    return sep.join(chunk[::-1] for chunk in chunks[::-1])


def _format_encoded(value: int, encode: type[IntEnum] | None) -> str | None:
    """Return ``EnumName.MEMBER`` if value decodes, else ``None``."""
    if encode is None:
        return None
    try:
        member = encode(int(value))
    except ValueError:
        return None
    return f"{encode.__name__}.{member.name}"


# --------------------------------------------------------------------------- #
#  FieldValue
# --------------------------------------------------------------------------- #


def _rebuild_field_value(int_val: int, metadata: dict[str, Any]) -> FieldValue:
    """Pickle hook — reconstruct a FieldValue from positional metadata."""
    encode_qualname = metadata.get("encode")
    encode = _resolve_encode(encode_qualname) if isinstance(encode_qualname, str) else None
    return FieldValue(
        int_val,
        lsb=metadata["lsb"],
        width=metadata["width"],
        name=metadata.get("name"),
        register_path=metadata.get("register_path"),
        encode=encode,
        description=metadata.get("description"),
    )


class FieldValue(int):
    """An immutable field value with bit position, width, and decode metadata.

    Instances behave like ``int`` (arithmetic, comparison, hashing) but carry
    enough context to format themselves nicely and round-trip through pickle
    or JSON.
    """

    _lsb: int
    _width: int
    _name: str | None
    _register_path: str | None
    _encode: type[IntEnum] | None
    _description: str | None

    def __new__(
        cls,
        value: int,
        *,
        lsb: int,
        width: int,
        name: str | None = None,
        register_path: str | None = None,
        encode: type[IntEnum] | None = None,
        description: str | None = None,
    ) -> Self:
        if width <= 0:
            raise ValueError(f"width must be positive, got {width}")
        if lsb < 0:
            raise ValueError(f"lsb must be non-negative, got {lsb}")
        # Mask to width — protects against accidental sign or overflow.
        mask = (1 << width) - 1
        instance = super().__new__(cls, int(value) & mask)
        instance._lsb = lsb
        instance._width = width
        instance._name = name
        instance._register_path = register_path
        instance._encode = encode
        instance._description = description
        return instance

    # -- properties -------------------------------------------------------- #

    @property
    def lsb(self) -> int:
        return self._lsb

    @property
    def width(self) -> int:
        return self._width

    @property
    def msb(self) -> int:
        return self._lsb + self._width - 1

    @property
    def mask(self) -> int:
        """Bit mask aligned to the parent register (lsb-shifted)."""
        return ((1 << self._width) - 1) << self._lsb

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def register_path(self) -> str | None:
        return self._register_path

    @property
    def encode(self) -> type[IntEnum] | None:
        return self._encode

    @property
    def description(self) -> str | None:
        return self._description

    # -- behavior ---------------------------------------------------------- #

    def __bool__(self) -> bool:
        return int(self) != 0

    def decoded(self) -> IntEnum | int:
        """Return the enum member if ``encode`` is set, else the int value."""
        if self._encode is not None:
            try:
                return self._encode(int(self))
            except ValueError:
                return int(self)
        return int(self)

    def __repr__(self) -> str:
        decoded = _format_encoded(int(self), self._encode)
        if decoded is not None:
            return f"{decoded} ({int(self)})"
        return f"FieldValue({int(self):#x}, lsb={self._lsb}, width={self._width})"

    # -- serialization ----------------------------------------------------- #

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        return (
            _rebuild_field_value,
            (
                int(self),
                {
                    "lsb": self._lsb,
                    "width": self._width,
                    "name": self._name,
                    "register_path": self._register_path,
                    "encode": _qualname_of(self._encode),
                    "description": self._description,
                },
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "value": int(self),
            "lsb": self._lsb,
            "width": self._width,
            "name": self._name,
            "register_path": self._register_path,
            "encode": _qualname_of(self._encode),
            "description": self._description,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FieldValue:
        encode = _resolve_encode(data.get("encode"))
        return cls(
            int(data["value"]),
            lsb=int(data["lsb"]),
            width=int(data["width"]),
            name=data.get("name"),
            register_path=data.get("register_path"),
            encode=encode,
            description=data.get("description"),
        )


# --------------------------------------------------------------------------- #
#  RegisterValue
# --------------------------------------------------------------------------- #


def _rebuild_register_value(int_val: int, metadata: dict[str, Any]) -> RegisterValue:
    """Pickle hook — see :func:`_rebuild_field_value`."""
    fields = metadata.get("fields") or {}
    # Resolve any ``encode`` qualnames stored as strings.
    resolved: dict[str, dict[str, Any]] = {}
    for fname, fmeta in fields.items():
        copy = dict(fmeta)
        enc = copy.get("encode")
        if isinstance(enc, str):
            copy["encode"] = _resolve_encode(enc)
        resolved[fname] = copy
    return RegisterValue(
        int_val,
        address=metadata.get("address", 0),
        width=metadata.get("width", 32),
        fields=resolved,
        name=metadata.get("name"),
        path=metadata.get("path"),
        description=metadata.get("description"),
    )


class RegisterValue(int):
    """An immutable register value with field decode metadata.

    Behaves like ``int`` for hashing and arithmetic. Field access is via
    attribute (``v.enable``) or item lookup (``v["enable"]``); both return a
    :class:`FieldValue`. Mutation produces a new value via ``.replace(...)``.
    """

    # Reserved attribute names that must not be shadowed by RDL field names.
    _RESERVED: ClassVar[frozenset[str]] = frozenset(
        {
            "address",
            "width",
            "fields",
            "name",
            "path",
            "description",
            "register_class",
        }
    )

    _address: int
    _width: int
    _fields_meta: dict[str, dict[str, Any]]
    _name: str | None
    _path: str | None
    _description: str | None
    _register_class: Any

    def __new__(
        cls,
        value: int,
        *,
        address: int = 0,
        width: int = 32,
        fields: Mapping[str, Any] | None = None,
        register_class: Any = None,  # noqa: ANN401
        name: str | None = None,
        path: str | None = None,
        description: str | None = None,
    ) -> Self:
        if width <= 0:
            raise ValueError(f"width must be positive, got {width}")
        mask = (1 << width) - 1
        instance = super().__new__(cls, int(value) & mask)
        instance._address = int(address)
        instance._width = int(width)
        instance._fields_meta = _normalize_field_meta(fields)
        instance._register_class = register_class
        instance._name = name
        instance._path = path
        instance._description = description
        return instance

    # -- properties -------------------------------------------------------- #

    @property
    def address(self) -> int:
        return self._address

    @property
    def width(self) -> int:
        return self._width

    @property
    def fields(self) -> dict[str, dict[str, Any]]:
        """Read-only view of field metadata (lsb, width, encode, ...)."""
        return dict(self._fields_meta)

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def description(self) -> str | None:
        return self._description

    @property
    def register_class(self) -> Any:  # noqa: ANN401
        return self._register_class

    # -- field access ------------------------------------------------------ #

    def _make_field(self, fname: str) -> FieldValue:
        meta = self._fields_meta[fname]
        lsb = meta["lsb"]
        width = meta["width"]
        raw = (int(self) >> lsb) & ((1 << width) - 1)
        return FieldValue(
            raw,
            lsb=lsb,
            width=width,
            name=fname,
            register_path=self._path,
            encode=meta.get("encode"),
            description=meta.get("description"),
        )

    def _missing_field_message(self, fname: str) -> str:
        suggestion = _did_you_mean(fname, self._fields_meta.keys())
        owner = self._path or self._name or "<unnamed>"
        return f"register {owner} has no field {fname!r}{suggestion}"

    def __getattr__(self, name: str) -> FieldValue:
        # Block dunder/private lookups so pickle, copy, and IPython
        # introspection don't recurse through field metadata.
        if name.startswith("_") or name in self._RESERVED:
            raise AttributeError(name)
        try:
            fields_meta = self._fields_meta
        except AttributeError:  # pragma: no cover - during __new__
            raise AttributeError(name) from None
        if name in fields_meta:
            return self._make_field(name)
        raise AttributeError(self._missing_field_message(name))

    def __getitem__(self, key: str | slice | int) -> FieldValue | int:
        """Field access by name, or integer/slice bit access."""
        if isinstance(key, str):
            if key not in self._fields_meta:
                raise KeyError(self._missing_field_message(key))
            return self._make_field(key)
        if isinstance(key, int):
            if key < 0:
                key += self._width
            if not 0 <= key < self._width:
                raise IndexError(f"bit index {key} out of range for width {self._width}")
            return (int(self) >> key) & 1
        if isinstance(key, slice):
            start, stop, step = key.indices(self._width)
            if step != 1:
                raise ValueError("RegisterValue bit slices must use step=1")
            if stop <= start:
                return 0
            return (int(self) >> start) & ((1 << (stop - start)) - 1)
        raise TypeError(f"register value indices must be str, int, or slice, got {type(key).__name__}")

    # -- mutation (returns a new value) ----------------------------------- #

    def replace(self, **fields: Any) -> RegisterValue:  # noqa: ANN401
        """Return a new :class:`RegisterValue` with the given fields replaced.

        Unknown field names raise ``KeyError`` with a did-you-mean suggestion.
        Field values are bounds-checked against the field width.
        """
        new_value = int(self)
        for fname, fval in fields.items():
            if fname not in self._fields_meta:
                raise KeyError(self._missing_field_message(fname))
            meta = self._fields_meta[fname]
            lsb = meta["lsb"]
            width = meta["width"]
            ival = _coerce_field_value(fval, name=fname, width=width)
            mask = ((1 << width) - 1) << lsb
            new_value = (new_value & ~mask) | (ival << lsb)
        return type(self)(
            new_value,
            address=self._address,
            width=self._width,
            fields=self._fields_meta,
            register_class=self._register_class,
            name=self._name,
            path=self._path,
            description=self._description,
        )

    # -- formatting -------------------------------------------------------- #

    def hex(self, group: int = 4) -> str:
        """Return ``0xAAAA_BBBB``-style hex (``group`` hex chars per group)."""
        nibbles = max(1, (self._width + 3) // 4)
        body = format(int(self), f"0{nibbles}x")
        return "0x" + _format_grouped(body, group)

    def bin(self, group: int = 8, fields: bool = False) -> str:
        """Return binary; with ``fields=True``, mark field boundaries with ``|``.

        ``group`` controls underscore grouping when ``fields=False``. When
        ``fields=True``, boundaries between adjacent fields are marked with
        ``|`` and a single underscore separates same-field bytes.
        """
        body = format(int(self), f"0{self._width}b")
        if not fields:
            return "0b" + _format_grouped(body, group)

        # Build a per-bit owner map: which field (if any) owns each bit.
        owners: list[str | None] = [None] * self._width
        for fname, meta in self._fields_meta.items():
            lsb = meta["lsb"]
            width = meta["width"]
            for bit in range(lsb, min(lsb + width, self._width)):
                owners[bit] = fname

        # Walk MSB-first (string is left-to-right MSB-first).
        out: list[str] = []
        prev_owner: str | None | object = object()  # sentinel
        for i, ch in enumerate(body):
            bit_index = self._width - 1 - i
            owner = owners[bit_index]
            if i == 0:
                out.append(ch)
                prev_owner = owner
                continue
            if owner != prev_owner:
                out.append("|")
            elif group > 0 and (self._width - bit_index - 1) % group == 0:
                out.append("_")
            out.append(ch)
            prev_owner = owner
        return "0b" + "".join(out)

    def table(self) -> str:
        """Multi-line ASCII table of fields, decoded values, and descriptions."""
        if not self._fields_meta:
            return f"{self._name or 'register'} = {self.hex()}\n  (no fields)"

        rows: list[tuple[str, str, str, str, str]] = []
        for fname, meta in self._fields_meta.items():
            lsb = meta["lsb"]
            width = meta["width"]
            raw = (int(self) >> lsb) & ((1 << width) - 1)
            bits = f"[{lsb}]" if width == 1 else f"[{lsb + width - 1}:{lsb}]"
            encode = meta.get("encode")
            decoded_label = _format_encoded(raw, encode)
            if decoded_label is not None:
                decoded = f"{decoded_label} ({raw})"
            elif encode is not None:
                decoded = f"{raw} (out of range)"
            elif width == 1:
                decoded = "1" if raw else "0"
            else:
                decoded = f"{raw:#x}"
            rows.append(
                (
                    bits,
                    fname,
                    decoded,
                    f"{raw:#x}",
                    meta.get("description") or "",
                )
            )

        # Column widths
        headers = ("Bits", "Field", "Value", "Raw", "Description")
        widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

        def fmt_row(values: tuple[str, ...]) -> str:
            return "  ".join(v.ljust(widths[i]) for i, v in enumerate(values)).rstrip()

        title = f"{self._path or self._name or 'register'} = {self.hex()}"
        if self._address:
            title += f"  @ {self._address:#x}"
        lines = [title, "", fmt_row(headers), fmt_row(tuple("-" * w for w in widths))]
        lines.extend(fmt_row(r) for r in rows)
        return "\n".join(lines)

    # -- repr -------------------------------------------------------------- #

    def __repr__(self) -> str:
        cls_name = self._name or type(self).__name__
        parts = [self.hex()]
        for fname, meta in self._fields_meta.items():
            lsb = meta["lsb"]
            width = meta["width"]
            raw = (int(self) >> lsb) & ((1 << width) - 1)
            decoded = _format_encoded(raw, meta.get("encode"))
            parts.append(f"{fname}={decoded}" if decoded is not None else f"{fname}={raw}")
        if len(parts) == 1:
            return f"{cls_name}({parts[0]})"
        return f"{cls_name}({parts[0]}, {', '.join(parts[1:])})"

    # -- serialization ----------------------------------------------------- #

    def _serialize_fields(self) -> dict[str, dict[str, Any]]:
        """Stringify ``encode`` classes for JSON/pickle payloads."""
        out: dict[str, dict[str, Any]] = {}
        for fname, meta in self._fields_meta.items():
            entry = dict(meta)
            enc = entry.get("encode")
            if isinstance(enc, type) and issubclass(enc, IntEnum):
                entry["encode"] = _qualname_of(enc)
            out[fname] = entry
        return out

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        return (
            _rebuild_register_value,
            (
                int(self),
                {
                    "address": self._address,
                    "width": self._width,
                    "fields": self._serialize_fields(),
                    "name": self._name,
                    "path": self._path,
                    "description": self._description,
                },
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "value": int(self),
            "address": self._address,
            "width": self._width,
            "name": self._name,
            "path": self._path,
            "description": self._description,
            "fields": self._serialize_fields(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RegisterValue:
        raw_fields = data.get("fields") or {}
        resolved: dict[str, dict[str, Any]] = {}
        for fname, meta in raw_fields.items():
            entry = dict(meta)
            enc = entry.get("encode")
            if isinstance(enc, str):
                entry["encode"] = _resolve_encode(enc)
            resolved[fname] = entry
        return cls(
            int(data["value"]),
            address=int(data.get("address", 0)),
            width=int(data.get("width", 32)),
            fields=resolved,
            name=data.get("name"),
            path=data.get("path"),
            description=data.get("description"),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, payload: str) -> RegisterValue:
        return cls.from_dict(json.loads(payload))

    # -- compose-then-write ------------------------------------------------ #

    @classmethod
    def build(
        cls,
        register_class: Any = None,  # noqa: ANN401
        /,
        *,
        address: int | None = None,
        width: int | None = None,
        fields: Mapping[str, Any] | None = None,
        name: str | None = None,
        path: str | None = None,
        description: str | None = None,
        **field_values: Any,  # noqa: ANN401
    ) -> RegisterValue:
        """Compose a new :class:`RegisterValue` from field values.

        The first positional argument is an optional *register descriptor*: any
        object that exposes ``.fields`` (mapping of field metadata),
        ``.address`` / ``.offset``, ``.width`` / ``.regwidth``, and an
        optional ``.reset`` value. Anything not provided by ``register_class``
        can be passed as a keyword argument. Fields not listed in
        ``field_values`` start at the descriptor's reset value (or zero).
        """
        meta = _extract_register_class_meta(register_class)
        merged: dict[str, Any] = dict(meta.get("fields") or {})
        if fields:
            merged.update(fields)
        merged_fields = _normalize_field_meta(merged)

        reset_value = int(meta.get("reset", 0))
        for fmeta in merged_fields.values():
            field_reset = fmeta.get("reset")
            if field_reset is None:
                continue
            lsb = fmeta["lsb"]
            w = fmeta["width"]
            mask = ((1 << w) - 1) << lsb
            reset_value = (reset_value & ~mask) | ((int(field_reset) & ((1 << w) - 1)) << lsb)

        seed = cls(
            reset_value,
            address=meta["address"] if address is None else address,
            width=meta["width"] if width is None else width,
            fields=merged_fields,
            register_class=register_class,
            name=name if name is not None else meta.get("name"),
            path=path if path is not None else meta.get("path"),
            description=description if description is not None else meta.get("description"),
        )
        return seed.replace(**field_values) if field_values else seed


def _extract_register_class_meta(register_class: Any) -> dict[str, Any]:  # noqa: ANN401
    """Pull the bits we need off a register-descriptor object.

    Any of the keys may be missing — the caller will fill them with defaults.
    """
    if register_class is None:
        return {"address": 0, "width": 32, "fields": {}, "reset": 0}
    out: dict[str, Any] = {}
    for src, dst in (
        ("address", "address"),
        ("offset", "address"),
        ("width", "width"),
        ("regwidth", "width"),
        ("reset", "reset"),
        ("reset_value", "reset"),
        ("name", "name"),
        ("path", "path"),
        ("description", "description"),
        ("fields", "fields"),
    ):
        if hasattr(register_class, src) and dst not in out:
            out[dst] = getattr(register_class, src)
    out.setdefault("address", 0)
    out.setdefault("width", 32)
    out.setdefault("reset", 0)
    return out


def build(register_class: Any = None, /, **field_values: Any) -> RegisterValue:  # noqa: ANN401
    """Module-level alias for :meth:`RegisterValue.build`."""
    return RegisterValue.build(register_class, **field_values)
