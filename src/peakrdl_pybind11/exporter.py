"""
Main exporter implementation for PeakRDL-pybind11
"""

import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import TypedDict

from jinja2 import Environment, PackageLoader, select_autoescape
from systemrdl.node import AddrmapNode, FieldNode, MemNode, Node, RegfileNode, RegNode, RootNode


class Nodes(TypedDict):
    addrmaps: list[AddrmapNode]
    regfiles: list[RegfileNode]
    regs: list[RegNode]
    fields: list[FieldNode]
    mems: list[MemNode]
    flag_regs: list[RegNode]  # Registers with flag UDP property
    enum_regs: list[RegNode]  # Registers with enum UDP property


class Pybind11Exporter:
    """
    Export SystemRDL register descriptions to PyBind11 C++ modules
    """

    def __init__(self) -> None:
        self.env = Environment(
            loader=PackageLoader("peakrdl_pybind11", "templates"),
            autoescape=select_autoescape(),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.soc_name: str | None = None
        self.soc_version: str = "0.1.0"
        self.top_node: AddrmapNode | None = None
        self.output_dir: Path | None = None

    def export(
        self,
        top_node: RootNode | AddrmapNode,
        output_dir: str,
        soc_name: str | None = None,
        soc_version: str = "0.1.0",
        gen_pyi: bool = True,
        split_bindings: int = 100,
        split_by_hierarchy: bool = False,
    ) -> None:
        """
        Export SystemRDL to PyBind11 modules

        Parameters:
            top_node: Root node of the SystemRDL compilation
            output_dir: Directory to write output files
            soc_name: Name of the SoC module (default: derived from top node)
            soc_version: Version string for the SoC module (default: "0.1.0")
            gen_pyi: Generate .pyi stub files for type hints
            split_bindings: Split bindings into multiple files when register count exceeds this threshold.
                           Set to 0 to disable splitting. Default: 100
                           Ignored when split_by_hierarchy is True.
            split_by_hierarchy: When True, split bindings by addrmap/regfile hierarchy instead of
                               by register count. This keeps related registers together and provides
                               more logical grouping. Default: False
        """
        self.top_node = top_node.top if isinstance(top_node, RootNode) else top_node
        self.output_dir = Path(output_dir)
        self.soc_name = soc_name or self.top_node.inst_name or "soc"
        self.soc_version = soc_version
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
        self._generate_python_runtime(nodes)

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

    def _generate_descriptors(self, nodes: Nodes) -> None:
        """Generate C++ descriptor header file"""
        template = self.env.get_template("descriptors.hpp.jinja")

        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            nodes=nodes,
        )

        assert self.output_dir is not None
        filepath = self.output_dir / f"{self.soc_name}_descriptors.hpp"
        with filepath.open("w") as f:
            f.write(output)

    def _generate_bindings(self, nodes: Nodes) -> None:
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

    def _generate_single_binding(self, nodes: Nodes) -> None:
        """Generate a single bindings file"""
        template = self.env.get_template("bindings.cpp.jinja")

        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            nodes=nodes,
            split_mode=False,
        )

        assert self.output_dir is not None
        filepath = self.output_dir / f"{self.soc_name}_bindings.cpp"
        with filepath.open("w") as f:
            f.write(output)

    def _generate_split_bindings(self, nodes: Nodes) -> None:
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
            nodes=nodes,
        )

        assert self.output_dir is not None
        filepath = self.output_dir / f"{self.soc_name}_bindings.cpp"
        with filepath.open("w") as f:
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

            filepath = self.output_dir / f"{self.soc_name}_bindings_{chunk_idx}.cpp"
            with filepath.open("w") as f:
                f.write(chunk_output)

    def _generate_hierarchical_split_bindings(self, nodes: Nodes) -> None:
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
            nodes=nodes,
        )

        assert self.output_dir is not None
        filepath = self.output_dir / f"{self.soc_name}_bindings.cpp"
        with filepath.open("w") as f:
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

            filepath = self.output_dir / f"{self.soc_name}_bindings_{chunk_idx}.cpp"
            with filepath.open("w") as f:
                f.write(chunk_output)

    def _group_registers_by_hierarchy(self, nodes: Nodes) -> OrderedDict[str, list[RegNode]]:
        """Group registers by their parent addrmap or regfile for hierarchical splitting

        This method groups registers based on their immediate parent addrmap or regfile.
        Priority: regfile > addrmap > top_level

        Performance: O(n) where n is the number of registers, using a single pass with O(1) lookups.
        """
        from collections import OrderedDict

        groups: OrderedDict[str, list[RegNode]] = OrderedDict()

        # Create lookup dictionaries using object id for O(1) lookups
        # Map id(node) -> node for quick membership testing
        regfiles_map = {id(rf): rf for rf in nodes["regfiles"]}
        addrmaps_map = {id(am): am for am in nodes["addrmaps"] if am != self.top_node}

        # Single pass through all registers to find their grouping parent
        for reg in nodes["regs"]:
            # Walk up the hierarchy to find the first regfile or addrmap (excluding top)
            group_parent = None
            current: AddrmapNode | MemNode | RegNode | RootNode | None = reg.parent

            while current is not None:
                current_id = id(current)
                # Prioritize regfiles over addrmaps
                if current_id in regfiles_map:
                    group_parent = current
                    break
                elif current_id in addrmaps_map:
                    group_parent = current
                    break
                current = current.parent

            # Determine the group name and add the register
            if group_parent is not None:
                group_name = group_parent.inst_name
                if group_name not in groups:
                    groups[group_name] = []
                groups[group_name].append(reg)
            else:
                # Orphan register (direct child of top node or no matching parent)
                if "top_level" not in groups:
                    groups["top_level"] = []
                groups["top_level"].append(reg)

        return groups

    def _generate_python_runtime(self, nodes: Nodes) -> None:
        """Generate Python runtime module"""
        template = self.env.get_template("runtime.py.jinja")

        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            nodes=nodes,
        )

        assert self.output_dir is not None
        filepath = self.output_dir / "__init__.py"
        with filepath.open("w") as f:
            f.write(output)

    def _generate_setup_py(self, nodes: Nodes) -> None:
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

        assert self.output_dir is not None
        # Generate CMakeLists.txt
        cmake_template = self.env.get_template("CMakeLists.txt.jinja")
        cmake_output = cmake_template.render(
            soc_name=self.soc_name,
            source_files=source_files,
        )
        cmake_filepath = self.output_dir / "CMakeLists.txt"
        with cmake_filepath.open("w") as f:
            f.write(cmake_output)

        # Generate pyproject.toml for the module
        pyproject_template = self.env.get_template("pyproject_module.toml.jinja")
        pyproject_output = pyproject_template.render(
            soc_name=self.soc_name,
            soc_version=self.soc_version,
        )
        pyproject_filepath = self.output_dir / "pyproject.toml"
        with pyproject_filepath.open("w") as f:
            f.write(pyproject_output)

    def _generate_pyi_stubs(self, nodes: Nodes) -> None:
        """Generate .pyi stub files for type hints"""
        template = self.env.get_template("stubs.pyi.jinja")

        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            nodes=nodes,
        )

        assert self.output_dir is not None
        filepath = self.output_dir / "__init__.pyi"
        with filepath.open("w") as f:
            f.write(output)

    def _collect_nodes(self, node: Node, nodes: Nodes | None = None) -> Nodes:
        """Recursively collect all nodes in the hierarchy"""
        if nodes is None:
            nodes = {
                "addrmaps": [],
                "regfiles": [],
                "regs": [],
                "fields": [],
                "mems": [],
                "flag_regs": [],
                "enum_regs": [],
            }

        if isinstance(node, AddrmapNode):
            nodes["addrmaps"].append(node)
            for child in node.children():
                self._collect_nodes(child, nodes)
        elif isinstance(node, RegfileNode):
            nodes["regfiles"].append(node)
            for child in node.children():
                self._collect_nodes(child, nodes)
        elif isinstance(node, MemNode):
            if node.is_array:
                for element in node.unrolled():
                    assert isinstance(element, MemNode)
                    children = list(element.children())
                    if children:
                        nodes["mems"].append(element)
                        for child in children:
                            self._collect_nodes(child, nodes)
            else:
                children = list(node.children())
                if children:
                    nodes["mems"].append(node)
                    for child in children:
                        self._collect_nodes(child, nodes)
        elif isinstance(node, RegNode):
            if node.is_array:
                for element in node.unrolled():
                    assert isinstance(element, RegNode)
                    nodes["regs"].append(element)
                    # Check for flag/is_enum UDP properties (may not be defined)
                    try:
                        if element.get_property('flag'):
                            nodes["flag_regs"].append(element)
                    except LookupError:
                        pass
                    try:
                        if element.get_property('is_enum'):
                            nodes["enum_regs"].append(element)
                    except LookupError:
                        pass
                    for field in element.fields():
                        nodes["fields"].append(field)
            else:
                nodes["regs"].append(node)
                # Check for flag/is_enum UDP properties (may not be defined)
                try:
                    if node.get_property('flag'):
                        nodes["flag_regs"].append(node)
                except LookupError:
                    pass
                try:
                    if node.get_property('is_enum'):
                        nodes["enum_regs"].append(node)
                except LookupError:
                    pass
                for field in node.fields():
                    nodes["fields"].append(field)

        return nodes
