"""
Main exporter implementation for PeakRDL-pybind11
"""

import os
import re
from typing import Optional
from systemrdl.node import RootNode, AddrmapNode, RegNode, RegfileNode, FieldNode
from systemrdl.rdltypes import AccessType, OnReadType, OnWriteType

from jinja2 import Environment, PackageLoader, select_autoescape


class Pybind11Exporter:
    """
    Export SystemRDL register descriptions to PyBind11 C++ modules
    """

    def __init__(self):
        self.env = Environment(
            loader=PackageLoader("peakrdl_pybind11", "templates"),
            autoescape=select_autoescape(),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.soc_name = None
        self.top_node = None
        self.output_dir = None

    def export(
        self,
        top_node: RootNode,
        output_dir: str,
        soc_name: Optional[str] = None,
        gen_pyi: bool = True,
        split_bindings: int = 100,
        split_by_hierarchy: bool = False,
    ):
        """
        Export SystemRDL to PyBind11 modules

        Parameters:
            top_node: Root node of the SystemRDL compilation
            output_dir: Directory to write output files
            soc_name: Name of the SoC module (default: derived from top node)
            gen_pyi: Generate .pyi stub files for type hints
            split_bindings: Split bindings into multiple files when register count exceeds this threshold.
                           Set to 0 to disable splitting. Default: 100
                           Ignored when split_by_hierarchy is True.
            split_by_hierarchy: When True, split bindings by addrmap/regfile hierarchy instead of
                               by register count. This keeps related registers together and provides
                               more logical grouping. Default: False
        """
        self.top_node = top_node.top if isinstance(top_node, RootNode) else top_node
        self.output_dir = output_dir
        self.soc_name = soc_name or self.top_node.inst_name or "soc"
        self.split_bindings = split_bindings
        self.split_by_hierarchy = split_by_hierarchy

        # Sanitize soc_name for use as identifier
        self.soc_name = self._sanitize_identifier(self.soc_name)

        # Create output directory
        os.makedirs(output_dir, exist_ok=True)

        # Collect all nodes first
        nodes = self._collect_nodes(self.top_node)

        # Generate C++ descriptor header
        self._generate_descriptors(nodes)

        # Generate PyBind11 bindings (split if needed)
        self._generate_bindings(nodes)

        # Generate Python runtime
        self._generate_python_runtime()

        # Generate setup.py for building the module
        self._generate_setup_py(nodes)

        # Generate .pyi stub files if requested
        if gen_pyi:
            self._generate_pyi_stubs(nodes)

    def _sanitize_identifier(self, name: str) -> str:
        """Sanitize a name to be a valid Python/C++ identifier"""
        # Replace invalid characters with underscores
        name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
        # Ensure it doesn't start with a digit
        if name and name[0].isdigit():
            name = "_" + name
        return name or "soc"

    def _generate_descriptors(self, nodes):
        """Generate C++ descriptor header file"""
        template = self.env.get_template("descriptors.hpp.jinja")

        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            nodes=nodes,
        )

        filepath = os.path.join(self.output_dir, f"{self.soc_name}_descriptors.hpp")
        with open(filepath, "w") as f:
            f.write(output)

    def _generate_bindings(self, nodes):
        """Generate PyBind11 bindings C++ file(s)"""
        reg_count = len(nodes["regs"])

        # Determine split mode
        if self.split_by_hierarchy:
            # Split by addrmap/regfile hierarchy
            self._generate_hierarchical_split_bindings(nodes)
        elif self.split_bindings > 0 and reg_count > self.split_bindings:
            # Split by register count
            self._generate_split_bindings(nodes)
        else:
            # Single file
            self._generate_single_binding(nodes)

    def _generate_single_binding(self, nodes):
        """Generate a single bindings file"""
        template = self.env.get_template("bindings.cpp.jinja")

        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            nodes=nodes,
            split_mode=False,
        )

        filepath = os.path.join(self.output_dir, f"{self.soc_name}_bindings.cpp")
        with open(filepath, "w") as f:
            f.write(output)

    def _generate_split_bindings(self, nodes):
        """Generate multiple split binding files for parallel compilation"""
        regs = nodes["regs"]
        chunk_size = self.split_bindings
        num_chunks = (len(regs) + chunk_size - 1) // chunk_size  # Ceiling division

        # Generate the main module file
        main_template = self.env.get_template("bindings_main.cpp.jinja")
        main_output = main_template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            num_chunks=num_chunks,
        )

        filepath = os.path.join(self.output_dir, f"{self.soc_name}_bindings.cpp")
        with open(filepath, "w") as f:
            f.write(main_output)

        # Generate split binding files
        chunk_template = self.env.get_template("bindings_chunk.cpp.jinja")

        for chunk_idx in range(num_chunks):
            start_idx = chunk_idx * chunk_size
            end_idx = min(start_idx + chunk_size, len(regs))
            chunk_regs = regs[start_idx:end_idx]

            chunk_output = chunk_template.render(
                soc_name=self.soc_name,
                chunk_idx=chunk_idx,
                regs=chunk_regs,
            )

            filepath = os.path.join(self.output_dir, f"{self.soc_name}_bindings_{chunk_idx}.cpp")
            with open(filepath, "w") as f:
                f.write(chunk_output)

    def _generate_hierarchical_split_bindings(self, nodes):
        """Generate split binding files organized by addrmap/regfile hierarchy"""
        # Group registers by their parent addrmap or regfile
        hierarchy_groups = self._group_registers_by_hierarchy(nodes)

        if not hierarchy_groups:
            # Fallback to single binding if no groups
            self._generate_single_binding(nodes)
            return

        num_chunks = len(hierarchy_groups)

        # Generate the main module file
        main_template = self.env.get_template("bindings_main.cpp.jinja")
        main_output = main_template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            num_chunks=num_chunks,
        )

        filepath = os.path.join(self.output_dir, f"{self.soc_name}_bindings.cpp")
        with open(filepath, "w") as f:
            f.write(main_output)

        # Generate split binding files for each hierarchy group
        chunk_template = self.env.get_template("bindings_chunk.cpp.jinja")

        for chunk_idx, (group_name, group_regs) in enumerate(hierarchy_groups.items()):
            chunk_output = chunk_template.render(
                soc_name=self.soc_name,
                chunk_idx=chunk_idx,
                regs=group_regs,
                chunk_name=group_name,  # Optional: for documentation/comments
            )

            filepath = os.path.join(self.output_dir, f"{self.soc_name}_bindings_{chunk_idx}.cpp")
            with open(filepath, "w") as f:
                f.write(chunk_output)

    def _group_registers_by_hierarchy(self, nodes):
        """Group registers by their parent addrmap or regfile for hierarchical splitting"""
        from collections import OrderedDict

        groups = OrderedDict()

        # Iterate through regfiles and addrmaps (excluding top node)
        for regfile in nodes["regfiles"]:
            group_name = regfile.inst_name
            group_regs = []

            # Collect all registers under this regfile
            for reg in nodes["regs"]:
                # Check if this register is a descendant of this regfile
                if self._is_descendant_of(reg, regfile):
                    group_regs.append(reg)

            if group_regs:
                groups[group_name] = group_regs

        # Handle addrmaps (excluding top node to avoid duplication)
        for addrmap in nodes["addrmaps"]:
            if addrmap == self.top_node:
                continue

            group_name = addrmap.inst_name
            group_regs = []

            # Collect all registers under this addrmap that aren't already in a regfile group
            for reg in nodes["regs"]:
                if self._is_descendant_of(reg, addrmap):
                    # Check if not already added via regfile
                    already_added = any(reg in regs for regs in groups.values())
                    if not already_added:
                        group_regs.append(reg)

            if group_regs:
                groups[group_name] = group_regs

        # Handle orphan registers (direct children of top node)
        orphan_regs = []
        for reg in nodes["regs"]:
            already_added = any(reg in regs for regs in groups.values())
            if not already_added:
                orphan_regs.append(reg)

        if orphan_regs:
            groups["top_level"] = orphan_regs

        return groups

    def _is_descendant_of(self, child_node, parent_node):
        """Check if child_node is a descendant of parent_node in the hierarchy"""
        current = child_node.parent
        while current is not None:
            if current == parent_node:
                return True
            current = current.parent
        return False

    def _generate_python_runtime(self):
        """Generate Python runtime module"""
        template = self.env.get_template("runtime.py.jinja")

        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
        )

        filepath = os.path.join(self.output_dir, "__init__.py")
        with open(filepath, "w") as f:
            f.write(output)

    def _generate_setup_py(self, nodes):
        """Generate CMakeLists.txt and pyproject.toml for building the C++ extension"""
        reg_count = len(nodes["regs"])

        # Determine if we're using split bindings and count the chunks
        if self.split_by_hierarchy:
            # Hierarchical splitting
            hierarchy_groups = self._group_registers_by_hierarchy(nodes)
            num_chunks = len(hierarchy_groups) if hierarchy_groups else 0
            use_split = num_chunks > 0
        else:
            # Register count splitting
            use_split = self.split_bindings > 0 and reg_count > self.split_bindings
            if use_split:
                chunk_size = self.split_bindings
                num_chunks = (reg_count + chunk_size - 1) // chunk_size
            else:
                num_chunks = 0

        if use_split and num_chunks > 0:
            source_files = [f"{self.soc_name}_bindings.cpp"] + [
                f"{self.soc_name}_bindings_{i}.cpp" for i in range(num_chunks)
            ]
        else:
            source_files = [f"{self.soc_name}_bindings.cpp"]

        # Generate CMakeLists.txt
        cmake_template = self.env.get_template("CMakeLists.txt.jinja")
        cmake_output = cmake_template.render(
            soc_name=self.soc_name,
            source_files=source_files,
        )
        cmake_filepath = os.path.join(self.output_dir, "CMakeLists.txt")
        with open(cmake_filepath, "w") as f:
            f.write(cmake_output)

        # Generate pyproject.toml for the module
        pyproject_template = self.env.get_template("pyproject_module.toml.jinja")
        pyproject_output = pyproject_template.render(
            soc_name=self.soc_name,
        )
        pyproject_filepath = os.path.join(self.output_dir, "pyproject.toml")
        with open(pyproject_filepath, "w") as f:
            f.write(pyproject_output)

    def _generate_pyi_stubs(self, nodes):
        """Generate .pyi stub files for type hints"""
        template = self.env.get_template("stubs.pyi.jinja")

        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            nodes=nodes,
        )

        filepath = os.path.join(self.output_dir, "__init__.pyi")
        with open(filepath, "w") as f:
            f.write(output)

    def _collect_nodes(self, node, nodes=None):
        """Recursively collect all nodes in the hierarchy"""
        if nodes is None:
            nodes = {
                "addrmaps": [],
                "regfiles": [],
                "regs": [],
                "fields": [],
            }

        if isinstance(node, AddrmapNode):
            nodes["addrmaps"].append(node)
            for child in node.children():
                self._collect_nodes(child, nodes)
        elif isinstance(node, RegfileNode):
            nodes["regfiles"].append(node)
            for child in node.children():
                self._collect_nodes(child, nodes)
        elif isinstance(node, RegNode):
            nodes["regs"].append(node)
            for field in node.fields():
                nodes["fields"].append(field)

        return nodes
