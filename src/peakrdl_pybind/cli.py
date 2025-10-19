"""Command line interface for the peakrdl-pybind backend."""

from __future__ import annotations

import argparse

from systemrdl import RDLCompiler

from .backend import PyBindBackend


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rdl", help="Path to the top-level SystemRDL file")
    parser.add_argument("--soc-name", required=True, help="Name of the generated SoC package")
    parser.add_argument("--out", required=True, help="Output directory for generated sources")
    parser.add_argument("--top", required=True, help="Name of the top-level addrmap symbol")
    parser.add_argument("--word-bytes", type=int, default=4, choices=(1, 2, 4, 8))
    parser.add_argument("--little-endian", action="store_true", default=True)
    parser.add_argument("--big-endian", action="store_false", dest="little_endian")
    parser.add_argument("--with-examples", action="store_true")
    parser.add_argument("--gen-pyi", action="store_true")
    parser.add_argument("--namespace", help="Override the generated Python package namespace")
    parser.add_argument("--no-access-checks", action="store_true")
    parser.add_argument("--emit-reset-writes", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    compiler = RDLCompiler()
    compiler.compile_file(args.rdl)
    root = compiler.elaborate(args.top)

    backend = PyBindBackend()
    backend.build(
        node=root,
        output_dir=args.out,
        soc_name=args.soc_name,
        word_bytes=args.word_bytes,
        little_endian=args.little_endian,
        with_examples=args.with_examples,
        gen_pyi=args.gen_pyi,
        namespace=args.namespace,
        no_access_checks=args.no_access_checks,
        emit_reset_writes=args.emit_reset_writes,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
