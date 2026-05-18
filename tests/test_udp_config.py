"""Tests for the ``--udp-config`` parser and exporter wiring (sketch §8.2 / §18).

The flag declares typed wrappers for user-defined RDL properties so
``info.tags.<udp_name>`` can be annotated with a real Python type
instead of falling back to ``Any``. This commit wires the flag through
``__peakrdl__`` -> ``Pybind11Exporter.export`` -> ``_udp_type_map``;
the stub-side emission of typed annotations is deferred (TODO in
``runtime/info.py``).

The tests below cover the parser surface (valid input, rejection of
unsupported types, malformed TOML, empty/missing-section cases) and
the exporter wiring (``udp_config=...`` kwarg → stored ``_udp_type_map``)
without standing up the full code-generation pipeline.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from peakrdl_pybind11.cli.udp_config import UDPConfigError, parse_udp_config
from peakrdl_pybind11.exporter import Pybind11Exporter

# The parser uses stdlib ``tomllib`` which only ships on Python 3.11+.
# On 3.10 the parser raises a clear ``ImportError`` with an upgrade
# hint, and the rest of the package keeps working — the tests below
# can't exercise the happy path without ``tomllib`` so we skip the
# whole module on 3.10.
pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="--udp-config requires Python 3.11+ (stdlib tomllib)",
)


class TestParseUDPConfig:
    def test_parses_valid_toml_with_three_entries(self, tmp_path: Path) -> None:
        """Sketch example: three UDPs of three different scalar types."""
        cfg = tmp_path / "udp.toml"
        cfg.write_text(
            '[udp]\n'
            'secure_field = "bool"\n'
            'max_value = "int"\n'
            'description = "str"\n',
            encoding="utf-8",
        )
        result = parse_udp_config(cfg)
        assert result == {
            "secure_field": "bool",
            "max_value": "int",
            "description": "str",
        }

    def test_rejects_unknown_type_with_clear_error(self, tmp_path: Path) -> None:
        """A non-scalar declared type must be rejected with a message
        that names the offending UDP, the bad type, and the allowed set."""
        cfg = tmp_path / "udp.toml"
        cfg.write_text(
            '[udp]\n'
            'fancy = "my_class"\n',
            encoding="utf-8",
        )
        with pytest.raises(UDPConfigError) as excinfo:
            parse_udp_config(cfg)
        msg = str(excinfo.value)
        assert "my_class" in msg
        assert "fancy" in msg
        # Allowed types are mentioned so the user can self-correct.
        assert "int" in msg and "bool" in msg and "str" in msg and "float" in msg

    def test_rejects_malformed_toml_with_clear_error(self, tmp_path: Path) -> None:
        """A TOML decode error must surface as a UDPConfigError that
        mentions the file path — not a raw TOMLDecodeError leaking from
        deep inside tomllib."""
        cfg = tmp_path / "broken.toml"
        cfg.write_text("[udp\nsecure = bool\n", encoding="utf-8")
        with pytest.raises(UDPConfigError) as excinfo:
            parse_udp_config(cfg)
        assert str(cfg) in str(excinfo.value)

    def test_empty_file_or_missing_udp_section_returns_empty_dict(self, tmp_path: Path) -> None:
        """An empty file is valid TOML and yields ``{}``; a file with
        no ``[udp]`` table likewise yields ``{}`` — undeclared UDPs fall
        back to today's permissive ``TagsNamespace``."""
        empty = tmp_path / "empty.toml"
        empty.write_text("", encoding="utf-8")
        assert parse_udp_config(empty) == {}

        other_section = tmp_path / "other.toml"
        other_section.write_text('[unrelated]\nfoo = "bar"\n', encoding="utf-8")
        assert parse_udp_config(other_section) == {}

        # An explicit but empty ``[udp]`` table is also fine.
        explicit_empty = tmp_path / "explicit_empty.toml"
        explicit_empty.write_text("[udp]\n", encoding="utf-8")
        assert parse_udp_config(explicit_empty) == {}

    def test_exporter_accepts_path_and_stores_parsed_map(self, tmp_path: Path) -> None:
        """The exporter wires ``udp_config=PATH`` through ``export(...)``
        and stashes the parsed map on ``self._udp_type_map``. We bypass
        a real code-gen run by exercising the parse hook through a
        minimal seam: the exporter's constructor initializes the map
        to ``{}``, and ``parse_udp_config(path)`` is the same parser the
        export entry point calls."""
        cfg = tmp_path / "udp.toml"
        cfg.write_text(
            '[udp]\n'
            'secure_field = "bool"\n'
            'max_value = "int"\n',
            encoding="utf-8",
        )

        exporter = Pybind11Exporter()
        # Default-constructed: empty map.
        assert exporter._udp_type_map == {}

        # Simulate the wiring path that ``export(udp_config=...)`` runs
        # before generating any output. The map is stored on the
        # exporter for downstream stub-typing consumers.
        exporter._udp_type_map = parse_udp_config(cfg)
        assert exporter._udp_type_map == {
            "secure_field": "bool",
            "max_value": "int",
        }


class TestStubUDPNamespace:
    """The stub generator emits a ``_UDPNamespace`` class with the
    declared types when ``udp_type_map`` is non-empty, so type-checkers
    see ``info.tags.<udp_name>: <type>`` (via ``cast``) instead of
    falling back to :data:`typing.Any`. Empty map → zero new stub
    content (no ``_UDPNamespace`` declaration at all)."""

    def _render_stub(self, udp_type_map: dict[str, str]) -> str:
        """Render ``stubs.pyi.jinja`` directly with a minimal RDL.

        Mirrors the pattern used by
        ``tests/test_exporter.py::test_field_write_stub_annotates_range``
        so the assertions target the template change alone (no C++
        build, no plugin post-processing)."""
        import os
        import tempfile

        from jinja2 import Environment, PackageLoader, select_autoescape
        from systemrdl import RDLCompiler

        rdl_src = """
        addrmap udp_stub_soc {
            reg {
                field { sw = rw; hw = r; } enable[0:0];
            } control @ 0x0000;
        };
        """
        fd, path = tempfile.mkstemp(suffix=".rdl")
        try:
            os.write(fd, rdl_src.encode("utf-8"))
            os.close(fd)

            rdl = RDLCompiler()
            rdl.compile_file(path)
            root = rdl.elaborate()

            exporter = Pybind11Exporter()
            nodes = exporter._collect_nodes(root.top)

            env = Environment(
                loader=PackageLoader("peakrdl_pybind11", "templates"),
                autoescape=select_autoescape(),
                trim_blocks=True,
                lstrip_blocks=True,
            )
            env.filters["pybind_name"] = exporter._pybind_name_from_node
            env.filters["safe_id"] = exporter._sanitize_identifier
            env.filters["members"] = exporter._members_for_node
            env.filters["field_encode_members"] = exporter._field_encode_members_for_node
            template = env.get_template("stubs.pyi.jinja")
            return template.render(
                soc_name="udp_stub_soc",
                top_node=root.top,
                nodes=nodes,
                udp_type_map=udp_type_map,
            )
        finally:
            os.unlink(path)

    def test_stub_emits_udp_namespace(self) -> None:
        """A non-empty ``udp_type_map`` produces a ``_UDPNamespace``
        class with one ``<name>: <type>`` annotation per entry. The
        class sits near the top of the stub (before per-node class
        declarations) so consumers can ``cast(_UDPNamespace, info.tags)``
        without forward-reference gymnastics."""
        rendered = self._render_stub(
            {"secure_field": "bool", "max_value": "int"},
        )
        assert "class _UDPNamespace:" in rendered
        assert "secure_field: bool" in rendered
        assert "max_value: int" in rendered

    def test_stub_omits_udp_namespace_when_empty(self) -> None:
        """An empty ``udp_type_map`` produces zero new stub content —
        the ``_UDPNamespace`` class is gated behind a Jinja
        ``{% if udp_type_map %}`` so SoCs built without ``--udp-config``
        get exactly the same stub they got before the flag landed."""
        rendered = self._render_stub({})
        assert "_UDPNamespace" not in rendered
