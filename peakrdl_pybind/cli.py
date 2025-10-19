"""CLI entry point for peakrdl-pybind."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import click

from .backend import BackendOptions, IRBuilder, PyBindBackend

try:  # pragma: no cover - optional import when available
    from systemrdl.compiler import Compiler
except Exception:  # pragma: no cover - allow operation without SystemRDL
    Compiler = None  # type: ignore


def _load_rdl_sources(sources: Iterable[Path], includes: Iterable[Path]) -> Any:
    if Compiler is None:
        raise RuntimeError("systemrdl-compiler is required to parse RDL files")
    compiler = Compiler()
    for inc in includes:
        compiler.add_search_path(str(inc))
    for source in sources:
        compiler.compile_file(str(source))
    return compiler.elaborate()


@click.command()
@click.option("--soc-name", "soc_name", required=True, help="Name of the generated SoC package")
@click.option("--out", "output", type=click.Path(path_type=Path), required=True, help="Output directory")
@click.option("--top", "top", required=True, help="Top-level addrmap symbol")
@click.option("--rdl", "rdl", type=click.Path(path_type=Path), multiple=True, help="SystemRDL source file")
@click.option("--include", "include", type=click.Path(path_type=Path), multiple=True, help="Include search path")
@click.option("--namespace", type=str, help="Python import namespace")
@click.option("--word-bytes", type=int, default=4, show_default=True)
@click.option("--little-endian/--big-endian", default=True, show_default=True)
@click.option("--with-examples/--no-examples", default=False, show_default=True)
@click.option("--gen-pyi/--no-gen-pyi", default=False, show_default=True)
@click.option("--emit-reset-writes/--no-emit-reset-writes", default=False, show_default=True)
@click.option("--no-access-checks/--access-checks", default=False, show_default=True)
@click.option("--dump-ir", is_flag=True, help="Print the intermediate representation as JSON")
@click.version_option(package_name="peakrdl-pybind")
def main(
    soc_name: str,
    output: Path,
    top: str,
    rdl: Iterable[Path],
    include: Iterable[Path],
    namespace: Optional[str],
    word_bytes: int,
    little_endian: bool,
    with_examples: bool,
    gen_pyi: bool,
    emit_reset_writes: bool,
    no_access_checks: bool,
    dump_ir: bool,
) -> None:
    """Command line interface for the backend."""

    options = BackendOptions(
        soc_name=soc_name,
        output=output,
        top=top,
        namespace=namespace,
        word_bytes=word_bytes,
        little_endian=little_endian,
        with_examples=with_examples,
        generate_pyi=gen_pyi,
        emit_reset_writes=emit_reset_writes,
        no_access_checks=no_access_checks,
    )
    output.mkdir(parents=True, exist_ok=True)

    backend = PyBindBackend()

    if rdl:
        top_node = _load_rdl_sources(rdl, include)
    else:
        raise click.UsageError("At least one --rdl file must be specified")

    builder = IRBuilder(options)
    ir = builder.build(top_node)
    if dump_ir:
        json.dump(_serialise_ir(ir), sys.stdout, indent=2)
        sys.stdout.write("\n")

    backend._render_sources(ir, options)  # type: ignore[attr-defined]
    click.echo(f"Generated PyBind11 module sources at {output}")


def _serialise_ir(block: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "name": block.name,
        "path": block.path,
        "address": block.address,
        "doc": block.doc,
    }
    if block.array:
        data["array"] = {"dimensions": block.array.dimensions, "stride": block.array.stride}
    if block.registers:
        data["registers"] = [
            {
                "name": reg.name,
                "path": reg.path,
                "address": reg.address,
                "width": reg.width,
                "reset": reg.reset,
                "volatile": reg.volatile,
                "access": reg.access,
                "doc": reg.doc,
                "fields": [
                    {
                        "name": field.name,
                        "lsb": field.lsb,
                        "msb": field.msb,
                        "access": field.access,
                        "reset": field.reset,
                        "doc": field.description,
                        "enum": field.enum,
                    }
                    for field in reg.fields
                ],
            }
            for reg in block.registers
        ]
    if block.blocks:
        data["blocks"] = [_serialise_ir(child) for child in block.blocks]
    return data


if __name__ == "__main__":  # pragma: no cover
    main()
