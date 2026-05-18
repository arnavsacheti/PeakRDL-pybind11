"""
Main exporter implementation for PeakRDL-pybind11
"""

import keyword
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import TypedDict

from jinja2 import Environment, PackageLoader, select_autoescape
from systemrdl.node import (
    AddressableNode,
    AddrmapNode,
    FieldNode,
    MemNode,
    Node,
    RegfileNode,
    RegNode,
    RootNode,
    SignalNode,
)

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
        "alignas",
        "alignof",
        "and",
        "and_eq",
        "asm",
        "auto",
        "bitand",
        "bitor",
        "bool",
        "break",
        "case",
        "catch",
        "char",
        "char8_t",
        "char16_t",
        "char32_t",
        "class",
        "compl",
        "concept",
        "const",
        "consteval",
        "constexpr",
        "constinit",
        "const_cast",
        "continue",
        "co_await",
        "co_return",
        "co_yield",
        "decltype",
        "default",
        "delete",
        "do",
        "double",
        "dynamic_cast",
        "else",
        "enum",
        "explicit",
        "export",
        "extern",
        "false",
        "float",
        "for",
        "friend",
        "goto",
        "if",
        "inline",
        "int",
        "long",
        "mutable",
        "namespace",
        "new",
        "noexcept",
        "not",
        "not_eq",
        "nullptr",
        "operator",
        "or",
        "or_eq",
        "private",
        "protected",
        "public",
        "register",
        "reinterpret_cast",
        "requires",
        "return",
        "short",
        "signed",
        "sizeof",
        "static",
        "static_assert",
        "static_cast",
        "struct",
        "switch",
        "template",
        "this",
        "thread_local",
        "throw",
        "true",
        "try",
        "typedef",
        "typeid",
        "typename",
        "union",
        "unsigned",
        "using",
        "virtual",
        "void",
        "volatile",
        "wchar_t",
        "while",
        "xor",
        "xor_eq",
        # Identifiers used by the generator itself; collisions would shadow
        # generated members.
        "Master",
        "RegisterBase",
        "FieldBase",
        "NodeBase",
        "MemoryBase",
    }
)


class RegArrayInfo(TypedDict):
    """Metadata for an arrayed RDL register (Phase 1 of Tier 3 array support).

    ``node`` is the unrolled-shape entry view (``current_idx`` set to
    index 0) so the rest of the pipeline can walk fields with absolute
    addresses for index 0; the C++ ``ArrayBase`` constructor then
    reconstructs each entry's absolute offset at runtime via
    ``offset_ + i * stride``. ``dimensions`` is a list of ints (e.g.
    ``[8]`` for ``reg foo[8]``); Phase 1 always has length 1. Phase 3
    (#138) added ``strides`` for multi-dim support, but kept the
    Phase 1 ``stride`` field populated with the innermost-axis stride
    so back-compat consumers keep working.
    """

    node: RegNode
    dimensions: list[int]
    stride: int
    relative_offset: int


class ArrayInfo(TypedDict):
    """Unified metadata for an arrayed RDL node (register or regfile).

    Phase 2 (issue #138) extended the Phase 1 ``RegArrayInfo`` shape
    with a ``kind`` discriminator. Phase 3 added per-axis ``strides``
    (a list of ints, length == ``len(dimensions)``); the back-compat
    ``stride`` (singular) field is preserved as the innermost-axis
    stride for any consumer that still reads it. Templates iterate
    ``nodes["arrays"]`` and filter by ``kind`` (``"reg"`` /
    ``"regfile"``) when their emission differs.

    ``node`` is the unrolled-shape entry view (``current_idx`` set to
    index 0) so the pipeline can walk children at absolute addresses
    for index 0; ``ArrayBase`` then reconstructs each entry's absolute
    offset at runtime via ``offset_ + i * stride``. ``dimensions`` is
    a list of ints; Phases 1+2 always had length 1, Phase 3 allows N.

    For an ``N``-dim array, ``strides[-1]`` is the innermost-axis stride
    (entry size for register arrays, regfile size for regfile arrays)
    and each outer entry multiplies by the next-inner dimension —
    ``strides[i] = strides[i+1] * dimensions[i+1]``. The C++ side
    consumes this list to construct one nested ``ArrayBase`` subclass
    per axis, with each level's ctor pre-filling its own size + stride.
    """

    kind: str
    node: RegNode | RegfileNode | AddrmapNode
    dimensions: list[int]
    stride: int
    strides: list[int]
    relative_offset: int


class Nodes(TypedDict):
    addrmaps: list[AddrmapNode]
    regfiles: list[RegfileNode]
    regs: list[RegNode]
    fields: list[FieldNode]
    mems: list[MemNode]
    flag_regs: list[RegNode]
    enum_regs: list[RegNode]
    # RDL ``signal`` declarations under any AddrmapNode / RegfileNode.
    # Consumed by ``runtime.py.jinja`` to populate the per-SoC signal
    # registry that ``runtime.signals._attach_signals`` reads at
    # create() time. Signals have no relevant descendants for the
    # exporter, so the collector never recurses into a SignalNode.
    signals: list[SignalNode]
    # Per-register flag/enum members: keyed by id(reg) -> [(name, value), ...].
    # Populated for entries in flag_regs and enum_regs.
    register_members: dict[int, list[tuple[str, int]]]
    # Per-field RDL ``encode`` enums: keyed by field path -> [(name, value), ...].
    # Populated when a FieldNode has a non-None ``encode`` property pointing
    # at a systemrdl ``UserEnum``. Consumed by ``runtime.py.jinja`` to emit
    # one ``IntEnum`` per encoded field (sketch §8.1). Orthogonal to the
    # register-level ``is_flag`` / ``is_enum`` mechanism.
    #
    # Keyed by path string (not ``id(field)``) because systemrdl's
    # ``RegNode.fields()`` returns fresh ``FieldNode`` wrappers every call —
    # identity is per-iteration, but ``get_path()`` is stable.
    field_encodes: dict[str, list[tuple[str, int]]]
    # Unified arrayed-node list (Phase 2 of Tier 3 array support;
    # issue #138). Each entry carries a ``kind`` (``"reg"`` /
    # ``"regfile"``) discriminator plus the underlying entry-type node,
    # dimensions, stride, and relative-offset. Templates iterate
    # ``nodes["arrays"]`` and filter by ``kind`` where their emission
    # differs (e.g. ``descriptors/arrays.hpp.jinja`` emits only
    # ``kind == "reg"`` because regfile-array typedefs need
    # ``<rf>_t`` to be complete and therefore live in a separate
    # partial included after ``regfiles.hpp.jinja``).
    #
    # Consumed by:
    #
    # * ``descriptors/arrays.hpp.jinja`` — emits ``<reg>_array_t :
    #   ArrayBase<<reg>_t>`` per ``kind == "reg"`` entry.
    # * ``descriptors/regfile_arrays.hpp.jinja`` — emits
    #   ``<rf>_array_t : ArrayBase<<rf>_t>`` per ``kind == "regfile"``
    #   entry. Included **after** ``regfiles.hpp.jinja`` so
    #   ``std::vector<<rf>_t>`` sees the complete type.
    # * ``bindings_main.cpp.jinja`` / ``bindings.cpp.jinja`` — emit the
    #   per-array pybind11 binding (``__len__``, int/slice ``__getitem__``,
    #   ``__iter__``, ``shape``, ``stride``).
    # * ``runtime.py.jinja`` — emits the ``_ARRAY_PATHS`` list and a
    #   post-create hook that swaps the raw C++ array node for an
    #   :class:`ArrayView` wrapper.
    #
    # Current scope: 1-D and multi-dim register / regfile / addrmap
    # arrays at the SoC root or under non-arrayed parents. Field arrays
    # use a separate ``FieldArray`` runtime wrapper (Phase 4 of issue
    # #138) and aren't tracked here.
    arrays: list["ArrayInfo"]
    # Phase 1 back-compat alias. Populated as the ``kind == "reg"``
    # subset of ``arrays``; downstream code that hasn't been migrated
    # to the unified list can still iterate ``reg_arrays``.
    reg_arrays: list["RegArrayInfo"]


def _compute_per_axis_strides(dimensions: list[int], inner_stride: int) -> list[int]:
    """Compute per-axis byte strides for a multi-dim arrayed node.

    ``dimensions`` is row-major (outermost first; e.g. ``[4, 8]`` for
    ``reg foo[4][8]``). ``inner_stride`` is the bytes between adjacent
    elements in the innermost axis — this is what ``systemrdl``'s
    ``array_stride`` reports for both 1-D and N-D arrays (verified
    empirically: ``foo[4][8]`` with 32-bit entries has
    ``array_stride == 4``, matching the inner-axis layout).

    The result is a list of the same length as ``dimensions``: the
    innermost entry is ``inner_stride``; each outer entry is the
    product of the next-inner entry's stride and the next-inner
    dimension. For ``[4, 8]`` with ``inner_stride=4`` the returned
    list is ``[32, 4]`` — the C++ outer ``ArrayBase`` ctor sees
    ``(count=4, stride=32)``, the inner sees ``(count=8, stride=4)``,
    and ``ArrayBase<ArrayBase<entry_t>>`` walks both levels correctly.

    A 1-D array reduces to ``[inner_stride]`` — back-compatible with
    Phase 1 / Phase 2 templates that read the (singular) ``stride``.
    """
    if not dimensions:
        return []
    strides = [0] * len(dimensions)
    strides[-1] = int(inner_stride)
    for i in range(len(dimensions) - 2, -1, -1):
        strides[i] = strides[i + 1] * dimensions[i + 1]
    return strides


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
        # Reports the array-base absolute address of a node, skipping
        # per-entry stride contribution from any arrayed ancestor.
        # Equivalent to ``node.absolute_address`` when nothing in the
        # lineage is arrayed; falls back to ``raw_absolute_address`` as
        # soon as any ancestor is. Surfaced as a filter so the runtime
        # template can stay declarative.
        self.env.filters["array_base_address"] = self._array_base_address
        # ``repr`` produces a properly-quoted, escape-safe Python literal
        # for any value — used by the signals block in ``runtime.py.jinja``
        # to inline RDL paths and UDP keys without re-implementing escape
        # logic.
        self.env.filters["python_string"] = repr
        self.env.filters["safe_id"] = self._sanitize_identifier
        # Lazily resolves to the (name, value) list for an is_flag / is_enum
        # register; populated by _collect_nodes.
        self._members_by_id: dict[int, list[tuple[str, int]]] = {}
        self.env.filters["members"] = self._members_for_node
        # Per-field encode IntEnum member list; populated by _collect_nodes.
        # Keyed by ``FieldNode.get_path()`` because systemrdl returns fresh
        # FieldNode wrappers from each ``RegNode.fields()`` call — keying on
        # ``id(field)`` would miss every template-side lookup.
        self._field_encodes_by_path: dict[str, list[tuple[str, int]]] = {}
        self.env.filters["field_encode_members"] = self._field_encode_members_for_node
        self.soc_name: str | None = None
        self.soc_version: str = "0.1.0"
        self.top_node: AddrmapNode | None = None
        self.output_dir: Path | None = None
        self._name_cache: dict[str, str] = {}
        # ``--udp-config`` declared-type map (sketch §8.2 / §18). Keys are
        # UDP attribute names; values are the *string* type name the user
        # declared (one of ``{"int", "bool", "str", "float"}``). Empty by
        # default — undeclared UDPs continue to fall back to ``Any`` on
        # the stub side and the permissive ``TagsNamespace`` at runtime.
        self._udp_type_map: dict[str, str] = {}

        # Discover sibling-unit exporter plugins. Each plugin's
        # ``register(self)`` runs immediately so it can install Jinja
        # filters, store references, etc.; codegen-time hooks (if any)
        # are scheduled by the plugin via attributes on the exporter.
        from .exporter_plugins import discover_plugins

        discover_plugins(self)

    def export(
        self,
        top_node: RootNode | AddrmapNode,
        output_dir: str,
        soc_name: str | None = None,
        soc_version: str = "0.1.0",
        gen_pyi: bool = True,
        split_bindings: int = 100,
        split_by_hierarchy: bool = False,
        interrupt_pattern: object | None = None,
        udp_config: str | Path | None = None,
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
            interrupt_pattern: Optional override for the interrupt-state-register matcher used by
                               the feature_detection exporter plugin. Accepts a regex string,
                               compiled ``re.Pattern``, or a callable ``(name: str) -> bool``.
            udp_config: Optional path to a TOML file declaring typed wrappers for user-defined
                        properties (UDPs), per sketch §8.2 / §18. The file maps UDP names to one
                        of ``{"int", "bool", "str", "float"}``; declared types replace the
                        default ``Any`` on ``info.tags.<udp_name>`` for type-checkers. Undeclared
                        UDPs fall back to today's permissive ``TagsNamespace``. Requires Python
                        3.11+ (uses :mod:`tomllib`); the rest of the package works on 3.10.
        """
        self.top_node = top_node.top if isinstance(top_node, RootNode) else top_node
        self.output_dir = Path(output_dir)
        self.soc_name = soc_name or self.top_node.inst_name or "soc"
        self.soc_version = soc_version
        self.split_bindings = split_bindings
        self.split_by_hierarchy = split_by_hierarchy
        self.interrupt_pattern = interrupt_pattern

        # Parse the ``--udp-config`` TOML once and stash the declared-type
        # map on ``self`` so downstream consumers (currently: planned
        # .pyi stub generation; see TODO in ``runtime/info.py``) can pick
        # it up without re-parsing. ``None`` clears any previous map.
        if udp_config is None:
            self._udp_type_map = {}
        else:
            from .cli.udp_config import parse_udp_config

            self._udp_type_map = parse_udp_config(udp_config)

        # Sanitize soc_name for use as identifier
        self.soc_name = self._sanitize_identifier(self.soc_name)

        # Create output directory
        os.makedirs(output_dir, exist_ok=True)

        # Collect all nodes first
        nodes = self._collect_nodes(self.top_node)
        self._members_by_id = nodes["register_members"]
        self._field_encodes_by_path = nodes["field_encodes"]

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

        # Run post-export plugins (interrupt detection, schema, etc.).
        # Plugins are best-effort: a plugin failure must not stop the
        # main exporter from declaring success.
        self._run_post_export_plugins(nodes)

    def _run_post_export_plugins(self, nodes: "Nodes") -> None:
        from .exporter_plugins import PluginContext, run_post_export

        assert self.top_node is not None
        assert self.output_dir is not None
        assert self.soc_name is not None

        ctx = PluginContext(
            exporter=self,
            top_node=self.top_node,
            output_dir=self.output_dir,
            soc_name=self.soc_name,
            nodes=nodes,
            options={"interrupt_pattern": self.interrupt_pattern},
        )
        try:
            run_post_export(ctx)
        except Exception:  # pragma: no cover - defensive
            import logging

            logging.getLogger(__name__).exception("post_export plugin failed")

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
        """Return a unique, sanitized identifier for a node.

        For arrayed nodes (``foo[]`` or ``foo[i]``), strip the empty
        ``[]`` first so the entry-type identifier stays clean
        (``a.lut[]`` → ``a__lut`` rather than ``a__lut__``). Concrete
        index suffixes ``foo[3]`` collapse to ``foo_3_`` as before; those
        only appear when something has unrolled the array and is asking
        for a specific entry's identifier.
        """
        if isinstance(value, Node):
            # ``get_path(empty_array_suffix="")`` drops trailing ``[]``
            # while leaving concrete indices (``foo[3]``) intact. Falls
            # back to plain ``get_path()`` on older systemrdl that
            # doesn't accept the kwarg.
            try:
                path = value.get_path(empty_array_suffix="")
            except TypeError:  # pragma: no cover - older systemrdl
                path = value.get_path().replace("[]", "")
        else:
            path = str(value)

        if path not in self._name_cache:
            sanitized_path = path.replace(".", "__").replace("[", "_").replace("]", "_")
            self._name_cache[path] = self._sanitize_identifier(sanitized_path)
        return self._name_cache[path]

    @staticmethod
    def _array_base_address(node: AddressableNode) -> int:
        """Return the array-base absolute address of ``node``.

        Equivalent to ``node.absolute_address`` when neither ``node``
        nor any ancestor is arrayed. As soon as any link in the
        lineage is arrayed, ``absolute_address`` raises (it needs a
        concrete ``current_idx`` to derive a per-entry address), so we
        fall back to ``raw_absolute_address`` which sums the raw
        offsets and ignores per-entry stride contribution. The C++
        ``ArrayBase`` constructor reconstructs the per-entry absolute
        offset at runtime; the runtime metadata only needs the base.

        Used as the ``array_base_address`` Jinja filter from the
        runtime template's ``_REGISTER_INFO`` block to handle registers
        whose ancestor chain includes an arrayed regfile (Phase 2 of
        Tier 3 array support, issue #138). Phase 1 was simpler — only
        the register itself could be arrayed.
        """
        cur: Node | None = node
        while cur is not None:
            if isinstance(cur, RootNode):
                break
            if getattr(cur, "is_array", False):
                return int(node.raw_absolute_address)
            cur = cur.parent
        return int(node.absolute_address)

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
        with filepath.open("w", encoding="utf-8") as f:
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
        with filepath.open("w", encoding="utf-8") as f:
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
        with filepath.open("w", encoding="utf-8") as f:
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
            with filepath.open("w", encoding="utf-8") as f:
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
        with filepath.open("w", encoding="utf-8") as f:
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
            with filepath.open("w", encoding="utf-8") as f:
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
            strict_fields=getattr(self, "strict_fields", True),
        )

        assert self.output_dir is not None
        assert self.soc_name is not None
        pkg_dir = self.output_dir / self.soc_name
        pkg_dir.mkdir(exist_ok=True)
        for filepath in (pkg_dir / "__init__.py", self.output_dir / "__init__.py"):
            with filepath.open("w", encoding="utf-8") as f:
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
        with cmake_filepath.open("w", encoding="utf-8") as f:
            f.write(cmake_output)

        # Generate pyproject.toml for the module
        pyproject_template = self.env.get_template("pyproject_module.toml.jinja")
        pyproject_output = pyproject_template.render(
            soc_name=self.soc_name,
            soc_version=self.soc_version,
        )
        pyproject_filepath = self.output_dir / "pyproject.toml"
        with pyproject_filepath.open("w", encoding="utf-8") as f:
            f.write(pyproject_output)

    def _generate_pyi_stubs(self, nodes: Nodes) -> None:
        """Generate .pyi stub files for type hints"""
        template = self.env.get_template("stubs.pyi.jinja")

        output = template.render(
            soc_name=self.soc_name,
            top_node=self.top_node,
            nodes=nodes,
            # ``--udp-config`` declared-type map (sketch §8.2 / §18).
            # Empty when no config was supplied — the template guards
            # the typed-UDP namespace emission with ``{% if udp_type_map %}``
            # so SoCs built without ``--udp-config`` produce zero new
            # stub content.
            udp_type_map=self._udp_type_map,
        )

        assert self.output_dir is not None
        assert self.soc_name is not None
        pkg_dir = self.output_dir / self.soc_name
        pkg_dir.mkdir(exist_ok=True)
        for filepath in (pkg_dir / "__init__.pyi", self.output_dir / "__init__.pyi"):
            with filepath.open("w", encoding="utf-8") as f:
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
                "signals": [],
                "register_members": {},
                "field_encodes": {},
                "arrays": [],
                "reg_arrays": [],
            }

        if isinstance(node, AddrmapNode):
            # Arrayed addrmap (issues #137 / #138 follow-up). Treated as
            # the regfile-array path's twin — same ``ArrayInfo`` shape,
            # same ``ArrayBase<entry_t>`` C++ codegen, but with
            # ``kind="addrmap"`` so the descriptor partial routed by
            # kind picks the right include order (after ``addrmaps.hpp``,
            # before ``top_node.hpp``).
            if node.is_array and node != self.top_node:
                dims = list(node.array_dimensions or [])
                inner_stride = node.array_stride
                if inner_stride is None:
                    inner_stride = int(node.size)
                strides = _compute_per_axis_strides(dims, int(inner_stride))
                nodes["arrays"].append(
                    {
                        "kind": "addrmap",
                        "node": node,
                        "dimensions": dims,
                        "stride": strides[-1] if strides else int(inner_stride),
                        "strides": strides,
                        "relative_offset": int(node.raw_address_offset),
                    }
                )

            nodes["addrmaps"].append(node)
            # Descend into children even when the addrmap is arrayed.
            # ``node.children()`` on an arrayed addrmap yields child
            # nodes with ``current_idx=0`` unrolled, so the inner
            # registers / regfiles get a bound index for their own
            # ``address_offset``. Required so each child's C++ class
            # is emitted exactly once regardless of array size.
            for child in node.children():
                # ``SignalNode`` children of an addrmap/regfile have no
                # relevant descendants for us; track them flat and skip
                # the recursive descent the other kinds use.
                if isinstance(child, SignalNode):
                    nodes["signals"].append(child)
                    continue
                self._collect_nodes(child, nodes)
        elif isinstance(node, RegfileNode):
            # Detect arrayed regfiles (Phase 2 of Tier 3). Phase 3 (#138)
            # extends to multi-dim — nested ``ArrayBase<ArrayBase<...>>``
            # is emitted via the per-axis ``strides`` list below.
            if node.is_array:
                dims = list(node.array_dimensions or [])
                # ``array_stride`` is the innermost axis stride (bytes
                # between adjacent elements when the array is unrolled
                # in row-major order). Each outer axis multiplies by
                # the next-inner dimension to build the full chain.
                inner_stride = node.array_stride
                if inner_stride is None:
                    inner_stride = int(node.size)
                strides = _compute_per_axis_strides(dims, int(inner_stride))
                # ``raw_address_offset`` because ``address_offset`` raises
                # on un-indexed array nodes; the C++ ``ArrayBase`` derives
                # each entry's absolute offset from this base + i*stride.
                nodes["arrays"].append(
                    {
                        "kind": "regfile",
                        "node": node,
                        "dimensions": dims,
                        # Phase 1/2 back-compat: ``stride`` (singular)
                        # = innermost-axis stride. For 1-D, this is the
                        # only stride; for multi-dim, ``strides`` carries
                        # the full chain.
                        "stride": strides[-1] if strides else int(inner_stride),
                        "strides": strides,
                        "relative_offset": int(node.raw_address_offset),
                    }
                )

            nodes["regfiles"].append(node)
            # Descend into children even when the regfile is arrayed.
            # ``node.children()`` on an arrayed regfile yields child
            # nodes with ``current_idx=0`` unrolled, so the inner
            # registers' ``address_offset`` works as on a non-arrayed
            # regfile. Required so each child register's C++ class is
            # emitted exactly once.
            for child in node.children():
                if isinstance(child, SignalNode):
                    nodes["signals"].append(child)
                    continue
                self._collect_nodes(child, nodes)
        elif isinstance(node, MemNode):
            children = list(node.children())
            if children:
                nodes["mems"].append(node)
                for child in children:
                    self._collect_nodes(child, nodes)
        elif isinstance(node, RegNode):
            # Detect arrayed registers (Phase 1+3 of Tier 3). 1-D arrays
            # produce a single ``ArrayBase<entry_t>`` subclass; N-dim
            # arrays produce ``N`` nested ``ArrayBase<...>`` subclasses,
            # one per axis. The per-axis ``strides`` list is the load-
            # bearing piece — see ``ArrayInfo`` for the formula.
            if node.is_array:
                dims = list(node.array_dimensions or [])
                # Use ``raw_address_offset`` because ``address_offset`` raises
                # on arrayed nodes (it needs ``current_idx`` to derive a
                # concrete entry's address). The C++ ``ArrayBase`` constructor
                # then derives each entry's absolute address from this base.
                # ``array_stride`` is ``Optional[int]`` in the systemrdl
                # type stubs, but is always set on a node where
                # ``is_array`` is True; fall back to the entry size for
                # the (unreachable) ``None`` branch. For multi-dim,
                # ``array_stride`` gives the innermost axis stride; the
                # outer-axis strides multiply out from there.
                inner_stride = node.array_stride
                if inner_stride is None:
                    inner_stride = int(node.size)
                strides = _compute_per_axis_strides(dims, int(inner_stride))
                nodes["arrays"].append(
                    {
                        "kind": "reg",
                        "node": node,
                        "dimensions": dims,
                        # Back-compat: innermost-axis stride. Multi-dim
                        # consumers should use ``strides`` (plural).
                        "stride": strides[-1] if strides else int(inner_stride),
                        "strides": strides,
                        "relative_offset": int(node.raw_address_offset),
                    }
                )
                # Phase 1 back-compat: keep ``reg_arrays`` populated so
                # any consumer that hasn't migrated to ``arrays`` still
                # sees the register-array subset. ``stride`` (singular)
                # stays the innermost-axis stride for 1-D parity.
                nodes["reg_arrays"].append(
                    {
                        "node": node,
                        "dimensions": dims,
                        "stride": strides[-1] if strides else int(inner_stride),
                        "relative_offset": int(node.raw_address_offset),
                    }
                )

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
                # Per-field RDL ``encode`` UDP (sketch §8.1). The property
                # returns a ``UserEnum`` subclass whose ``.members`` is an
                # ordered dict of ``name -> UserEnumMember``. Coerce to a
                # ``(name, int)`` list once so the template doesn't need
                # to know about systemrdl internals.
                try:
                    enc = field.get_property("encode")
                except LookupError:
                    enc = None
                if enc is not None:
                    members = getattr(enc, "members", None)
                    if members:
                        nodes["field_encodes"][field.get_path()] = [
                            (str(name), int(member.value)) for name, member in members.items()
                        ]

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

    def _field_encode_members_for_node(self, node: Node) -> list[tuple[str, int]]:
        """Jinja filter: return the IntEnum members for a field with RDL ``encode``.

        Returns ``[]`` when the field has no encode — that's the signal the
        template uses to decide whether to emit the per-field enum class.

        Lookup is by ``node.get_path()`` because systemrdl's
        ``RegNode.fields()`` returns fresh FieldNode wrappers per call;
        keying on ``id(node)`` would always miss the template-side lookups.
        """
        return self._field_encodes_by_path.get(node.get_path(), [])

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
                raise ValueError(f"{where}: bit index {idx} is out of range for a width-{width} field")
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
                "rdl_compiler does not look like a systemrdl RDLCompiler (no register_udp method)"
            )
        # soft=False so the UDP is recognized immediately without the user
        # also having to declare `property is_flag { ... };` in their RDL.
        register(udp_class, soft=False)
