"""Tests for :mod:`peakrdl_pybind11.runtime.signals`.

The signal-attachment runtime is pure Python and operates against any
duck-typed SoC tree, so these tests build hand-rolled fakes rather than
compiling C++. The exporter-rendering test runs the Jinja template
directly (no cmake) to confirm the generated ``runtime.py`` carries the
expected ``_SIGNAL_METADATA`` entries.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from peakrdl_pybind11.runtime import _registry
from peakrdl_pybind11.runtime.routing import _kind_for, attach_discovery
from peakrdl_pybind11.runtime.signals import (
    Signal,
    _attach_signals,
    attach_to,
    register_signals,
)


# ---------------------------------------------------------------------------
# Hand-rolled fake SoC. Mirrors the ``_DiscSoC`` pattern in
# ``test_routing.py``: plain Python classes with vars()-visible
# attributes so the discovery walk sees them. No bus access methods —
# Signals don't need them.
# ---------------------------------------------------------------------------
class _FakeUart:
    def __init__(self) -> None:
        self.name = "uart"


class _FakeSoC:
    def __init__(self) -> None:
        self.name = "soc"
        self.uart = _FakeUart()


# ---------------------------------------------------------------------------
# Dataclass smoke test
# ---------------------------------------------------------------------------
class TestSignalDataclass:
    def test_constructs_with_required_fields(self) -> None:
        sig = Signal(name="rst", path="rst", width=1)
        assert sig.name == "rst"
        assert sig.path == "rst"
        assert sig.width == 1
        assert sig.lsb == 0
        assert sig.external is False
        assert sig.description is None
        assert sig.tags == {}

    def test_signal_is_frozen(self) -> None:
        sig = Signal(name="rst", path="rst", width=1)
        with pytest.raises(Exception):  # FrozenInstanceError on dataclass
            sig.name = "other"  # type: ignore[misc]

    def test_value_equality(self) -> None:
        a = Signal(name="rst", path="rst", width=1, external=True)
        b = Signal(name="rst", path="rst", width=1, external=True)
        assert a == b
        # Frozen dataclasses are hashable when all fields are hashable;
        # ``tags`` defaults to ``dict`` (unhashable), so we don't rely
        # on ``hash(sig)`` — equality alone powers the idempotency check.
        assert a is not b

    def test_tags_round_trip(self) -> None:
        sig = Signal(
            name="cpuif_reset",
            path="cpuif_reset",
            width=1,
            tags={"activehigh": True, "owner": "soc_top"},
        )
        assert sig.tags == {"activehigh": True, "owner": "soc_top"}


# ---------------------------------------------------------------------------
# attach_to(): walk the dotted path and assign on the parent
# ---------------------------------------------------------------------------
class TestAttachTo:
    def test_top_level_signal_attaches(self) -> None:
        soc = _FakeSoC()
        sig = Signal(name="rst", path="rst", width=1, external=True)
        attach_to(soc, "rst", sig)
        assert soc.rst is sig  # type: ignore[attr-defined]

    def test_nested_signal_attaches(self) -> None:
        soc = _FakeSoC()
        sig = Signal(name="tx_signal", path="uart.tx_signal", width=1)
        attach_to(soc, "uart.tx_signal", sig)
        assert soc.uart.tx_signal is sig  # type: ignore[attr-defined]

    def test_missing_parent_path_raises(self) -> None:
        soc = _FakeSoC()
        sig = Signal(name="tx", path="missing.tx", width=1)
        with pytest.raises(AttributeError):
            attach_to(soc, "missing.tx", sig)

    def test_idempotent_on_value_equality(self) -> None:
        """Re-attaching the same signal is a no-op.

        Two Signals with identical fields compare equal (frozen
        dataclass ``__eq__``). ``attach_to`` checks the leaf and skips
        the assignment when the existing value is equal — so the
        attribute identity stays pinned to the first instance.
        """

        soc = _FakeSoC()
        first = Signal(name="rst", path="rst", width=1, external=True)
        attach_to(soc, "rst", first)
        assert soc.rst is first  # type: ignore[attr-defined]

        # A separate but equal Signal: idempotent skip keeps the
        # original instance bound to soc.rst.
        same = Signal(name="rst", path="rst", width=1, external=True)
        attach_to(soc, "rst", same)
        assert soc.rst is first  # type: ignore[attr-defined]

    def test_replaces_when_value_differs(self) -> None:
        """A Signal with different fields replaces the existing one."""

        soc = _FakeSoC()
        first = Signal(name="rst", path="rst", width=1, external=False)
        attach_to(soc, "rst", first)
        replacement = Signal(name="rst", path="rst", width=1, external=True)
        attach_to(soc, "rst", replacement)
        assert soc.rst is replacement  # type: ignore[attr-defined]

    def test_empty_path_raises(self) -> None:
        soc = _FakeSoC()
        with pytest.raises(ValueError):
            attach_to(soc, "", Signal(name="x", path="x", width=1))


# ---------------------------------------------------------------------------
# Discovery integration: soc.walk(kind="signal") yields Signal instances.
# ---------------------------------------------------------------------------
class TestDiscoveryWalk:
    def test_kind_for_returns_signal(self) -> None:
        sig = Signal(name="rst", path="rst", width=1)
        assert _kind_for(sig) == "Signal"

    def test_signal_not_misclassified_as_field(self) -> None:
        """Signal.lsb shouldn't make `_kind_for` return 'Field'."""

        # A Signal has ``lsb`` as part of its dataclass schema — the
        # routing duck-typed classifier looks for ``lsb`` to identify
        # Field nodes, so the Signal-isinstance branch must run first.
        sig = Signal(name="rst", path="rst", width=1, lsb=2)
        assert _kind_for(sig) == "Signal"

    def test_walk_signal_yields_attached_signals(self) -> None:
        soc = _FakeSoC()
        attach_discovery(soc)

        rst = Signal(name="rst", path="rst", width=1, external=True)
        tx = Signal(name="tx_signal", path="uart.tx_signal", width=1)
        attach_to(soc, "rst", rst)
        attach_to(soc, "uart.tx_signal", tx)

        walked = list(soc.walk(kind="signal"))  # type: ignore[attr-defined]
        assert rst in walked
        assert tx in walked
        # Nothing else should be classified as a signal.
        assert all(isinstance(n, Signal) for n in walked)


# ---------------------------------------------------------------------------
# Post-create hook wiring + registry
# ---------------------------------------------------------------------------
class TestPostCreateHook:
    def test_attach_signals_registered_as_post_create_hook(self) -> None:
        hooks = _registry.get_post_create_hooks()
        assert _attach_signals in hooks

    def test_attach_signals_strips_root_segment(self) -> None:
        """The hook strips the leading SoC-root path segment."""

        class _SoCWithChildren:
            def __init__(self) -> None:
                self.uart = _FakeUart()

        soc = _SoCWithChildren()
        # Register entries against this concrete class. The path
        # carries the SoC-root segment; the hook strips it before
        # walking.
        register_signals(
            type(soc),
            [
                ("my_soc.uart.tx_signal", {
                    "name": "tx_signal",
                    "width": 1,
                    "lsb": 0,
                    "external": False,
                    "description": None,
                    "tags": {},
                }),
            ],
        )
        _attach_signals(soc)
        assert isinstance(soc.uart.tx_signal, Signal)  # type: ignore[attr-defined]
        # And the stored path is the root-stripped form (the user-facing
        # contract per the API sketch).
        assert soc.uart.tx_signal.path == "uart.tx_signal"  # type: ignore[attr-defined]

    def test_attach_signals_constructs_with_tags(self) -> None:
        """Metadata ``tags`` round-trip into the constructed Signal."""

        class _RootSoC:
            pass

        soc = _RootSoC()
        register_signals(
            type(soc),
            [
                ("top.rst", {
                    "name": "rst",
                    "width": 1,
                    "lsb": 0,
                    "external": True,
                    "description": "reset wire",
                    "tags": {"activehigh": True},
                }),
            ],
        )
        _attach_signals(soc)
        sig = soc.rst  # type: ignore[attr-defined]
        assert isinstance(sig, Signal)
        assert sig.name == "rst"
        assert sig.path == "rst"
        assert sig.width == 1
        assert sig.external is True
        assert sig.description == "reset wire"
        assert sig.tags == {"activehigh": True}

    def test_attach_signals_no_op_when_no_entries(self) -> None:
        """SoC classes with no registered signals don't crash the hook."""

        class _Empty:
            pass

        # Must not raise even though no register_signals() call was
        # made for this class.
        _attach_signals(_Empty())


# ---------------------------------------------------------------------------
# Exporter-rendering integration: drive runtime.py.jinja with a small RDL
# that includes signals and confirm the generated module carries the
# expected metadata block. No cmake / no C++.
# ---------------------------------------------------------------------------
_SIGNAL_RDL = """
property owner { type = string; component = signal; };

addrmap signal_soc {
    name = "Signal SoC";
    desc = "Tests that signals reach the generated runtime.py";

    signal {
        signalwidth = 1;
        desc = "top-level reset";
        owner = "soc_top";
    } cpuif_reset;

    addrmap {
        name = "uart";
        signal { signalwidth = 1; activehigh = true; } tx_signal;
        reg {
            field { sw = rw; hw = r; } enable[0:0];
        } ctrl @ 0;
    } uart @ 0x100;
};
"""


def _write_rdl(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".rdl")
    os.write(fd, content.encode("utf-8"))
    os.close(fd)
    return path


class TestExporterRendering:
    def test_runtime_template_emits_signal_metadata(self) -> None:
        """``runtime.py.jinja`` renders a ``_SIGNAL_METADATA`` block."""

        try:
            from jinja2 import Environment, PackageLoader, select_autoescape
            from systemrdl import RDLCompiler

            from peakrdl_pybind11.exporter import Pybind11Exporter
        except ImportError as exc:
            pytest.skip(f"required import unavailable: {exc}")

        rdl_path = _write_rdl(_SIGNAL_RDL)
        rdlc = RDLCompiler()
        Pybind11Exporter.register_udps(rdlc)
        rdlc.compile_file(rdl_path)
        root = rdlc.elaborate()

        exporter = Pybind11Exporter()
        nodes = exporter._collect_nodes(root.top)

        # Sanity: collector found both signals.
        signal_paths = {sig.get_path() for sig in nodes["signals"]}
        assert signal_paths == {
            "signal_soc.cpuif_reset",
            "signal_soc.uart.tx_signal",
        }

        # Render the template the same way the exporter does at export
        # time. We use the exporter's own ``env`` so filter wiring
        # (``python_string``, ``safe_id``, etc.) is in place.
        template = exporter.env.get_template("runtime.py.jinja")
        rendered = template.render(
            soc_name="signal_soc",
            top_node=root.top,
            nodes=nodes,
            strict_fields=True,
        )

        # The metadata block is present and includes both signal paths.
        assert "_SIGNAL_METADATA: list[tuple[str, dict]] = [" in rendered
        assert "'signal_soc.cpuif_reset'" in rendered
        assert "'signal_soc.uart.tx_signal'" in rendered
        # Per-signal payload survived the round-trip — the cpuif_reset's
        # description and UDP owner end up in the rendered template.
        assert "'top-level reset'" in rendered
        assert "'owner'" in rendered
        # The post-create wiring call is emitted so a hot reload of the
        # generated module re-registers the entries.
        assert "_peakrdl_signals.register_signals(signal_soc_t, _SIGNAL_METADATA)" in rendered

    def test_runtime_template_handles_no_signals(self) -> None:
        """Templates with zero signals render NO signal block at all.

        Skipping the block when there are no signals keeps the runtime
        prologue free of references to the SoC type — that matters for
        isolated-render tests in ``test_cli_subcommands`` which exec the
        prologue without a real native import."""

        try:
            from systemrdl import RDLCompiler

            from peakrdl_pybind11.exporter import Pybind11Exporter
        except ImportError as exc:
            pytest.skip(f"required import unavailable: {exc}")

        no_signal_rdl = """
        addrmap empty_soc {
            reg { field { sw=rw; hw=r; } e[0:0]; } r0 @ 0;
        };
        """
        rdl_path = _write_rdl(no_signal_rdl)
        rdlc = RDLCompiler()
        Pybind11Exporter.register_udps(rdlc)
        rdlc.compile_file(rdl_path)
        root = rdlc.elaborate()

        exporter = Pybind11Exporter()
        nodes = exporter._collect_nodes(root.top)
        assert nodes["signals"] == []

        template = exporter.env.get_template("runtime.py.jinja")
        rendered = template.render(
            soc_name="empty_soc",
            top_node=root.top,
            nodes=nodes,
            strict_fields=True,
        )
        # Block omitted when there are zero signals — neither the import
        # nor the metadata list nor the wiring call appears.
        assert "_SIGNAL_METADATA" not in rendered
        assert "register_signals" not in rendered
        assert "peakrdl_pybind11.runtime import signals" not in rendered
