"""
PeakRDL exporter integration
"""
from typing import TYPE_CHECKING

from peakrdl.plugins.exporter import ExporterSubcommandPlugin

from .exporter import Pybind11Exporter

if TYPE_CHECKING:
    import argparse
    from systemrdl.node import AddrmapNode


class Exporter(ExporterSubcommandPlugin):
    """Entry point for PeakRDL exporter plugin"""
    
    name = "pybind11"
    short_desc = "Export SystemRDL to PyBind11 modules for Python-based hardware testing"
    long_desc = (
        "Generate PyBind11 C++ bindings and Python modules from SystemRDL register descriptions. "
        "This exporter creates a complete Python API for hardware register access with pluggable "
        "master backends (Mock, OpenOCD, SSH, or custom)."
    )
    
    def add_exporter_arguments(self, arg_group: 'argparse.ArgumentParser') -> None:
        """Add exporter-specific arguments to the command line"""
        arg_group.add_argument(
            "--soc-name",
            dest="soc_name",
            metavar="NAME",
            help="Name of the generated SoC module (default: derived from input file)"
        )
        arg_group.add_argument(
            "--gen-pyi",
            dest="gen_pyi",
            action="store_true",
            default=True,
            help="Generate .pyi stub files for type hints (default: enabled)"
        )
        arg_group.add_argument(
            "--no-gen-pyi",
            dest="gen_pyi",
            action="store_false",
            help="Disable generation of .pyi stub files"
        )
    
    def do_export(self, top_node: 'AddrmapNode', options: 'argparse.Namespace') -> None:
        """Execute the export"""
        exporter = Pybind11Exporter()
        
        # Get soc_name from options or derive from input
        soc_name = getattr(options, 'soc_name', None)
        if soc_name is None:
            soc_name = top_node.inst_name or "soc"
        
        gen_pyi = getattr(options, 'gen_pyi', True)
        
        exporter.export(
            top_node,
            options.output,
            soc_name=soc_name,
            gen_pyi=gen_pyi
        )
