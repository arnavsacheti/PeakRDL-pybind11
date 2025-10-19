"""Command line interface for the PyBind backend."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
from systemrdl import RDLCompiler

from .backend import PyBindBackend, PyBindOptions


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("rdl", nargs=-1, type=click.Path(exists=True, dir_okay=False))
@click.option("--soc-name", required=True, help="Name of the SoC/package to generate")
@click.option("--top", "top_symbol", required=True, help="Top-level addrmap symbol to elaborate")
@click.option("--out", "out_dir", required=True, type=click.Path(file_okay=False), help="Output directory")
@click.option("--namespace", default=None, help="Python package namespace for the generated module")
@click.option("--word-bytes", default=4, show_default=True, type=click.IntRange(1, 8), help="Bus word size in bytes")
@click.option("--little-endian/--big-endian", default=True, show_default=True, help="Target endianness")
@click.option("--gen-pyi/--no-gen-pyi", default=False, help="Emit typing stub (.pyi)")
@click.option("--with-examples/--no-with-examples", default=False, help="Generate example master implementations")
@click.option("--no-access-checks", is_flag=True, help="Disable software access policy checks")
@click.option("--emit-reset-writes", is_flag=True, help="Emit helper that writes reset values")
def main(
    rdl: tuple[str, ...],
    soc_name: str,
    top_symbol: str,
    out_dir: str,
    namespace: Optional[str],
    word_bytes: int,
    little_endian: bool,
    gen_pyi: bool,
    with_examples: bool,
    no_access_checks: bool,
    emit_reset_writes: bool,
) -> None:
    if not rdl:
        raise click.UsageError("At least one RDL file must be provided")

    compiler = RDLCompiler()
    for path in rdl:
        compiler.compile_file(path)

    try:
        top_node = compiler.elaborate(top_symbol)
    except Exception as exc:  # pragma: no cover - pass through compiler failures
        raise click.ClickException(str(exc)) from exc

    options = PyBindOptions(
        soc_name=soc_name,
        namespace=namespace,
        out_dir=Path(out_dir),
        word_bytes=word_bytes,
        little_endian=little_endian,
        gen_pyi=gen_pyi,
        with_examples=with_examples,
        access_checks=not no_access_checks,
        emit_reset_writes=emit_reset_writes,
    )

    backend = PyBindBackend()
    backend.run(top_node, options)


if __name__ == "__main__":  # pragma: no cover
    try:
        main.main(standalone_mode=False)
    except SystemExit as exc:
        if exc.code:
            sys.exit(exc.code)
