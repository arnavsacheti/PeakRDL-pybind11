from pathlib import Path
import sys

import pytest

pytest.importorskip("jinja2")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from peakrdl_pybind.backend import PyBindBackend


def test_parse_options(tmp_path: Path) -> None:
    backend = PyBindBackend()
    options = backend._parse_options(  # type: ignore[attr-defined]
        output_dir=str(tmp_path),
        soc_name="aurora",
        word_bytes=8,
        little_endian=False,
        with_examples=True,
        gen_pyi=True,
        namespace="aurora_soc",
        no_access_checks=True,
        emit_reset_writes=True,
    )
    assert options.output == tmp_path
    assert options.word_bytes == 8
    assert not options.little_endian
    assert options.generate_examples
    assert options.generate_pyi
    assert options.namespace == "aurora_soc"
    assert options.no_access_checks
    assert options.emit_reset_writes


def test_render_templates_round_trip(tmp_path: Path) -> None:
    backend = PyBindBackend()
    context = {
        "options": backend._parse_options(str(tmp_path), soc_name="aurora"),  # type: ignore[attr-defined]
        "registers": [],
    }
    backend._render_templates(tmp_path, context)  # type: ignore[attr-defined]
    generated = {p.name for p in tmp_path.iterdir() if p.is_file()}
    assert "CMakeLists.txt" in generated
    assert "soc_module.cpp" in generated
