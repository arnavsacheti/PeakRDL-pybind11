"""
Main exporter implementation for PeakRDL-pybind11
"""

import keyword
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import TypedDict

# Words that cannot be used as identifiers in either Python or C++.
#
# We deliberately use only ``keyword.kwlist`` (hard Python keywords) and skip
# ``keyword.softkwlist`` -- soft keywords like ``_``, ``match``, ``case``,
# ``type`` are only reserved in specific syntactic contexts (e.g. match-case
# patterns) and remain valid identifiers everywhere else. Mangling them
# would, for example, turn the bare ``_`` field into ``__``, which is more
# disruptive than helpful.
_RESERVED_WORDS: frozenset[str] = frozenset(
    set(keyword.kwlist)
    | {
        # C++ keywords / reserved identifiers that could collide with an RDL
        # inst_name. Not exhaustive -- focused on commonly-used names.
        "alignas", "alignof", "and", "and_eq", "asm", "auto", "bitand",
        "bitor", "bool", "break", "case", "catch", "char", "char8_t",
        "char16_t", "char32_t", "class", "compl", "concept", "const",
        "consteval", "constexpr", "constinit", "const_cast", "continue",
        "co_await", "co_return", "co_yield", "decltype", "default", "delete",
        "do", "double", "dynamic_cast", "else", "enum", "explicit", "export",
        "extern", "false", "float", "for", "friend", "goto", "if", "inline",
        "int", "long", "mutable", "namespace", "new", "noexcept", "not",
        "not_eq", "nullptr", "operator", "or", "or_eq", "private", "protected",
        "public", "register", "reinterpret_cast", "requires", "return",
        "short", "signed", "sizeof", "static", "static_assert", "static_cast",
        "struct", "switch", "template", "this", "thread_local", "throw",
        "true", "try", "typedef", "typeid", "typename", "union", "unsigned",
        "using", "virtual", "void", "volatile", "wchar_t", "while", "xor",
        "xor_eq",
        # Identifiers used by the generator itself; collisions would shadow
        # generated members.
        "Master", "RegisterBase", "FieldBase", "NodeBase", "MemoryBase",
    }
)

from jinja2 import Environment, PackageLoader, select_autoescape
from systemrdl.node import AddrmapNode, FieldNode, MemNode, Node, RegfileNode, RegNode, RootNode


class Nodes(TypedDict):
    addrmaps: list[AddrmapNode]
    regfiles: list[RegfileNode]
    regs: list[RegNode]
    fields: list[FieldNode]
    mems: list[MemNode]
    flag_regs: list[RegNode]
    enum_regs: list[RegNode]
    # Per-register flag/enum members: keyed by id(reg) -> [(name, value), ...].
    # Populated for entries in flag_regs and enum_regs.
    register_members: dict[int, list[tuple[str, int]]]


# UDPs that the exporter understands. CLI users declare these in their RDL
# (or call ``Pybind11Exporter.register_udps(rdl_compiler)`` when invoking
# the compiler programmatically).
#
# - is_flag, is_enum   (reg, bool)   : tag a register as IntFlag / IntEnum
# - flag_disable        (field, str) : comma-separated list of bit indices
#                                      within the field (0 = lsb) to drop
#                                      from the generated enum/flag.
# - flag_names          (field, str) : comma-separated identifiers, mapped
#                                      1:1 to the bits remaining after
#                                      flag_disable. Trailing positions
#                                      without an entry fall back to the
#                                      default "{field}_{i}" naming.
_KNOWN_UDPS: tuple[tuple[str, str, type], ...] = (
    ("is_flag", "reg", bool),
    ("is_enum", "reg", bool),
    ("flag_disable", "field", str),
    ("flag_names", "field", str),
)


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
        self.env.filters["pybind_name"] = self._pybind_name_from_node
        self.env.filters["enum_member"] = self._enum_member_name
        self.env.filters["cpp_string"] = self._cpp_string_escape
        self.env.filters["safe_id"] = self._sanitize_identifier
        # Lazily resolves to the (name, value) list for an is_flag / is_enum
        # register; populated by _collect_nodes.
        self._members_by_id: dict[int, list[tuple[str, int]]] = {}
        self.env.filters["members"] = self._members_for_node
        self.soc_name: str | None = None
        self.soc_version: str = "0.1.0"
        self.top_node: AddrmapNode | None = None
        self.output_dir: Path | None = None
        self._name_cache: dict[str, str] = {}

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
        self._members_by_id = nodes["register_members"]

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
        """Sanitize a name to be a valid Python/C++ identifier.

        Replaces non-identifier characters with underscores, prefixes a
        leading digit, and appends a trailing underscore to any sanitized
        name that collides with a Python or C++ reserved word.
        """
        name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
        if name and name[0].isdigit():
            name = "_" + name
        if name in _RESERVED_WORDS:
            name = name + "_"
        return name or "soc"

    def _pybind_name_from_node(self, value: Node | str) -> str:
        """Return a unique, sanitized identifier for a node."""
        if isinstance(value, Node):
            path = value.get_path()
        else:
            path = str(value)

        if path not in self._name_cache:
            sanitized_path = path.replace(".", "__").replace("[", "_").replace("]", "_")
            self._name_cache[path] = self._sanitize_identifier(sanitized_path)
        return self._name_cache[path]

    @staticmethod
    def _cpp_string_escape(value: object) -> str:
        """Escape ``value`` so it is safe to embed inside a C++ "..." literal."""
        text = "" if value is None else str(value)
        out: list[str] = []
        for ch in text:
            if ch == "\\":
                out.append("\\\\")
            elif ch == '"':
                out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20:
                out.append(f"\\x{ord(ch):02x}")
            else:
                out.append(ch)
        return "".join(out)

    def _enum_member_name(self, name: str) -> str:
        """Convert a field name into a suitable enum member name."""
        parts = re.split(r"[^a-zA-Z0-9]+", name)
        camel = "".join(part[:1].upper() + part[1:] for part in parts if part)
        candidate = camel or name
        candidate = re.sub(r"[^a-zA-Z0-9_]", "_", candidate)
        if candidate and candidate[0].isdigit():
            candidate = "_" + candidate
        return candidate or "Field"

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
                "register_members": {},
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
            children = list(node.children())
            if children:
                nodes["mems"].append(node)
                for child in children:
                    self._collect_nodes(child, nodes)
        elif isinstance(node, RegNode):
            nodes["regs"].append(node)
            is_flag = self._get_bool_property(node, "is_flag")
            is_enum = self._get_bool_property(node, "is_enum")
            # If both are set, is_flag takes precedence.
            if is_flag:
                nodes["flag_regs"].append(node)
                nodes["register_members"][id(node)] = self._register_member_layout(node)
            elif is_enum:
                nodes["enum_regs"].append(node)
                nodes["register_members"][id(node)] = self._register_member_layout(node)
            for field in node.fields():
                nodes["fields"].append(field)

        return nodes

    def _get_bool_property(self, node: Node, name: str) -> bool:
        """Safely read a boolean property from a node."""
        try:
            value = node.get_property(name)
        except LookupError:
            return False
        return bool(value)

    def _members_for_node(self, node: Node) -> list[tuple[str, int]]:
        """Jinja filter: return the (name, value) members for a flag/enum reg."""
        return self._members_by_id.get(id(node), [])

    @staticmethod
    def _get_string_property(node: Node, name: str) -> str | None:
        """Safely read a string property from a node, returning None if absent."""
        try:
            value = node.get_property(name)
        except LookupError:
            return None
        if value is None or value == "":
            return None
        return str(value)

    @staticmethod
    def _parse_index_list(value: str, *, width: int, where: str) -> set[int]:
        """Parse a comma-separated bit-index list and validate against ``width``."""
        result: set[int] = set()
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                idx = int(token, 0)
            except ValueError as e:
                raise ValueError(f"{where}: cannot parse {token!r} as an integer") from e
            if idx < 0 or idx >= width:
                raise ValueError(
                    f"{where}: bit index {idx} is out of range for a width-{width} field"
                )
            result.add(idx)
        return result

    @staticmethod
    def _parse_name_list(value: str) -> list[str]:
        """Parse a comma-separated identifier list, dropping empty entries."""
        return [s.strip() for s in value.split(",") if s.strip()]

    def _register_member_layout(self, reg: RegNode) -> list[tuple[str, int]]:
        """Compute (name, value) pairs for an is_flag/is_enum register.

        Each field contributes one member per *enabled* bit position. The
        ``flag_disable`` field UDP drops bit positions (indices 0..width-1,
        where 0 is the field's lsb) before naming. The ``flag_names`` field
        UDP supplies explicit identifiers mapped 1:1 to the remaining bits
        in ascending order; trailing positions without an entry fall back
        to ``{field}_{i}`` (where i is the bit-position-within-field).
        """
        members: list[tuple[str, int]] = []
        for field in reg.fields():
            width = field.width
            low = field.low

            disable_str = self._get_string_property(field, "flag_disable")
            if disable_str:
                disabled = self._parse_index_list(
                    disable_str, width=width, where=f"flag_disable on {field.get_path()}"
                )
            else:
                disabled = set()
            enabled = [i for i in range(width) if i not in disabled]

            names_str = self._get_string_property(field, "flag_names")
            if names_str:
                names = self._parse_name_list(names_str)
                if len(names) > len(enabled):
                    raise ValueError(
                        f"flag_names on {field.get_path()} has {len(names)} entries "
                        f"but only {len(enabled)} bit(s) remain after flag_disable"
                    )
            else:
                names = []

            base_name = self._sanitize_identifier(field.inst_name)
            for slot, bit_index in enumerate(enabled):
                if slot < len(names):
                    name = self._sanitize_identifier(names[slot])
                elif width == 1:
                    name = base_name
                else:
                    name = f"{base_name}_{bit_index}"
                members.append((name, 1 << (low + bit_index)))
        return members

    @classmethod
    def register_udps(cls, rdl_compiler: object) -> None:
        """Register every UDP this exporter recognizes with the given compiler.

        For programmatic use:

            from systemrdl import RDLCompiler
            from peakrdl_pybind11 import Pybind11Exporter

            rdl = RDLCompiler()
            Pybind11Exporter.register_udps(rdl)
            rdl.compile_file(...)

        CLI users can equivalently declare the UDPs in their RDL.
        """
        for prop_name, component, prop_type in _KNOWN_UDPS:
            cls._register_udp(rdl_compiler, prop_name, component, prop_type)

    @staticmethod
    def _register_udp(rdl_compiler: object, prop_name: str, component: str, prop_type: type) -> None:
        from systemrdl import component as _comp
        from systemrdl.udp import UDPDefinition

        component_cls_map = {
            "reg": _comp.Reg,
            "field": _comp.Field,
        }
        try:
            comp_cls = component_cls_map[component]
        except KeyError as e:
            raise ValueError(f"Unsupported UDP component scope: {component!r}") from e

        udp_class = type(
            f"_UDPDef_{prop_name}",
            (UDPDefinition,),
            {
                "name": prop_name,
                "valid_components": {comp_cls},
                "valid_type": prop_type,
            },
        )
        register = getattr(rdl_compiler, "register_udp", None)
        if register is None:
            raise TypeError(
                "rdl_compiler does not look like a systemrdl RDLCompiler "
                "(no register_udp method)"
            )
        # soft=False so the UDP is recognized immediately without the user
        # also having to declare `property is_flag { ... };` in their RDL.
        register(udp_class, soft=False)
