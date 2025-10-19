"""Core implementation of the PeakRDL PyBind11 backend."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from jinja2 import Environment, FileSystemLoader, PackageLoader, select_autoescape

try:  # pragma: no cover - optional import when running inside SystemRDL
    from systemrdl.compiler import RDLCompiler
    from systemrdl.compiler import Backend as RDLBackend
    from systemrdl.node import AddrmapNode, FieldNode, NodeVisitor, RegNode
except Exception:  # pragma: no cover - allow running unit tests without systemrdl
    RDLCompiler = Any  # type: ignore

    class RDLBackend:  # type: ignore
        """Fallback shim to allow unit testing without SystemRDL."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class NodeVisitor:  # type: ignore
        pass

    class AddrmapNode:  # type: ignore
        pass

    class RegNode:  # type: ignore
        pass

    class FieldNode:  # type: ignore
        pass


@dataclass
class FieldIR:
    """Represents a generated field."""

    name: str
    lsb: int
    msb: int
    access: str
    reset: Optional[int] = None
    description: str = ""
    enum: Dict[str, int] = field(default_factory=dict)

    @property
    def mask(self) -> int:
        width = self.msb - self.lsb + 1
        return ((1 << width) - 1) << self.lsb


@dataclass
class RegisterIR:
    """Represents a register and its metadata."""

    name: str
    path: str
    address: int
    width: int
    reset: Optional[int]
    volatile: bool
    access: str
    fields: List[FieldIR] = field(default_factory=list)
    doc: str = ""

    def sorted_fields(self) -> List[FieldIR]:
        return sorted(self.fields, key=lambda f: f.lsb)


@dataclass
class ArrayIR:
    """Array metadata for blocks or registers."""

    dimensions: List[int]
    stride: int


@dataclass
class BlockIR:
    """Represents a block (addrmap/regfile)."""

    name: str
    path: str
    address: int
    registers: List[RegisterIR] = field(default_factory=list)
    blocks: List["BlockIR"] = field(default_factory=list)
    array: Optional[ArrayIR] = None
    doc: str = ""

    def all_registers(self) -> Iterable[RegisterIR]:
        for reg in self.registers:
            yield reg
        for block in self.blocks:
            yield from block.all_registers()


@dataclass
class BackendOptions:
    """Configuration collected from CLI or PeakRDL invocation."""

    soc_name: str
    output: Path
    top: str
    namespace: Optional[str] = None
    word_bytes: int = 4
    little_endian: bool = True
    emit_reset_writes: bool = False
    generate_pyi: bool = False
    with_examples: bool = False
    no_access_checks: bool = False


class TemplateManager:
    """Wraps the Jinja2 environment and template rendering."""

    def __init__(self, template_dir: Optional[Path] = None) -> None:
        if template_dir is None:
            loader = PackageLoader("peakrdl_pybind", "templates")
        else:
            loader = FileSystemLoader(str(template_dir))
        self.env = Environment(loader=loader, autoescape=select_autoescape(enabled_extensions=("jinja",)))
        self.env.filters.setdefault("hex", lambda v: f"0x{int(v):X}")

    def render_to_file(self, template: str, destination: Path, **context: Any) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = self.env.get_template(template).render(**context)
        destination.write_text(data + "\n", encoding="utf8")


class IRBuilder(NodeVisitor):
    """Walks a SystemRDL design to produce a serialisable IR."""

    def __init__(self, options: BackendOptions) -> None:
        self.options = options

    def build(self, top: AddrmapNode) -> BlockIR:  # type: ignore[override]
        return self._build_block(top, parent_path=self.options.namespace or self.options.soc_name)

    def _build_block(self, node: Any, parent_path: str) -> BlockIR:
        name = getattr(node, "inst_name", getattr(node, "name", "")) or parent_path
        path = f"{parent_path}.{name}" if parent_path else name
        address = int(getattr(node, "absolute_address", getattr(node, "addr", 0)))
        block = BlockIR(name=name, path=path, address=address, doc=self._doc(node))

        # handle arrays
        array_dims = getattr(node, "array_dimensions", None)
        if array_dims:
            stride = int(getattr(node, "array_stride", getattr(node, "stride", self.options.word_bytes)))
            block.array = ArrayIR(dimensions=list(array_dims), stride=stride)

        for child in getattr(node, "children", lambda: [])():
            kind = getattr(child, "type_name", child.__class__.__name__).lower()
            if "reg" in kind and not kind.endswith("file"):
                block.registers.append(self._build_register(child, path))
            else:
                block.blocks.append(self._build_block(child, path))

        return block

    def _doc(self, node: Any) -> str:
        return getattr(node, "description", getattr(node, "desc", "")) or ""

    def _build_register(self, node: RegNode, parent_path: str) -> RegisterIR:  # type: ignore[override]
        name = getattr(node, "inst_name", getattr(node, "name", "reg"))
        path = f"{parent_path}.{name}" if parent_path else name
        address = int(getattr(node, "absolute_address", getattr(node, "addr", 0)))
        width = int(getattr(node, "total_width", getattr(node, "width", 32)))
        reset = getattr(node, "reset", None)
        volatile = bool(getattr(node, "volatile", getattr(node, "is_volatile", False)))
        sw_access = self._stringify_access(getattr(node, "sw_access", getattr(node, "access", "rw")))
        reg = RegisterIR(
            name=name,
            path=path,
            address=address,
            width=width,
            reset=reset,
            volatile=volatile,
            access=sw_access,
            doc=self._doc(node),
        )

        field_iter = getattr(node, "field_children", None)
        if callable(field_iter):
            fields = list(field_iter())
        else:
            fields = getattr(node, "fields", [])

        for field in fields:
            reg.fields.append(self._build_field(field))
        reg.fields.sort(key=lambda f: f.lsb)
        return reg

    def _build_field(self, node: FieldNode) -> FieldIR:  # type: ignore[override]
        name = getattr(node, "inst_name", getattr(node, "name", "field"))
        lsb = int(getattr(node, "lsb", getattr(node, "low", 0)))
        msb = int(getattr(node, "msb", getattr(node, "high", lsb)))
        access = self._stringify_access(getattr(node, "sw_access", getattr(node, "access", "rw")))
        reset = getattr(node, "reset", None)
        doc = self._doc(node)
        enum_values = self._extract_enum(node)
        return FieldIR(name=name, lsb=lsb, msb=msb, access=access, reset=reset, description=doc, enum=enum_values)

    def _extract_enum(self, node: Any) -> Dict[str, int]:
        enum = {}
        for name, value in getattr(node, "enum_dict", {}).items():
            try:
                enum[name] = int(value)
            except Exception:
                continue
        return enum

    def _stringify_access(self, value: Any) -> str:
        if value is None:
            return "rw"
        if isinstance(value, str):
            return value
        return str(value)


class PyBindBackend(RDLBackend):
    """PeakRDL backend that renders a PyBind11 extension module."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.template_manager = TemplateManager()

    def generate(self, top: AddrmapNode, options: Optional[Dict[str, Any]] = None) -> Path:  # type: ignore[override]
        """Entry point invoked by the SystemRDL compiler."""

        if options is None:
            raise ValueError("PyBindBackend requires options specifying output directory and soc name")

        backend_options = self._parse_options(options)
        builder = IRBuilder(backend_options)
        ir = builder.build(top)

        self._render_sources(ir, backend_options)
        return backend_options.output

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _parse_options(self, options: Dict[str, Any]) -> BackendOptions:
        output = Path(options.get("output", options.get("out", "build")))
        soc_name = options.get("soc_name") or options.get("soc")
        top = options.get("top")
        if not soc_name or not top:
            raise ValueError("Options must include 'soc_name' and 'top'")
        backend_options = BackendOptions(
            soc_name=soc_name,
            output=output,
            top=top,
            namespace=options.get("namespace"),
            word_bytes=int(options.get("word_bytes", 4)),
            little_endian=bool(options.get("little_endian", True)),
            emit_reset_writes=bool(options.get("emit_reset_writes", False)),
            generate_pyi=bool(options.get("gen_pyi", options.get("generate_pyi", False))),
            with_examples=bool(options.get("with_examples", False)),
            no_access_checks=bool(options.get("no_access_checks", False)),
        )
        backend_options.output.mkdir(parents=True, exist_ok=True)
        return backend_options

    def _render_sources(self, ir: BlockIR, options: BackendOptions) -> None:
        context = {
            "options": options,
            "ir": ir,
            "module_name": options.namespace or f"soc_{options.soc_name}",
            "registers": list(ir.all_registers()),
        }
        tm = self.template_manager
        out = options.output
        tm.render_to_file("master.hpp.jinja", out / "cpp" / "master.hpp", **context)
        tm.render_to_file("master.cpp.jinja", out / "cpp" / "master.cpp", **context)
        tm.render_to_file("accessors.hpp.jinja", out / "cpp" / "accessors.hpp", **context)
        tm.render_to_file("reg_model.hpp.jinja", out / "cpp" / "reg_model.hpp", **context)
        tm.render_to_file("reg_model.cpp.jinja", out / "cpp" / "reg_model.cpp", **context)
        tm.render_to_file("soc_module.cpp.jinja", out / "cpp" / "soc_module.cpp", **context)
        tm.render_to_file("CMakeLists.txt.jinja", out / "cpp" / "CMakeLists.txt", **context)
        tm.render_to_file("pyproject.toml.jinja", out / "pyproject.toml", **context)
        if options.generate_pyi:
            tm.render_to_file("soc.pyi.jinja", out / "typing" / f"{options.soc_name}.pyi", **context)
        if options.with_examples:
            tm.render_to_file("openocd_master.cpp.jinja", out / "cpp" / "masters" / "openocd_master.cpp", **context)
            tm.render_to_file("ssh_devmem_master.cpp.jinja", out / "cpp" / "masters" / "ssh_devmem_master.cpp", **context)


__all__ = ["PyBindBackend", "BackendOptions", "IRBuilder", "FieldIR", "RegisterIR", "BlockIR"]
