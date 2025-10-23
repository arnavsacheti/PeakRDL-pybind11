"""
PeakRDL exporter integration
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse

    from peakrdl.plugins.exporter import ExporterSubcommandPlugin
    from systemrdl.node import AddrmapNode
else:
    try:
        from peakrdl.plugins.exporter import ExporterSubcommandPlugin  # pyrefly: ignore[missing-import]
    except ImportError:
        # peakrdl is an optional dependency
        ExporterSubcommandPlugin = object  # type: ignore[misc]

from .exporter import Pybind11Exporter


class Exporter(ExporterSubcommandPlugin):
    """Entry point for PeakRDL exporter plugin"""

    name = "pybind11"
    short_desc = "Export SystemRDL to PyBind11 modules for Python-based hardware testing"
    long_desc = (
        "Generate PyBind11 C++ bindings and Python modules from SystemRDL register descriptions. "
        "This exporter creates a complete Python API for hardware register access with pluggable "
        "master backends (Mock, OpenOCD, SSH, or custom)."
    )

    def add_exporter_arguments(self, arg_group: "argparse._ActionsContainer") -> None:
        """Add exporter-specific arguments to the command line"""
        arg_group.add_argument(
            "--soc-name",
            dest="soc_name",
            metavar="NAME",
            help="Name of the generated SoC module (default: derived from input file)",
        )
        arg_group.add_argument(
            "--gen-pyi",
            dest="gen_pyi",
            action="store_true",
            default=True,
            help="Generate .pyi stub files for type hints (default: enabled)",
        )
        arg_group.add_argument(
            "--no-gen-pyi", dest="gen_pyi", action="store_false", help="Disable generation of .pyi stub files"
        )
        arg_group.add_argument(
            "--split-bindings",
            dest="split_bindings",
            type=int,
            metavar="COUNT",
            default=100,
            help=(
                "Split bindings into multiple files for parallel compilation when register count "
                "exceeds this threshold. This significantly speeds up compilation for large "
                "register maps. Set to 0 to disable splitting. Ignored when --split-by-hierarchy "
                "is used. (default: 100)"
            ),
        )
        arg_group.add_argument(
            "--split-by-hierarchy",
            dest="split_by_hierarchy",
            action="store_true",
            default=False,
            help=(
                "Split bindings by addrmap/regfile hierarchy instead of by register count. "
                "This keeps related registers together in the same compilation unit, providing "
                "more logical grouping and better organization. Recommended for large designs "
                "with clear hierarchical structure."
            ),
        )

    def do_export(self, top_node: "AddrmapNode", options: "argparse.Namespace") -> None:
        """Execute the export"""
        exporter = Pybind11Exporter()

        # Get soc_name from options or derive from input
        soc_name = getattr(options, "soc_name", None)
        if soc_name is None:
            soc_name = top_node.inst_name or "soc"

        gen_pyi = getattr(options, "gen_pyi", True)
        split_bindings = getattr(options, "split_bindings", 100)
        split_by_hierarchy = getattr(options, "split_by_hierarchy", False)

        exporter.export(
            top_node,
            options.output,
            soc_name=soc_name,
            gen_pyi=gen_pyi,
            split_bindings=split_bindings,
            split_by_hierarchy=split_by_hierarchy,
        )
