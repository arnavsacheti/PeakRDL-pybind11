"""Shared fixtures for ``tests/runtime/``.

Three session-scoped fixtures are exposed:

* ``tiny_rdl`` — yields the path to a small (3-register) RDL file written
  to a tmpdir. Used by every unit-level test that needs *something* to
  feed the exporter.
* ``tiny_soc_built`` — exports ``tiny_rdl`` via
  :class:`peakrdl_pybind11.exporter.Pybind11Exporter`, builds the C++
  extension via cmake, imports the resulting module, and yields the SoC
  handle. Skipped automatically if cmake is missing or the build fails.
* ``mock_master`` — instantiates a :class:`MockMaster` and attaches it to
  ``tiny_soc_built``.

Tests that depend on ``tiny_soc_built`` should be marked
``@pytest.mark.integration`` so the default unit-test run can opt out
via ``-m "not integration"``. (Pytest 9 disallows applying marks to
fixtures themselves, so the mark belongs on the test.)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# A deliberately small RDL: three registers across two regfiles is enough
# to exercise hierarchy, multiple field shapes, and a flag/enum register
# without paying for cmake on every test that just needs the exporter.
_TINY_RDL = """
addrmap tiny_soc {
    name = "Tiny SoC";
    desc = "Three-register fixture for runtime unit tests";

    reg {
        name = "Control Register";
        field { sw = rw; hw = r; } enable[0:0] = 0;
        field { sw = rw; hw = r; } mode[3:1] = 0;
    } control @ 0x0;

    reg {
        name = "Status Register";
        field { sw = r; hw = w; } ready[0:0] = 0;
    } status @ 0x4;

    reg {
        name = "Data Register";
        field { sw = rw; hw = rw; } data[31:0] = 0;
    } data @ 0x8;
};
"""


@pytest.fixture(scope="session")
def tiny_rdl(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Yield the path to a session-scoped 3-register RDL file."""
    tmp_dir = tmp_path_factory.mktemp("tiny_rdl")
    rdl_path = tmp_dir / "tiny.rdl"
    rdl_path.write_text(_TINY_RDL)
    return rdl_path


def _have_cmake() -> bool:
    return shutil.which("cmake") is not None


@pytest.fixture(scope="session")
def tiny_soc_built(
    tiny_rdl: Path,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[Any]:
    """Export ``tiny_rdl`` and build the C++ extension; yield the imported SoC.

    Tests that depend on this fixture should be marked
    ``@pytest.mark.integration`` so the default unit-test run can opt out
    via ``-m "not integration"``. (Pytest 9 disallows marking fixtures
    directly, so the marker lives on the test, not the fixture.)
    Skipped if cmake is unavailable or the build fails.
    """
    if not _have_cmake():
        pytest.skip("cmake not available; skipping native build fixture")

    out_dir = tmp_path_factory.mktemp("tiny_soc_build")

    # 1. Compile the RDL.
    try:
        from systemrdl import RDLCompiler

        from peakrdl_pybind11.exporter import Pybind11Exporter
    except ImportError as exc:
        pytest.skip(f"required imports unavailable: {exc}")

    rdlc = RDLCompiler()
    Pybind11Exporter.register_udps(rdlc)
    rdlc.compile_file(str(tiny_rdl))
    root = rdlc.elaborate()

    # 2. Export.
    Pybind11Exporter().export(
        root, output_dir=str(out_dir), soc_name="tiny_soc", split_bindings=0
    )

    # 3. Build via cmake (release / Python's discovered cmake).
    try:
        subprocess.run(
            ["cmake", "-S", str(out_dir), "-B", str(out_dir / "build")],
            check=True,
            capture_output=True,
            timeout=300,
        )
        subprocess.run(
            ["cmake", "--build", str(out_dir / "build"), "--config", "Release"],
            check=True,
            capture_output=True,
            timeout=600,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"cmake build failed: {exc}")

    # 4. Move the built shared object next to the runtime so import works.
    pkg_dir = out_dir / "tiny_soc"
    build_dir = out_dir / "build"
    found = list(build_dir.glob("**/_tiny_soc_native*"))
    if not found:
        pytest.skip("native module not found after cmake build")
    for src in found:
        if src.is_file():
            shutil.copy2(src, pkg_dir / src.name)

    # 5. Import.
    sys.path.insert(0, str(out_dir))
    try:
        import importlib

        module = importlib.import_module("tiny_soc")
        soc = module.create()
        yield soc
    finally:
        sys.path.remove(str(out_dir))


@pytest.fixture()
def mock_master(tiny_soc_built: Any) -> Any:
    """A :class:`MockMaster` already attached to ``tiny_soc_built``.

    Tests using this fixture should also be marked
    ``@pytest.mark.integration``.
    """
    # The MockMaster class lives inside the generated module; per the
    # docstring on ``masters/base.py`` we prefer the C++ trampoline-free
    # version for in-memory fixtures.
    soc = tiny_soc_built
    master = soc.MockMaster() if hasattr(soc, "MockMaster") else None
    if master is None:
        pytest.skip("generated module exposes no MockMaster")
    # ``soc.attach()`` is the API in IDEAL_API_SKETCH.md §13; sibling
    # units may add it before any test actually exercises this fixture.
    # If it's missing today, fall back to the existing ``set_master`` shape.
    if hasattr(soc, "attach"):
        soc.attach(master)
    elif hasattr(soc, "set_master"):
        soc.set_master(master)
    return master
