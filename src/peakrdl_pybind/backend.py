"""Backend implementation for generating PyBind11-based register models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined

if TYPE_CHECKING:  # pragma: no cover - imported for type checking only
    from systemrdl.node import AddrmapNode, FieldNode, Node
else:  # pragma: no cover - runtime fallback when SystemRDL is unavailable
    AddrmapNode = Any  # type: ignore[assignment]
    FieldNode = Any  # type: ignore[assignment]
    Node = Any  # type: ignore[assignment]


@dataclass
class FieldInfo:
    """Description of a single field extracted from the SystemRDL AST."""

    name: str
    lsb: int
    msb: int
    access: str
    reset: Optional[int]


@dataclass
class RegisterInfo:
    """Description of a register and its fields."""

    name: str
    path: str
    absolute_address: int
    width: int
    reset: Optional[int]
    access: str
    volatile: bool
    fields: List[FieldInfo] = field(default_factory=list)


@dataclass
class BackendOptions:
    """Normalized backend options passed from the CLI/backend API."""

    soc_name: str
    output: Path
    word_bytes: int = 4
    little_endian: bool = True
    generate_examples: bool = False
    generate_pyi: bool = False
    namespace: Optional[str] = None
    no_access_checks: bool = False
    emit_reset_writes: bool = False


class PyBindBackend:
    """PeakRDL backend that renders a PyBind11-based register model."""

    short_desc = "Generate a PyBind11 backed Python module"

    def __init__(self) -> None:
        templates_path = Path(__file__).parent / "templates"
        self._environment = Environment(
            loader=FileSystemLoader(str(templates_path)),
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )

    # -- Backend API -----------------------------------------------------
    def build(self, node: AddrmapNode, output_dir: str, **kwargs: object) -> None:
        """Entry point used by the SystemRDL compiler."""

        options = self._parse_options(output_dir, **kwargs)
        registers = list(self._collect_registers(node))
        context = {
            "options": options,
            "registers": registers,
        }
        self._render_templates(options.output, context)

    # -- Internal helpers ------------------------------------------------
    def _parse_options(self, output_dir: str, **kwargs: object) -> BackendOptions:
        if "soc_name" not in kwargs:
            raise ValueError("soc_name option is required")
        namespace = kwargs.get("namespace")
        options = BackendOptions(
            soc_name=str(kwargs["soc_name"]),
            output=Path(output_dir),
            word_bytes=int(kwargs.get("word_bytes", 4)),
            little_endian=bool(kwargs.get("little_endian", True)),
            generate_examples=bool(kwargs.get("with_examples", False)),
            generate_pyi=bool(kwargs.get("gen_pyi", False)),
            namespace=str(namespace) if namespace else None,
            no_access_checks=bool(kwargs.get("no_access_checks", False)),
            emit_reset_writes=bool(kwargs.get("emit_reset_writes", False)),
        )
        options.output.mkdir(parents=True, exist_ok=True)
        return options

    def _collect_registers(self, node: Node) -> Iterable[RegisterInfo]:
        if getattr(node, "is_reg", False):
            yield self._build_register(node)
        for child in node.children():
            yield from self._collect_registers(child)

    def _build_register(self, node: Node) -> RegisterInfo:
        fields = [self._build_field(field) for field in node.fields()]
        return RegisterInfo(
            name=node.inst_name,
            path=node.get_path(),
            absolute_address=node.absolute_address,
            width=node.get_property("regwidth"),
            reset=node.get_property("reset") if node.has_property("reset") else None,
            access=node.get_property("sw"),
            volatile=bool(node.get_property("volatile"))
            if node.has_property("volatile")
            else False,
            fields=fields,
        )

    def _build_field(self, node: FieldNode) -> FieldInfo:
        lsb = node.get_property("lsb")
        msb = node.get_property("msb")
        access = node.get_property("sw")
        reset = node.get_property("reset") if node.has_property("reset") else None
        return FieldInfo(name=node.inst_name, lsb=lsb, msb=msb, access=access, reset=reset)

    def _render_templates(self, output_dir: Path, context: Dict[str, object]) -> None:
        for template_name in self._environment.list_templates():
            template = self._environment.get_template(template_name)
            rendered = template.render(**context)
            suffix = ".jinja"
            target_name = (
                template_name[:- len(suffix)] if template_name.endswith(suffix) else template_name
            )
            target_path = output_dir / target_name
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(rendered)

