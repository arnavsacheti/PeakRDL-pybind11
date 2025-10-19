"""Backend entry point implementation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:  # pragma: no cover - optional dependency during tests
    from peakrdl.plugins.exporter import ExporterBackend  # type: ignore
except Exception:  # pragma: no cover - fallback for PeakRDL versions without ExporterBackend
    class ExporterBackend:  # type: ignore[misc]
        """Fallback backend base class."""

        def __init__(self) -> None:  # noqa: D401 - simple stub
            """Initialise the fallback backend."""

        def get_additional_args(self, parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
            return parser

        def run(self, top_node, options):  # pragma: no cover - interface stub
            raise NotImplementedError


from .ir import IRBuilder
from .render import TemplateRenderer


@dataclass
class PyBindOptions:
    """Options that control code generation."""

    soc_name: str
    namespace: Optional[str]
    out_dir: Path
    word_bytes: int = 4
    little_endian: bool = True
    gen_pyi: bool = False
    with_examples: bool = False
    access_checks: bool = True
    emit_reset_writes: bool = False

    def module_name(self) -> str:
        base = self.namespace or f"soc_{self.soc_name}"
        return base


class PyBindBackend(ExporterBackend):
    """PeakRDL backend that emits a PyBind11 extension."""

    short_description = "Generate a PyBind11 register access module"

    def __init__(self) -> None:
        super().__init__()  # type: ignore[misc]
        self.renderer = TemplateRenderer()

    # ------------------------------------------------------------------
    # PeakRDL integration helpers
    # ------------------------------------------------------------------
    def get_additional_args(self, parser: argparse.ArgumentParser) -> argparse.ArgumentParser:  # pragma: no cover - exercised via CLI
        parser.add_argument("--soc-name", required=True, help="Name of the generated SoC package")
        parser.add_argument("--out", dest="out_dir", required=True, help="Output directory")
        parser.add_argument("--namespace", dest="namespace", help="Python package namespace")
        parser.add_argument("--word-bytes", dest="word_bytes", type=int, default=4, help="Bus word size in bytes")
        parser.add_argument("--little-endian", dest="little_endian", action="store_true", default=True)
        parser.add_argument("--big-endian", dest="little_endian", action="store_false")
        parser.add_argument("--gen-pyi", dest="gen_pyi", action="store_true", default=False)
        parser.add_argument("--with-examples", dest="with_examples", action="store_true", default=False)
        parser.add_argument("--no-access-checks", dest="access_checks", action="store_false", default=True)
        parser.add_argument("--emit-reset-writes", dest="emit_reset_writes", action="store_true", default=False)
        return parser

    def _options_from_namespace(self, namespace: argparse.Namespace) -> PyBindOptions:
        return PyBindOptions(
            soc_name=namespace.soc_name,
            namespace=getattr(namespace, "namespace", None),
            out_dir=Path(namespace.out_dir),
            word_bytes=int(getattr(namespace, "word_bytes", 4)),
            little_endian=bool(getattr(namespace, "little_endian", True)),
            gen_pyi=bool(getattr(namespace, "gen_pyi", False)),
            with_examples=bool(getattr(namespace, "with_examples", False)),
            access_checks=bool(getattr(namespace, "access_checks", True)),
            emit_reset_writes=bool(getattr(namespace, "emit_reset_writes", False)),
        )

    # ------------------------------------------------------------------
    # Entry point used by both CLI and PeakRDL
    # ------------------------------------------------------------------
    def run(self, top_node, options: PyBindOptions | argparse.Namespace) -> None:
        if isinstance(options, argparse.Namespace):
            options = self._options_from_namespace(options)

        out_dir = options.out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        module_name = options.module_name()
        cpp_namespace = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in module_name)

        builder = IRBuilder(
            word_bytes=options.word_bytes,
            little_endian=options.little_endian,
            access_checks=options.access_checks,
            emit_reset_writes=options.emit_reset_writes,
        )
        soc_ir = builder.build(top_node, soc_name=options.soc_name, namespace=module_name)

        all_registers = list(soc_ir.top.flatten_registers())

        top_descriptor = "{}".format(
            "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in soc_ir.top.path.replace(".", "_"))) + "_desc"

        context = {
            "soc": soc_ir,
            "module_name": module_name,
            "namespace": cpp_namespace,
            "word_bytes": options.word_bytes,
            "little_endian": options.little_endian,
            "access_checks": options.access_checks,
            "emit_reset_writes": options.emit_reset_writes,
            "registers": all_registers,
            "top_descriptor": top_descriptor,
        }

        # Core C++ sources
        self.renderer.render_to_path("cpp/master.hpp.jinja", context, out_dir / "cpp" / "master.hpp")
        self.renderer.render_to_path("cpp/master.cpp.jinja", context, out_dir / "cpp" / "master.cpp")
        self.renderer.render_to_path("cpp/accessors.hpp.jinja", context, out_dir / "cpp" / "accessors.hpp")
        self.renderer.render_to_path("cpp/accessors.cpp.jinja", context, out_dir / "cpp" / "accessors.cpp")
        self.renderer.render_to_path("cpp/reg_model.hpp.jinja", context, out_dir / "cpp" / "reg_model.hpp")
        self.renderer.render_to_path("cpp/reg_model.cpp.jinja", context, out_dir / "cpp" / "reg_model.cpp")
        self.renderer.render_to_path("cpp/soc_module.cpp.jinja", context, out_dir / "cpp" / "soc_module.cpp")

        # Build system files
        self.renderer.render_to_path("CMakeLists.txt.jinja", context, out_dir / "CMakeLists.txt")
        self.renderer.render_to_path("pyproject.toml.jinja", context, out_dir / "pyproject.toml")

        if options.gen_pyi:
            self.renderer.render_to_path("typing/soc.pyi.jinja", context, out_dir / "typing" / f"{module_name}.pyi")

        if options.with_examples:
            self.renderer.render_to_path("masters/openocd_master.cpp.jinja", context, out_dir / "masters" / "openocd_master.cpp")
            self.renderer.render_to_path("masters/ssh_devmem_master.cpp.jinja", context, out_dir / "masters" / "ssh_devmem_master.cpp")


__all__ = ["PyBindBackend", "PyBindOptions"]
