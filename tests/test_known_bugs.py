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
    for regfile in ("block1", "block2"):
        pattern = re.compile(rf'py::class_<\s*{regfile}_t\b')
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
        assert re.search(rf'py::class_<\s*{regfile}_t\b', combined), (
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


@pytest.mark.xfail(
    reason="Issue #32: top-level register absolute_address is double-counted",
    strict=True,
)
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
        desc_payload = line.split('"chatty_t",', 1)[1]
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


@pytest.mark.xfail(
    reason="Issue #34: identifier sanitizer does not protect Python/C++ keywords",
    strict=True,
)
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
FLAG_RDL = """
addrmap flag_soc {
    reg {
        is_flag = true;
        field { sw = rw; hw = r; } bit_a[0:0];
        field { sw = rw; hw = r; } bit_b[1:1];
        field { sw = rw; hw = r; } bit_c[2:2];
    } flags @ 0x0;
};
"""


def _is_flag_user_property_supported() -> bool:
    """is_flag/is_enum require the user-property registration done elsewhere.

    The compiler will refuse the RDL otherwise. We try a cheap compile to find
    out, and skip the test if the property isn't registered in this build.
    """
    rdl = RDLCompiler()
    try:
        rdl.compile_file(_write_rdl(FLAG_RDL))
        rdl.elaborate()
    except Exception:
        return False
    return True


@pytest.mark.xfail(
    reason="Issue #35: IntFlag values for multi-bit fields are masks, not flag bits",
    strict=True,
)
def test_issue_35_flag_register_emits_single_bit_values() -> None:
    if not _is_flag_user_property_supported():
        pytest.skip("is_flag user property not registered for this build")

    out = _export(FLAG_RDL, "flag_soc", split_bindings=0)
    try:
        runtime = _read(os.path.join(out, "__init__.py"))

        # Each single-bit flag field must end up as exactly the corresponding bit:
        #   bit_a -> 1 << 0, bit_b -> 1 << 1, bit_c -> 1 << 2
        # Currently the template emits `2 ** field.low`, which is correct here,
        # but it also emits a multi-bit MASK when width > 1 (not exercised in
        # this minimal RDL). We assert IntFlag inheritance and that each member
        # is a power of two -- the invariant that multi-bit-field handling
        # currently violates.
        assert "class flags_f(RegisterIntFlag):" in runtime

        tree = ast.parse(runtime)
        flag_cls = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.ClassDef) and n.name == "flags_f"),
            None,
        )
        assert flag_cls is not None

        for stmt in flag_cls.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                value = stmt.value.value
                assert isinstance(value, int) and value > 0 and (value & (value - 1)) == 0, (
                    f"flag member {ast.unparse(stmt.targets[0])} has non-power-of-two value {value}"
                )
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
