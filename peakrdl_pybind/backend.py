"""PeakRDL backend implementation for generating a PyBind11 module."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from packaging.utils import canonicalize_name

try:  # pragma: no cover - optional dependency used at runtime
    from systemrdl.compiler import RDLCompiler  # type: ignore
    from systemrdl.node import AddrmapNode  # type: ignore
except Exception:  # pragma: no cover - used only when systemrdl is not installed
    RDLCompiler = None  # type: ignore
    AddrmapNode = Any  # type: ignore

from .ir import AccessMode, BlockIR, FieldIR, RegisterIR, SoCIR, ArrayIR
from .render import render_soc


@dataclass
class BackendOptions:
    soc_name: str
    namespace: Optional[str]
    output: Path
    top: Optional[str]
    word_bytes: int = 4
    little_endian: bool = True
    include_examples: bool = False
    generate_pyi: bool = False
    emit_reset_writes: bool = True
    no_access_checks: bool = False

    @property
    def module_name(self) -> str:
        if self.namespace:
            return self.namespace
        return f"soc_{canonicalize_name(self.soc_name).replace('-', '_')}"


class PyBindBackend:
    """Backend entry point used by PeakRDL."""

    def __init__(self, options: Optional[BackendOptions] = None) -> None:
        self.options = options

    # ------------------------------------------------------------------
    # PeakRDL backend interface
    # ------------------------------------------------------------------
    def get_export_options(self) -> Dict[str, Any]:  # pragma: no cover - API shim
        return {
            "soc_name": {
                "type": str,
                "desc": "Name of the SoC/package."
            },
            "namespace": {
                "type": str,
                "desc": "Python package name to emit (defaults to soc_<soc_name>)."
            },
            "word_bytes": {
                "type": int,
                "default": 4,
                "desc": "Bus word size in bytes."
            },
            "little_endian": {
                "type": bool,
                "default": True,
                "desc": "Emit little endian helpers."
            },
            "with_examples": {
                "type": bool,
                "default": False,
                "desc": "Generate example master implementations."
            },
            "gen_pyi": {
                "type": bool,
                "default": False,
                "desc": "Emit typing stubs alongside the extension."
            },
            "emit_reset_writes": {
                "type": bool,
                "default": True,
                "desc": "Generate reset_to_defaults helper."
            },
            "no_access_checks": {
                "type": bool,
                "default": False,
                "desc": "Disable access policy enforcement in generated code."
            },
        }

    # Public API -------------------------------------------------------
    def export(self, node: Any, output_dir: Path, **kwargs: Any) -> None:
        """Entry point invoked by the PeakRDL compiler."""

        merged = dict(kwargs)
        merged.setdefault("output", output_dir)
        opts = self._merge_options(merged)
        builder = ModelBuilder(opts)
        top_block = builder.build_block(node, path="top")
        soc = SoCIR(
            module_name=opts.module_name,
            namespace=opts.module_name,
            word_bytes=opts.word_bytes,
            little_endian=opts.little_endian,
            top=top_block,
            generate_pyi=opts.generate_pyi,
            include_examples=opts.include_examples,
            options={
                "emit_reset_writes": opts.emit_reset_writes,
                "no_access_checks": opts.no_access_checks,
            },
        )
        render_soc(soc, output_dir)
        (output_dir / "soc.json").write_text(json.dumps(_serialize_soc(soc), indent=2), encoding="utf-8")

    # CLI helper -------------------------------------------------------
    def run_cli(self, *, rdl_files: Sequence[str], out: Path, soc_name: str, top: str, **kwargs: Any) -> None:
        if RDLCompiler is None:  # pragma: no cover - runtime guard
            raise RuntimeError("systemrdl-compiler is required for the CLI")

        compiler = RDLCompiler()
        for path in rdl_files:
            compiler.compile_file(str(path))
        root = compiler.elaborate(top)
        options = self._merge_options({
            "soc_name": soc_name,
            "top": top,
            "namespace": kwargs.get("namespace"),
            "word_bytes": kwargs.get("word_bytes", 4),
            "little_endian": kwargs.get("little_endian", True),
            "with_examples": kwargs.get("with_examples", False),
            "gen_pyi": kwargs.get("gen_pyi", False),
            "emit_reset_writes": kwargs.get("emit_reset_writes", True),
            "no_access_checks": kwargs.get("no_access_checks", False),
        })
        self.export(root, out, **options.__dict__)

    # Internal helpers -------------------------------------------------
    def _merge_options(self, kwargs: Dict[str, Any]) -> BackendOptions:
        if self.options:
            base = self.options.__dict__.copy()
            base.update(kwargs)
            return BackendOptions(**base)
        if "soc_name" not in kwargs:
            raise ValueError("'soc_name' option is required")
        output = kwargs.get("output") or kwargs.get("out")
        if output is None:
            output = Path.cwd()
        else:
            output = Path(output)
        return BackendOptions(
            soc_name=kwargs["soc_name"],
            namespace=kwargs.get("namespace"),
            output=output,
            top=kwargs.get("top"),
            word_bytes=int(kwargs.get("word_bytes", 4)),
            little_endian=bool(kwargs.get("little_endian", True)),
            include_examples=bool(kwargs.get("with_examples", False)),
            generate_pyi=bool(kwargs.get("gen_pyi", False)),
            emit_reset_writes=bool(kwargs.get("emit_reset_writes", True)),
            no_access_checks=bool(kwargs.get("no_access_checks", False)),
        )


class ModelBuilder:
    """Turns elaborated SystemRDL nodes into the internal representation."""

    def __init__(self, options: BackendOptions) -> None:
        self.options = options

    def build_block(self, node: Any, path: str) -> BlockIR:
        base_address = self._absolute_address(node)
        description = self._description(node)
        block = BlockIR(name=self._inst_name(node), path=path, base_address=base_address, description=description)

        for child in self._iter_children(node):
            type_name = self._type_name(child)
            if type_name == "reg":
                block.registers.append(self._build_register(child, block))
            elif type_name in {"addrmap", "regfile"}:
                if self._is_array(child):
                    array = self._build_array(child, parent=block)
                    block.arrays.append(array)
                else:
                    sub_path = f"{path}.{self._inst_name(child)}"
                    block.blocks.append(self.build_block(child, sub_path))

        return block

    # ------------------------------------------------------------------
    def _build_array(self, node: Any, parent: BlockIR) -> ArrayIR:
        element_path = f"{parent.path}.{self._inst_name(node)}[elem]"
        element = self.build_block(node, element_path)
        count = self._array_count(node)
        stride = self._array_stride(node)
        return ArrayIR(name=self._inst_name(node), element=element, count=count, stride=stride)

    def _build_register(self, node: Any, block: BlockIR) -> RegisterIR:
        name = self._inst_name(node)
        path = f"{block.path}.{name}"
        address = self._absolute_address(node)
        width = self._width(node)
        reset = self._reset(node)
        access = AccessMode.from_string(self._access(node))
        is_volatile = bool(self._get_property(node, "volatile") or False)
        description = self._description(node)
        offset = address - block.base_address
        reg = RegisterIR(
            name=name,
            path=path,
            address=address,
            offset=offset,
            width=width,
            reset=reset,
            access=access,
            is_volatile=is_volatile,
            description=description,
        )
        for field in self._iter_fields(node):
            reg.fields.append(self._build_field(field, reg))
        return reg

    def _build_field(self, field: Any, reg: RegisterIR) -> FieldIR:
        name = self._inst_name(field)
        lsb = int(getattr(field, "lsb", getattr(field, "low", 0)))
        msb = int(getattr(field, "msb", getattr(field, "high", lsb)))
        access = AccessMode.from_string(self._access(field))
        reset = self._reset(field)
        description = self._description(field)
        return FieldIR(name=name, lsb=lsb, msb=msb, access=access, reset=reset, description=description)

    # ------------------------------------------------------------------
    def _iter_children(self, node: Any) -> Iterable[Any]:
        children_attr = getattr(node, "children", None)
        if callable(children_attr):
            try:
                for child in children_attr():
                    yield child
                    return
            except TypeError:
                try:
                    for child in children_attr(1):
                        yield child
                        return
                except TypeError:
                    pass
        if isinstance(children_attr, Iterable):
            for child in children_attr:
                yield child
        # Fall back to getattr style APIs used in tests
        manual_children = getattr(node, "_children", None)
        if manual_children:
            for child in manual_children:
                yield child

    def _iter_fields(self, node: Any) -> Iterable[Any]:
        field_attr = getattr(node, "fields", None)
        if callable(field_attr):
            yield from field_attr()
            return
        if isinstance(field_attr, Iterable):
            for field in field_attr:
                yield field
            return
        manual_fields = getattr(node, "_fields", None)
        if manual_fields:
            for field in manual_fields:
                yield field

    # ------------------------------------------------------------------
    def _inst_name(self, node: Any) -> str:
        name = getattr(node, "inst_name", None) or getattr(node, "name", None)
        if name is None:
            return "anonymous"
        return str(name)

    def _type_name(self, node: Any) -> str:
        name = getattr(node, "type_name", None)
        if name:
            return str(name)
        cls_name = node.__class__.__name__.lower()
        if "regfile" in cls_name:
            return "regfile"
        if "addrmap" in cls_name or "block" in cls_name:
            return "addrmap"
        if "reg" in cls_name:
            return "reg"
        return cls_name

    def _absolute_address(self, node: Any) -> int:
        if hasattr(node, "absolute_address"):
            value = getattr(node, "absolute_address")
            if callable(value):
                value = value()
            if value is not None:
                return int(value)
        if hasattr(node, "get_absolute_address"):
            value = node.get_absolute_address()
            if value is not None:
                return int(value)
        return int(getattr(node, "address", 0))

    def _width(self, node: Any) -> int:
        width = getattr(node, "width", None)
        if width is not None:
            return int(width)
        width_prop = self._get_property(node, "width")
        if width_prop is not None:
            return int(width_prop)
        return int(getattr(node, "size", self.options.word_bytes * 8))

    def _reset(self, node: Any) -> Optional[int]:
        reset = self._get_property(node, "reset")
        if isinstance(reset, Iterable) and not isinstance(reset, (str, bytes)):
            reset_list = list(reset)
            if reset_list:
                reset = reset_list[0]
        if hasattr(reset, "value"):
            reset = reset.value
        if reset is None:
            return None
        try:
            return int(reset)
        except (ValueError, TypeError):
            return None

    def _access(self, node: Any) -> str:
        prop = self._get_property(node, "sw")
        if prop is None:
            prop = self._get_property(node, "access")
        if hasattr(prop, "name"):
            return str(prop.name)
        if prop is None:
            return "rw"
        return str(prop)

    def _description(self, node: Any) -> Optional[str]:
        desc = self._get_property(node, "desc")
        if desc is None:
            desc = getattr(node, "description", None)
        return str(desc) if desc is not None else None

    def _get_property(self, node: Any, name: str) -> Any:
        getter = getattr(node, "get_property", None)
        if callable(getter):
            try:
                return getter(name)
            except Exception:  # pragma: no cover - defensive fallback
                return None
        return getattr(node, name, None)

    def _is_array(self, node: Any) -> bool:
        return bool(getattr(node, "is_array", False))

    def _array_count(self, node: Any) -> int:
        dims = getattr(node, "array_dimensions", None)
        if not dims:
            return int(getattr(node, "array_size", 1))
        total = 1
        for dim in dims:
            total *= int(dim)
        return total

    def _array_stride(self, node: Any) -> int:
        stride = getattr(node, "array_stride", None)
        if stride is None:
            stride = getattr(node, "stride", self.options.word_bytes)
        return int(stride)


def _serialize_soc(soc: SoCIR) -> Dict[str, Any]:
    def serialize_block(block: BlockIR) -> Dict[str, Any]:
        return {
            "name": block.name,
            "path": block.path,
            "base_address": block.base_address,
            "registers": [serialize_reg(reg) for reg in block.registers],
            "blocks": [serialize_block(child) for child in block.blocks],
            "arrays": [
                {
                    "name": array.name,
                    "count": array.count,
                    "stride": array.stride,
                    "element": serialize_block(array.element),
                }
                for array in block.arrays
            ],
        }

    def serialize_reg(reg: RegisterIR) -> Dict[str, Any]:
        return {
            "name": reg.name,
            "path": reg.path,
            "address": reg.address,
            "width": reg.width,
            "reset": reg.reset,
            "access": reg.access.value,
            "fields": [
                {
                    "name": field.name,
                    "lsb": field.lsb,
                    "msb": field.msb,
                    "access": field.access.value,
                    "reset": field.reset,
                }
                for field in reg.fields
            ],
        }

    return {
        "module_name": soc.module_name,
        "namespace": soc.namespace,
        "word_bytes": soc.word_bytes,
        "little_endian": soc.little_endian,
        "top": serialize_block(soc.top),
    }
