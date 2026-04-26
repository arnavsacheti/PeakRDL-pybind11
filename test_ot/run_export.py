#!/usr/bin/env python3
"""Export the generated top_earlgrey.rdl into a PyBind11 module."""
from pathlib import Path

from systemrdl import RDLCompiler

from peakrdl_pybind11 import Pybind11Exporter

HERE = Path(__file__).parent
RDL = HERE / "top_earlgrey.rdl"
OUT = HERE / "output"

rdl = RDLCompiler()
rdl.compile_file(str(RDL))
root = rdl.elaborate()

OUT.mkdir(exist_ok=True)
exporter = Pybind11Exporter()
exporter.export(
    root.top,
    str(OUT),
    soc_name="top_earlgrey",
    gen_pyi=True,
    split_by_hierarchy=True,
)

print("Exported to", OUT)
print("To build: cd output && pip install .")
print("Then: python -c 'import top_earlgrey; soc=top_earlgrey.create(); print(dir(soc))'")
