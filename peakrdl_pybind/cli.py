"""Command line entry point for the peakrdl-pybind backend."""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import click

from .backend import PyBindBackend, BackendOptions


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("rdl", nargs=-1, type=click.Path(path_type=Path))
@click.option("--soc-name", required=True, help="Name of the SoC/package to generate")
@click.option("--out", "output", required=True, type=click.Path(path_type=Path), help="Output directory")
@click.option("--top", required=True, help="Top-level addrmap symbol")
@click.option("--word-bytes", default=4, show_default=True, type=int, help="Bus word size in bytes")
@click.option("--little-endian/--big-endian", default=True, show_default=True, help="Endianess for generated helpers")
@click.option("--with-examples", is_flag=True, help="Generate example master implementations")
@click.option("--gen-pyi", is_flag=True, help="Generate typing stubs")
@click.option("--namespace", default=None, help="Override Python package name")
@click.option("--emit-reset-writes/--no-emit-reset-writes", default=True, show_default=True, help="Emit reset_to_defaults helper")
@click.option("--no-access-checks", is_flag=True, help="Disable access policy enforcement")
def main(
    rdl: Tuple[Path, ...],
    soc_name: str,
    output: Path,
    top: str,
    word_bytes: int,
    little_endian: bool,
    with_examples: bool,
    gen_pyi: bool,
    namespace: str | None,
    emit_reset_writes: bool,
    no_access_checks: bool,
) -> None:
    """CLI wrapper that compiles RDL files and invokes the backend."""

    if not rdl:
        raise click.UsageError("At least one .rdl file must be provided")

    backend = PyBindBackend()
    backend.run_cli(
        rdl_files=[str(path) for path in rdl],
        out=output,
        soc_name=soc_name,
        top=top,
        word_bytes=word_bytes,
        little_endian=little_endian,
        with_examples=with_examples,
        gen_pyi=gen_pyi,
        namespace=namespace,
        emit_reset_writes=emit_reset_writes,
        no_access_checks=no_access_checks,
    )


__all__ = ["main"]
