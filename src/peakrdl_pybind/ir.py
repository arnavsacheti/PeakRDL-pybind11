"""Intermediate representation helpers for the PyBind backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence


@dataclass
class EnumIR:
    """Enumerated value for a field."""

    name: str
    value: int
    description: Optional[str] = None


@dataclass
class FieldIR:
    """Description of a register field."""

    name: str
    lsb: int
    msb: int
    access: str
    reset: Optional[int]
    description: Optional[str] = None
    enums: List[EnumIR] = field(default_factory=list)

    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1

    @property
    def mask(self) -> int:
        return ((1 << self.width) - 1) << self.lsb


@dataclass
class RegisterIR:
    """Description of a register."""

    name: str
    path: str
    absolute_address: int
    width: int
    reset: Optional[int]
    volatile: bool
    access: str
    description: Optional[str] = None
    fields: List[FieldIR] = field(default_factory=list)
    array_dimensions: Sequence[int] = field(default_factory=tuple)
    stride: Optional[int] = None

    @property
    def is_array(self) -> bool:
        return bool(self.array_dimensions)

    @property
    def element_count(self) -> int:
        count = 1
        for dim in self.array_dimensions:
            count *= dim
        return count


@dataclass
class BlockIR:
    """Address space/regfile/block description."""

    name: str
    path: str
    absolute_address: int
    description: Optional[str] = None
    registers: List[RegisterIR] = field(default_factory=list)
    blocks: List["BlockIR"] = field(default_factory=list)
    array_dimensions: Sequence[int] = field(default_factory=tuple)
    stride: Optional[int] = None

    def flatten_registers(self) -> Iterable[RegisterIR]:
        yield from self.registers
        for block in self.blocks:
            yield from block.flatten_registers()

    @property
    def is_array(self) -> bool:
        return bool(self.array_dimensions)


@dataclass
class SocIR:
    """Root object representing the generated SoC."""

    soc_name: str
    namespace: str
    top: BlockIR
    word_bytes: int
    little_endian: bool
    access_checks: bool = True
    emit_reset_writes: bool = False


class IRBuilder:
    """Builds the IR from a SystemRDL elaborated design."""

    def __init__(
        self,
        *,
        word_bytes: int = 4,
        little_endian: bool = True,
        access_checks: bool = True,
        emit_reset_writes: bool = False,
    ) -> None:
        self.word_bytes = word_bytes
        self.little_endian = little_endian
        self.access_checks = access_checks
        self.emit_reset_writes = emit_reset_writes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def build(self, top_node, *, soc_name: str, namespace: str) -> SocIR:
        block = self._collect_block(top_node, parent_path=namespace)
        return SocIR(
            soc_name=soc_name,
            namespace=namespace,
            top=block,
            word_bytes=self.word_bytes,
            little_endian=self.little_endian,
            access_checks=self.access_checks,
            emit_reset_writes=self.emit_reset_writes,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _collect_block(self, node, *, parent_path: str) -> BlockIR:
        name = self._get_inst_name(node)
        path = f"{parent_path}.{name}" if parent_path else name
        desc = self._get_property(node, "desc")
        abs_addr = self._get_absolute_address(node)
        array_dims = tuple(self._get_array_dimensions(node))
        stride = self._get_stride(node)

        registers: List[RegisterIR] = []
        blocks: List[BlockIR] = []

        for child in self._iter_children(node):
            kind = self._classify_node(child)
            if kind == "reg":
                registers.append(self._collect_register(child, parent_path=f"{path}.{self._get_inst_name(child)}"))
            elif kind in {"addrmap", "regfile", "block"}:
                blocks.append(self._collect_block(child, parent_path=path))

        registers.sort(key=lambda r: (r.absolute_address, r.name))
        blocks.sort(key=lambda b: (b.absolute_address, b.name))

        return BlockIR(
            name=name,
            path=path,
            absolute_address=abs_addr,
            description=desc,
            registers=registers,
            blocks=blocks,
            array_dimensions=array_dims,
            stride=stride,
        )

    def _collect_register(self, node, *, parent_path: str) -> RegisterIR:
        name = self._get_inst_name(node)
        path = parent_path
        abs_addr = self._get_absolute_address(node)
        width = self._get_bit_width(node)
        reset = self._get_reset(node)
        volatile = bool(self._get_property(node, "volatile") or False)
        access = self._stringify_access(self._get_property(node, "sw"))
        desc = self._get_property(node, "desc")
        array_dims = tuple(self._get_array_dimensions(node))
        stride = self._get_stride(node)
        if stride is None and array_dims:
            stride = max(self.word_bytes, (width + 7) // 8)

        fields: List[FieldIR] = []
        for field_node in self._iter_fields(node):
            fields.append(self._collect_field(field_node, parent_path=f"{path}.{self._get_inst_name(field_node)}"))

        fields.sort(key=lambda f: (f.lsb, f.name))

        return RegisterIR(
            name=name,
            path=path,
            absolute_address=abs_addr,
            width=width,
            reset=reset,
            volatile=volatile,
            access=access,
            description=desc,
            fields=fields,
            array_dimensions=array_dims,
            stride=stride,
        )

    def _collect_field(self, node, *, parent_path: str) -> FieldIR:
        name = self._get_inst_name(node)
        desc = self._get_property(node, "desc")
        lsb = getattr(node, "lsb", 0)
        msb = getattr(node, "msb", lsb)
        reset_val = self._get_reset(node)
        access = self._stringify_access(self._get_property(node, "sw"))
        enums = [
            EnumIR(name=enum_name, value=enum_value, description=enum_desc)
            for enum_name, enum_value, enum_desc in self._iter_enums(node)
        ]

        return FieldIR(
            name=name,
            lsb=lsb,
            msb=msb,
            access=access,
            reset=reset_val,
            description=desc,
            enums=enums,
        )

    # ------------------------------------------------------------------
    # Introspection utilities
    # ------------------------------------------------------------------
    def _classify_node(self, node) -> str:
        type_name = getattr(node, "type_name", None) or node.__class__.__name__
        type_name = type_name.lower()
        if "field" in type_name:
            return "field"
        if "regfile" in type_name:
            return "regfile"
        if "addrmap" in type_name:
            return "addrmap"
        if type_name in {"reg", "register", "registernode"}:
            return "reg"
        if "mem" in type_name:
            return "mem"
        if "signal" in type_name:
            return "signal"
        return type_name

    def _iter_children(self, node) -> Iterable:
        children: Iterable = []
        if hasattr(node, "children") and callable(getattr(node, "children")):
            children = list(node.children())
        elif hasattr(node, "children"):
            children = getattr(node, "children")  # type: ignore[assignment]
        elif hasattr(node, "inst") and hasattr(node.inst, "children"):
            children = list(node.inst.children())
        return [child for child in children if self._classify_node(child) != "field"]

    def _iter_fields(self, node) -> Iterable:
        if hasattr(node, "fields") and callable(getattr(node, "fields")):
            return list(node.fields())
        if hasattr(node, "field_children"):
            return list(getattr(node, "field_children"))
        children = []
        if hasattr(node, "children") and callable(getattr(node, "children")):
            children = list(node.children())
        return [child for child in children if self._classify_node(child) == "field"]

    def _iter_enums(self, node) -> Iterable:
        enums = []
        enum_dict = getattr(node, "enumerated_values", None)
        if isinstance(enum_dict, dict):
            for name, value in enum_dict.items():
                enums.append((name, int(value), None))
        elif hasattr(node, "enum_definition") and node.enum_definition is not None:
            enum_def = node.enum_definition
            items = getattr(enum_def, "items", None)
            if items:
                for enum_item in items:
                    enums.append((enum_item.name, int(enum_item.value), getattr(enum_item, "description", None)))
        return enums

    def _get_inst_name(self, node) -> str:
        return getattr(node, "inst_name", getattr(node, "name", "unnamed"))

    def _get_absolute_address(self, node) -> int:
        for attr in ("absolute_address", "addr", "absolute_address_offset"):
            if hasattr(node, attr):
                value = getattr(node, attr)
                if value is not None:
                    return int(value)
        getter = getattr(node, "get_absolute_address", None)
        if callable(getter):
            return int(getter())
        getter = getattr(node, "get_address", None)
        if callable(getter):
            return int(getter())
        offset = getattr(node, "address_offset", 0)
        if hasattr(node, "parent") and node.parent is not None:
            return self._get_absolute_address(node.parent) + int(offset)
        return int(offset)

    def _get_bit_width(self, node) -> int:
        for attr in ("width", "bit_width", "total_bit_width"):
            if hasattr(node, attr):
                value = getattr(node, attr)
                if value is not None:
                    return int(value)
        getter = getattr(node, "get_property", None)
        if callable(getter):
            width_prop = getter("width")
            if width_prop is not None:
                try:
                    return int(width_prop)
                except TypeError:
                    pass
        return self.word_bytes * 8

    def _get_reset(self, node) -> Optional[int]:
        getter = getattr(node, "get_property", None)
        if callable(getter):
            try:
                reset_prop = getter("reset")
            except Exception:  # pragma: no cover - defensive fallback
                reset_prop = None
            if reset_prop is not None:
                if isinstance(reset_prop, dict):
                    value = reset_prop.get("value")
                    if value is not None:
                        return int(value)
                try:
                    return int(reset_prop)
                except (TypeError, ValueError):
                    pass
        return None

    def _get_property(self, node, prop: str):
        getter = getattr(node, "get_property", None)
        if callable(getter):
            try:
                return getter(prop)
            except Exception:  # pragma: no cover - property missing
                return None
        return getattr(node, prop, None)

    def _stringify_access(self, value) -> str:
        if value is None:
            return "rw"
        if isinstance(value, str):
            return value
        if hasattr(value, "name"):
            return str(value.name)
        return str(value)

    def _get_array_dimensions(self, node) -> Sequence[int]:
        dims = getattr(node, "array_dimensions", None)
        if dims:
            return [int(d) for d in dims]
        if getattr(node, "is_array", False):
            for attr in ("dimensions", "shape"):
                candidate = getattr(node, attr, None)
                if candidate:
                    return [int(d) for d in candidate]
        return []

    def _get_stride(self, node) -> Optional[int]:
        for attr in ("array_stride", "stride", "address_stride"):
            if hasattr(node, attr):
                value = getattr(node, attr)
                if value is not None:
                    return int(value)
        getter = getattr(node, "get_address_stride", None)
        if callable(getter):
            stride = getter()
            if stride is not None:
                return int(stride)
        return None


__all__ = [
    "EnumIR",
    "FieldIR",
    "RegisterIR",
    "BlockIR",
    "SocIR",
    "IRBuilder",
]
