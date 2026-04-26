"""
Regression tests for known bugs in the codebase.

Each test asserts the correct, expected behavior. Tests covering bugs that have
not yet been fixed are marked ``xfail(strict=True)`` with a reference to the
tracking issue. Once a bug is fixed the test will start XPASS-ing, and strict
xfail will fail CI until the marker is removed -- this is the regression latch.

Tracked issues (https://github.com/arnavsacheti/PeakRDL-pybind11/issues):
  #30 Duplicate pybind11 class registration for regfiles and addrmaps
  #31 Split-bindings mode never registers regfile/addrmap classes
  #32 Generated descriptors compute incorrect register addresses
  #33 Description strings not escaped before C++ literal embedding
  #34 Identifier sanitization does not handle reserved words / collisions
  #35 Generated IntFlag/IntEnum values use bit positions, not encodings
  #36 MockMaster.read ignores width
  #37 Runtime enhancement layer was heuristic
  #38 Memory set_offset reassigned entries, breaking pybind11 references
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile

import pytest
from systemrdl import RDLCompiler

try:
    from peakrdl_pybind11 import Pybind11Exporter
except ImportError:
    pytest.skip("peakrdl_pybind11 not installed", allow_module_level=True)


def _write_rdl(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".rdl")
    os.write(fd, content.encode("utf-8"))
    os.close(fd)
    return path


def _export(rdl_text: str, soc_name: str, **kwargs) -> str:
    """Compile RDL and export into a fresh temp dir; return that dir."""
    rdl = RDLCompiler()
    rdl.compile_file(_write_rdl(rdl_text))
    root = rdl.elaborate()

    tmpdir = tempfile.mkdtemp()
    Pybind11Exporter().export(root.top, tmpdir, soc_name=soc_name, **kwargs)
    return tmpdir


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


HIERARCHICAL_RDL = """
addrmap hier_soc {
    regfile {
        reg {
            field { sw = rw; hw = r; } data[7:0];
        } reg_a @ 0x00;
        reg {
            field { sw = rw; hw = r; } data[7:0];
        } reg_b @ 0x04;
    } block1 @ 0x100;

    regfile {
        reg {
            field { sw = rw; hw = r; } data[7:0];
        } reg_c @ 0x00;
    } block2 @ 0x200;
};
"""


# ---------------------------------------------------------------------------
# Issue #30: regfiles/addrmaps registered twice in single-file bindings
# ---------------------------------------------------------------------------
def test_issue_30_no_duplicate_class_registrations() -> None:
    out = _export(HIERARCHICAL_RDL, "hier_soc", split_bindings=0)
    bindings = _read(os.path.join(out, "hier_soc_bindings.cpp"))

    # Each regfile's pybind11 class should be registered exactly once.
    # Class names are now path-derived (e.g. hier_soc__block1_t) to keep them
    # globally unique across IPs that reuse names like INTR_STATE.
    for regfile in ("block1", "block2"):
        pattern = re.compile(rf'py::class_<\s*hier_soc__{regfile}_t\b')
        count = len(pattern.findall(bindings))
        assert count == 1, f"{regfile}_t registered {count} times in single-file bindings"

    shutil.rmtree(out, ignore_errors=True)


# ---------------------------------------------------------------------------
# Issue #31: split-bindings never registers regfile/addrmap
# ---------------------------------------------------------------------------
def test_issue_31_split_bindings_register_regfiles() -> None:
    out = _export(HIERARCHICAL_RDL, "hier_soc", split_by_hierarchy=True)

    # Concatenate every emitted .cpp file -- regfile bindings must appear somewhere.
    combined = ""
    for name in os.listdir(out):
        if name.endswith(".cpp"):
            combined += _read(os.path.join(out, name))

    for regfile in ("block1", "block2"):
        assert re.search(rf'py::class_<\s*hier_soc__{regfile}_t\b', combined), (
            f"{regfile}_t never registered with pybind11 in split-bindings output"
        )

    shutil.rmtree(out, ignore_errors=True)


# ---------------------------------------------------------------------------
# Issue #32: nested register address calculation
# ---------------------------------------------------------------------------
ADDR_RDL = """
addrmap addr_soc {
    reg {
        field { sw = rw; hw = r; } data[7:0];
    } top_reg @ 0x40;

    regfile {
        reg {
            field { sw = rw; hw = r; } data[7:0];
        } nested_reg @ 0x10;
    } block @ 0x200;
};
"""


def _build_address_probe(out: str, soc_name: str) -> int:
    """Compile a tiny driver that prints addresses, return its exit code.

    Skips if a C++ compiler isn't available.
    """
    cxx = shutil.which("g++") or shutil.which("clang++")
    if cxx is None:
        pytest.skip("No C++ compiler available")

    driver = os.path.join(out, "_probe.cpp")
    with open(driver, "w") as f:
        f.write(f"""
#include <cstdio>
#include <cstdlib>
#include "{soc_name}_descriptors.hpp"

int main() {{
    {soc_name}::{soc_name}_t soc;
    // Expected absolute addresses from the RDL:
    //   top_reg          = 0x40
    //   block.nested_reg = 0x200 + 0x10 = 0x210
    if (soc.top_reg.offset() != 0x40) {{
        std::fprintf(stderr, "top_reg offset = 0x%llx, expected 0x40\\n",
                     (unsigned long long)soc.top_reg.offset());
        return 1;
    }}
    if (soc.block.nested_reg.offset() != 0x210) {{
        std::fprintf(stderr, "block.nested_reg offset = 0x%llx, expected 0x210\\n",
                     (unsigned long long)soc.block.nested_reg.offset());
        return 2;
    }}
    return 0;
}}
""")

    binary = os.path.join(out, "_probe")
    compile_result = subprocess.run(
        [cxx, "-std=c++17", "-I", out, driver, "-o", binary],
        capture_output=True,
        timeout=30,
    )
    if compile_result.returncode != 0:
        pytest.fail(
            "Address probe failed to compile:\n"
            + compile_result.stdout.decode(errors="ignore")
            + compile_result.stderr.decode(errors="ignore")
        )

    run_result = subprocess.run([binary], capture_output=True, timeout=10)
    if run_result.returncode != 0:
        # Surface the failure text so the regression is readable when triaging.
        print(run_result.stderr.decode(errors="ignore"))
    return run_result.returncode


def test_issue_32_top_level_register_address() -> None:
    out = _export(ADDR_RDL, "addr_soc", split_bindings=0)
    try:
        rc = _build_address_probe(out, "addr_soc")
        assert rc == 0, f"address probe exited with {rc}"
    finally:
        shutil.rmtree(out, ignore_errors=True)


# ---------------------------------------------------------------------------
# Issue #33: descriptions with " or \ break the generated C++
# ---------------------------------------------------------------------------
QUOTE_DESC_RDL = r'''
addrmap quote_soc {
    reg {
        desc = "He said \"hello\" and used a backslash: \\";
        field { sw = rw; hw = r; } data[7:0];
    } chatty @ 0x0;
};
'''


def test_issue_33_descriptions_are_cpp_escaped() -> None:
    out = _export(QUOTE_DESC_RDL, "quote_soc", split_bindings=0)
    try:
        bindings = _read(os.path.join(out, "quote_soc_bindings.cpp"))

        # Find the line that registers the chatty register; the description
        # must arrive as a properly escaped C++ string literal.
        line = next(ln for ln in bindings.splitlines() if 'chatty_t' in ln and 'py::class_' in ln)

        # The raw description was: He said "hello" and used a backslash: \
        # After escaping, both " and \ must be doubled. There must be no
        # unescaped double-quote inside the literal payload.
        assert r'\"hello\"' in line, f"description not C-escaped: {line}"
        assert r'backslash: \\' in line, f"backslash not C-escaped: {line}"

        # Sanity: the literal payload (between the leading and trailing "")
        # must contain only escaped quotes -- never a bare " followed by
        # non-escape characters.
        # Strip the C++ identifier "chatty_t" string literal and the trailing ")"
        # then count non-escaped quotes -- should be exactly two (the opening
        # and closing of the description literal itself).
        desc_payload = line.split('"quote_soc__chatty_t",', 1)[1]
        non_escaped_quotes = re.findall(r'(?<!\\)"', desc_payload)
        assert len(non_escaped_quotes) == 2, (
            f"unbalanced/unescaped quotes in description literal:\n  {line}"
        )
    finally:
        shutil.rmtree(out, ignore_errors=True)


# ---------------------------------------------------------------------------
# Issue #34: reserved-word identifiers
# ---------------------------------------------------------------------------
RESERVED_WORD_RDL = """
addrmap kw_soc {
    reg {
        field { sw = rw; hw = r; } data[7:0];
    } class @ 0x0;

    reg {
        field { sw = rw; hw = r; } data[7:0];
    } template @ 0x4;
};
"""


def test_issue_34_reserved_word_identifiers_produce_valid_python() -> None:
    out = _export(RESERVED_WORD_RDL, "kw_soc", split_bindings=0)
    try:
        # The generated runtime module must at least parse as Python.
        runtime = _read(os.path.join(out, "__init__.py"))
        ast.parse(runtime)

        # Stubs must also parse.
        stubs = _read(os.path.join(out, "__init__.pyi"))
        ast.parse(stubs)
    finally:
        shutil.rmtree(out, ignore_errors=True)


# ---------------------------------------------------------------------------
# Issue #35: enum/flag value semantics
# ---------------------------------------------------------------------------
def _export_with_udps(rdl_text: str, soc_name: str, **kwargs) -> str:
    """Compile RDL with the exporter's UDPs pre-registered, then export."""
    from peakrdl_pybind11 import Pybind11Exporter

    rdl = RDLCompiler()
    Pybind11Exporter.register_udps(rdl)
    rdl.compile_file(_write_rdl(rdl_text))
    root = rdl.elaborate()

    tmpdir = tempfile.mkdtemp()
    Pybind11Exporter().export(root.top, tmpdir, soc_name=soc_name, **kwargs)
    return tmpdir


def _flag_class_members(runtime_src: str, class_name: str) -> dict[str, int]:
    """Parse runtime.py and return {member_name: int_value} for ``class_name``."""
    tree = ast.parse(runtime_src)
    cls = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == class_name),
        None,
    )
    assert cls is not None, f"class {class_name} not found in generated runtime"
    out: dict[str, int] = {}
    for stmt in cls.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, int)
        ):
            out[stmt.targets[0].id] = stmt.value.value
    return out


def test_issue_35_single_bit_flag_field_keeps_bare_name() -> None:
    rdl = """
    addrmap flag_soc {
        reg {
            is_flag = true;
            field { sw = rw; hw = r; } bit_a[0:0];
            field { sw = rw; hw = r; } bit_b[1:1];
            field { sw = rw; hw = r; } bit_c[2:2];
        } flags @ 0x0;
    };
    """
    out = _export_with_udps(rdl, "flag_soc", split_bindings=0)
    try:
        members = _flag_class_members(_read(os.path.join(out, "__init__.py")), "flag_soc__flags_f")
        assert members == {"bit_a": 1, "bit_b": 2, "bit_c": 4}
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_issue_35_multibit_flag_field_uses_indexed_suffix() -> None:
    """Width-N field expands to N power-of-two members named field_0..field_{N-1}."""
    rdl = """
    addrmap flag_soc {
        reg {
            is_flag = true;
            field { sw = rw; hw = r; } solo[0:0];
            field { sw = rw; hw = r; } trio[3:1];
        } flags @ 0x0;
    };
    """
    out = _export_with_udps(rdl, "flag_soc", split_bindings=0)
    try:
        members = _flag_class_members(_read(os.path.join(out, "__init__.py")), "flag_soc__flags_f")
        # solo at bit 0; trio spans bits [3:1] -> trio_0=2, trio_1=4, trio_2=8.
        assert members == {"solo": 1, "trio_0": 2, "trio_1": 4, "trio_2": 8}
        # Every member must still be a single power of two.
        for name, value in members.items():
            assert value & (value - 1) == 0, f"{name} = {value:#x} is not a single bit"
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_issue_35_flag_disable_drops_bits_before_naming() -> None:
    """flag_disable removes positions; the rest keep their original bit-position index."""
    rdl = """
    addrmap flag_soc {
        reg {
            is_flag = true;
            field { sw = rw; hw = r; flag_disable = "1,3"; } quad[3:0];
        } flags @ 0x0;
    };
    """
    out = _export_with_udps(rdl, "flag_soc", split_bindings=0)
    try:
        members = _flag_class_members(_read(os.path.join(out, "__init__.py")), "flag_soc__flags_f")
        # Bits 1 and 3 are disabled; bits 0 and 2 remain. Names preserve the
        # original bit-position index so the user can still reason about
        # which bit a member touches.
        assert members == {"quad_0": 1, "quad_2": 4}
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_issue_35_flag_names_overrides_default_suffixes_one_to_one() -> None:
    rdl = """
    addrmap flag_soc {
        reg {
            is_flag = true;
            field {
                sw = rw; hw = r;
                flag_names = "alpha,beta,gamma";
            } trio[2:0];
        } flags @ 0x0;
    };
    """
    out = _export_with_udps(rdl, "flag_soc", split_bindings=0)
    try:
        members = _flag_class_members(_read(os.path.join(out, "__init__.py")), "flag_soc__flags_f")
        # Names mapped 1:1 to bits 0,1,2 in ascending order.
        assert members == {"alpha": 1, "beta": 2, "gamma": 4}
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_issue_35_flag_disable_then_flag_names() -> None:
    """flag_disable is applied first, flag_names then aligns 1:1 with what remains."""
    rdl = """
    addrmap flag_soc {
        reg {
            is_flag = true;
            field {
                sw = rw; hw = r;
                flag_disable = "1,3";
                flag_names    = "low,high";
            } quad[3:0];
        } flags @ 0x0;
    };
    """
    out = _export_with_udps(rdl, "flag_soc", split_bindings=0)
    try:
        members = _flag_class_members(_read(os.path.join(out, "__init__.py")), "flag_soc__flags_f")
        # Bits 1,3 disabled -> bits 0,2 enabled -> names ("low","high")
        # mapped 1:1 to bit positions 0 and 2.
        assert members == {"low": 1, "high": 4}
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_issue_35_flag_names_with_fewer_entries_falls_back_to_indexed_suffix() -> None:
    rdl = """
    addrmap flag_soc {
        reg {
            is_flag = true;
            field {
                sw = rw; hw = r;
                flag_names = "first";
            } trio[2:0];
        } flags @ 0x0;
    };
    """
    out = _export_with_udps(rdl, "flag_soc", split_bindings=0)
    try:
        members = _flag_class_members(_read(os.path.join(out, "__init__.py")), "flag_soc__flags_f")
        # First name picks up bit 0; bits 1 and 2 fall back to indexed names.
        assert members == {"first": 1, "trio_1": 2, "trio_2": 4}
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_issue_35_flag_names_too_many_entries_raises() -> None:
    rdl = """
    addrmap flag_soc {
        reg {
            is_flag = true;
            field {
                sw = rw; hw = r;
                flag_names = "a,b,c,d";
            } pair[1:0];
        } flags @ 0x0;
    };
    """
    with pytest.raises(ValueError, match="flag_names"):
        _export_with_udps(rdl, "flag_soc", split_bindings=0)


def test_issue_35_enum_register_uses_same_naming_rules() -> None:
    """is_enum follows the same disable/names rules as is_flag."""
    rdl = """
    addrmap enum_soc {
        reg {
            is_enum = true;
            field {
                sw = rw; hw = r;
                flag_disable = "0";
                flag_names    = "running,error";
            } state[2:0];
        } mode @ 0x0;
    };
    """
    out = _export_with_udps(rdl, "enum_soc", split_bindings=0)
    try:
        members = _flag_class_members(_read(os.path.join(out, "__init__.py")), "enum_soc__mode_e")
        # Bit 0 disabled; bits 1,2 enabled -> "running"=2, "error"=4.
        assert members == {"running": 2, "error": 4}
    finally:
        shutil.rmtree(out, ignore_errors=True)


# ---------------------------------------------------------------------------
# Issue #37: runtime enhancement layer should be explicit, not heuristic
# ---------------------------------------------------------------------------
RUNTIME_ENHANCEMENT_RDL = """
addrmap rt_soc {
    reg {
        field { sw = rw; hw = r; } enable[0:0];
        field { sw = rw; hw = r; } mode[3:1];
    } control @ 0x0;
    reg {
        field { sw = r; hw = w; } ready[0:0];
    } status @ 0x4;
};
"""


def test_issue_37_runtime_enhancement_is_explicit_and_idempotent() -> None:
    out = _export(RUNTIME_ENHANCEMENT_RDL, "rt_soc", split_bindings=0)
    try:
        runtime = _read(os.path.join(out, "__init__.py"))
        ast.parse(runtime)

        # Per-register field layout must be baked in at codegen time, not
        # rebuilt by walking dir(self) on every .read().
        assert "_REGISTER_FIELDS" in runtime, "missing precomputed field table"
        assert "control_t: {" in runtime
        assert '"enable": (0, 1)' in runtime
        assert '"mode": (1, 3)' in runtime
        assert "status_t: {" in runtime
        assert '"ready": (0, 1)' in runtime

        # Field class set must be enumerated explicitly (no globals() walk
        # / no name-suffix heuristic).
        assert "_FIELD_CLASSES" in runtime
        assert "control_enable_field" in runtime
        assert "control_mode_field" in runtime
        assert "status_ready_field" in runtime

        # Forbid the old dir() / globals() heuristics.
        assert "for attr_name in dir(self)" not in runtime
        assert "for _name in list(globals().keys())" not in runtime

        # Wrapping must be idempotent: the wrappers carry a marker the
        # enhance helpers check before re-wrapping.
        assert "__peakrdl_enhanced__" in runtime
    finally:
        shutil.rmtree(out, ignore_errors=True)


# ---------------------------------------------------------------------------
# Issue #36: MockMaster ignores width on read
# ---------------------------------------------------------------------------
def test_issue_36_mock_master_read_honours_width() -> None:
    from peakrdl_pybind11.masters import MockMaster

    master = MockMaster()
    master.write(0x100, 0xDEADBEEF, 4)

    # Reading the same address at a narrower width should return only the
    # low `width` bytes, not the full stored 32-bit value.
    assert master.read(0x100, 1) == 0xEF
    assert master.read(0x100, 2) == 0xBEEF
    assert master.read(0x100, 4) == 0xDEADBEEF


# ---------------------------------------------------------------------------
# Issue #38: memory set_offset must update entries in place, not reassign
# ---------------------------------------------------------------------------
MEMORY_RELOCATE_RDL = """
addrmap relocate_soc {
    external mem {
        mementries = 4;
        memwidth   = 32;
        reg {
            field { sw = rw; hw = r; } data[7:0];
        } entry;
    } ctrl_mem @ 0x1000;
};
"""


def test_issue_38_memory_set_offset_preserves_entry_identity() -> None:
    """Compile a g++ probe that:

      1. holds a pointer to a memory entry,
      2. calls ``mem.set_offset(...)`` to relocate the array,
      3. confirms the *same* entry pointer reflects the new absolute
         address (i.e. the entry was updated in place, not reassigned).
    """
    cxx = shutil.which("g++") or shutil.which("clang++")
    if cxx is None:
        pytest.skip("No C++ compiler available")

    out = _export(MEMORY_RELOCATE_RDL, "relocate_soc", split_bindings=0)
    try:
        driver = os.path.join(out, "_probe.cpp")
        with open(driver, "w") as f:
            f.write(r"""
#include <cstdio>
#include "relocate_soc_descriptors.hpp"

int main() {
    relocate_soc::relocate_soc_t soc;

    // Initial layout: ctrl_mem at 0x1000 with 32-bit (4-byte) entries.
    auto* entry2_before = &soc.ctrl_mem[2];
    auto offset_before  = entry2_before->offset();
    if (offset_before != 0x1000ull + 2 * 4) {
        std::fprintf(stderr, "entry[2] initial offset = 0x%llx, expected 0x1008\n",
                     (unsigned long long)offset_before);
        return 1;
    }

    // Relocate the whole SoC.
    soc.set_offset(0x80000000ull);

    // Pointer must still be valid AND must reflect the new layout.
    auto* entry2_after = &soc.ctrl_mem[2];
    if (entry2_after != entry2_before) {
        std::fprintf(stderr, "entry[2] address changed across set_offset\n");
        return 2;
    }
    auto offset_after = entry2_before->offset();
    if (offset_after != 0x80001000ull + 2 * 4) {
        std::fprintf(stderr, "entry[2] post-relocate offset = 0x%llx, expected 0x80001008\n",
                     (unsigned long long)offset_after);
        return 3;
    }
    return 0;
}
""")

        binary = os.path.join(out, "_probe")
        compile_result = subprocess.run(
            [cxx, "-std=c++17", "-I", out, driver, "-o", binary],
            capture_output=True,
            timeout=30,
        )
        if compile_result.returncode != 0:
            pytest.fail(
                "Memory-relocation probe failed to compile:\n"
                + compile_result.stdout.decode(errors="ignore")
                + compile_result.stderr.decode(errors="ignore")
            )

        run_result = subprocess.run([binary], capture_output=True, timeout=10)
        if run_result.returncode != 0:
            print(run_result.stderr.decode(errors="ignore"))
        assert run_result.returncode == 0, (
            f"memory-relocation probe exited with {run_result.returncode}"
        )
    finally:
        shutil.rmtree(out, ignore_errors=True)
