"""Unit tests for ``peakrdl_pybind11.runtime.observers`` (Unit 7).

The tests run entirely against a Python ``MockMaster`` standing in for the
generated SoC. They cover the four behaviours called out in the task:

1. A read observer fires with a fully-populated :class:`Event` after the
   master returns.
2. ``where="uart.*"`` only routes matching paths to the hook.
3. ``with soc.observe() as obs:`` captures both reads and writes; the
   resulting :class:`CoverageReport` lists the touched paths.
4. Removing an observer stops further events.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from peakrdl_pybind11.masters.mock import MockMaster
from peakrdl_pybind11.runtime.observers import (
    CoverageReport,
    Event,
    ObserverChain,
    ObserverScope,
    register_master_extension,
    register_post_create,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeSoc:
    """Minimal stand-in for the generated ``Soc``. Carries a master and an
    address->path table; uses :func:`register_master_extension` for wiring.
    """

    def __init__(self, address_map: dict[int, str] | None = None) -> None:
        self.master = MockMaster()
        self._address_map: dict[int, str] = dict(address_map or {})

    def _resolve(self, address: int, op: str) -> str:  # noqa: ARG002
        return self._address_map.get(address, "")


@pytest.fixture
def soc() -> _FakeSoc:
    """A SoC pre-populated with three named registers."""
    return _FakeSoc(
        address_map={
            0x00: "uart.control",
            0x04: "uart.status",
            0x08: "spi.control",
        }
    )


@pytest.fixture
def chain() -> ObserverChain:
    return ObserverChain()


@pytest.fixture
def wired_soc(soc: _FakeSoc, chain: ObserverChain) -> _FakeSoc:
    """SoC with the observer chain wired into both seams."""
    register_master_extension(
        soc.master, chain, path_resolver=soc._resolve
    )
    register_post_create(soc, chain)
    return soc


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------


class TestEvent:
    def test_event_is_frozen(self) -> None:
        evt = Event(path="x", address=0, value=0, op="read", timestamp=0.0)
        with pytest.raises((AttributeError, TypeError)):
            evt.value = 1  # type: ignore[misc]

    def test_event_uses_slots(self) -> None:
        evt = Event(path="x", address=0, value=0, op="read", timestamp=0.0)
        # frozen+slots means no __dict__ and no ad-hoc attribute assignment.
        assert not hasattr(evt, "__dict__")
        with pytest.raises((AttributeError, TypeError)):
            evt.extra = 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Read observer fires after a real read
# ---------------------------------------------------------------------------


class TestReadObserver:
    def test_read_callback_fires_with_correct_fields(
        self, wired_soc: _FakeSoc, chain: ObserverChain
    ) -> None:
        captured: list[Event] = []
        chain.add_read(captured.append)

        # Seed the mock so the read returns something distinctive.
        wired_soc.master._observer_inner_write(0x00, 0xDEAD_BEEF, 4)

        before = time.monotonic()
        value = wired_soc.master.read(0x00, 4)
        after = time.monotonic()

        assert value == 0xDEAD_BEEF
        assert len(captured) == 1
        evt = captured[0]
        assert evt.path == "uart.control"
        assert evt.address == 0x00
        assert evt.value == 0xDEAD_BEEF
        assert evt.op == "read"
        assert before <= evt.timestamp <= after

    def test_write_does_not_trigger_read_observer(
        self, wired_soc: _FakeSoc, chain: ObserverChain
    ) -> None:
        reads: list[Event] = []
        chain.add_read(reads.append)
        wired_soc.master.write(0x00, 0xCAFE, 4)
        assert reads == []

    def test_write_observer_fires_with_correct_fields(
        self, wired_soc: _FakeSoc, chain: ObserverChain
    ) -> None:
        captured: list[Event] = []
        chain.add_write(captured.append)
        wired_soc.master.write(0x04, 0x55AA, 4)
        assert len(captured) == 1
        evt = captured[0]
        assert evt.op == "write"
        assert evt.path == "uart.status"
        assert evt.address == 0x04
        assert evt.value == 0x55AA

    def test_value_reflects_master_return_not_input(
        self, wired_soc: _FakeSoc, chain: ObserverChain
    ) -> None:
        # Mock master masks reads to width; if a wider value is somehow stored,
        # the hook must see the masked value the master actually returned.
        events: list[Event] = []
        chain.add_read(events.append)
        wired_soc.master._observer_inner_write(0x00, 0x1_0000_FFFF & 0xFFFF_FFFF, 4)
        wired_soc.master.read(0x00, 4)
        assert events[0].value == wired_soc.master._observer_inner_read(0x00, 4)


# ---------------------------------------------------------------------------
# Filtering via ``where=``
# ---------------------------------------------------------------------------


class TestWhereFilter:
    def test_glob_matches_only_matching_paths(
        self, wired_soc: _FakeSoc, chain: ObserverChain
    ) -> None:
        uart_events: list[Event] = []
        chain.add_read(uart_events.append, where="uart.*")

        wired_soc.master.read(0x00, 4)  # uart.control -> matches
        wired_soc.master.read(0x04, 4)  # uart.status  -> matches
        wired_soc.master.read(0x08, 4)  # spi.control  -> filtered out

        paths = [e.path for e in uart_events]
        assert paths == ["uart.control", "uart.status"]

    def test_glob_filter_applies_per_op(
        self, wired_soc: _FakeSoc, chain: ObserverChain
    ) -> None:
        writes: list[Event] = []
        chain.add_write(writes.append, where="spi.*")
        wired_soc.master.write(0x00, 1, 4)  # uart.control (no)
        wired_soc.master.write(0x08, 2, 4)  # spi.control (yes)
        assert [e.path for e in writes] == ["spi.control"]

    def test_callable_predicate_is_supported(
        self, wired_soc: _FakeSoc, chain: ObserverChain
    ) -> None:
        captured: list[Event] = []
        chain.add_read(captured.append, where=lambda evt: evt.address >= 0x04)
        wired_soc.master.read(0x00, 4)
        wired_soc.master.read(0x04, 4)
        wired_soc.master.read(0x08, 4)
        assert [e.address for e in captured] == [0x04, 0x08]

    def test_invalid_where_type_raises(self, chain: ObserverChain) -> None:
        with pytest.raises(TypeError):
            chain.add_read(lambda _: None, where=123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Scoped ``soc.observe()`` context manager
# ---------------------------------------------------------------------------


class TestObserveContext:
    def test_observe_captures_reads_and_writes(self, wired_soc: _FakeSoc) -> None:
        with wired_soc.observe() as obs:
            wired_soc.master.write(0x00, 0xAAAA, 4)
            wired_soc.master.read(0x00, 4)
            wired_soc.master.read(0x04, 4)
            wired_soc.master.write(0x08, 0xBBBB, 4)

        assert isinstance(obs, ObserverScope)
        ops = [(e.op, e.path) for e in obs.events]
        assert ops == [
            ("write", "uart.control"),
            ("read", "uart.control"),
            ("read", "uart.status"),
            ("write", "spi.control"),
        ]

    def test_coverage_report_summary(self, wired_soc: _FakeSoc) -> None:
        with wired_soc.observe() as obs:
            wired_soc.master.read(0x00, 4)
            wired_soc.master.read(0x00, 4)
            wired_soc.master.read(0x04, 4)
            wired_soc.master.write(0x08, 0xBEEF, 4)

        report = obs.coverage_report()
        assert isinstance(report, CoverageReport)
        assert report.nodes_read == frozenset({"uart.control", "uart.status"})
        assert report.nodes_written == frozenset({"spi.control"})
        assert report.total_reads == 3
        assert report.total_writes == 1
        # uart.control is read twice, so it ranks first; ties break alphabetical.
        assert report.paths_by_frequency[0] == ("uart.control", 2)
        # All paths show up in the frequency table.
        assert {p for p, _ in report.paths_by_frequency} == {
            "uart.control",
            "uart.status",
            "spi.control",
        }

    def test_observe_detaches_on_exit(self, wired_soc: _FakeSoc) -> None:
        chain: ObserverChain = wired_soc.observers  # type: ignore[attr-defined]
        with wired_soc.observe():
            wired_soc.master.read(0x00, 4)
        # After the block, the scope's hooks should be gone.
        assert chain.read_hooks == ()
        assert chain.write_hooks == ()

    def test_observe_detaches_even_on_exception(
        self, wired_soc: _FakeSoc
    ) -> None:
        chain: ObserverChain = wired_soc.observers  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError):
            with wired_soc.observe():
                wired_soc.master.read(0x00, 4)
                raise RuntimeError("boom")
        assert chain.read_hooks == ()
        assert chain.write_hooks == ()


# ---------------------------------------------------------------------------
# Removal stops further events
# ---------------------------------------------------------------------------


class TestRemoval:
    def test_remove_read_stops_further_events(
        self, wired_soc: _FakeSoc, chain: ObserverChain
    ) -> None:
        captured: list[Event] = []

        def hook(evt: Event) -> None:
            captured.append(evt)

        chain.add_read(hook)
        wired_soc.master.read(0x00, 4)
        assert len(captured) == 1

        removed = chain.remove_read(hook)
        assert removed is True

        wired_soc.master.read(0x00, 4)
        assert len(captured) == 1  # no growth after removal

    def test_remove_write_stops_further_events(
        self, wired_soc: _FakeSoc, chain: ObserverChain
    ) -> None:
        captured: list[Event] = []

        def hook(evt: Event) -> None:
            captured.append(evt)

        chain.add_write(hook)
        wired_soc.master.write(0x00, 1, 4)
        assert len(captured) == 1
        chain.remove_write(hook)
        wired_soc.master.write(0x00, 2, 4)
        assert len(captured) == 1

    def test_removing_unknown_hook_returns_false(
        self, chain: ObserverChain
    ) -> None:
        assert chain.remove_read(lambda _evt: None) is False
        assert chain.remove_write(lambda _evt: None) is False

    def test_remove_only_strips_first_occurrence(
        self, wired_soc: _FakeSoc, chain: ObserverChain
    ) -> None:
        captured: list[Event] = []

        def hook(evt: Event) -> None:
            captured.append(evt)

        chain.add_read(hook)
        chain.add_read(hook)
        wired_soc.master.read(0x00, 4)
        assert len(captured) == 2  # both subscriptions fire

        chain.remove_read(hook)
        wired_soc.master.read(0x00, 4)
        assert len(captured) == 3  # one survivor still fires


# ---------------------------------------------------------------------------
# Wiring helpers (the seam used by Unit 1's `_registry`)
# ---------------------------------------------------------------------------


class TestRegistrationHelpers:
    def test_register_master_extension_is_idempotent(self) -> None:
        master = MockMaster()
        chain = ObserverChain()
        register_master_extension(master, chain)
        first_read = master.read
        register_master_extension(master, chain)
        # Still wrapped, but rebinding doesn't add another layer:
        # the inner methods were stashed once and restored on rebind.
        captured: list[Event] = []
        chain.add_read(captured.append)
        master.read(0x00, 4)
        assert len(captured) == 1, "second wrap stacked an extra dispatcher"
        # And the wrapper is callable as before.
        assert callable(first_read)

    def test_register_post_create_attaches_chain_and_observe(self) -> None:
        soc: Any = type("S", (), {})()
        chain = register_post_create(soc)
        assert isinstance(chain, ObserverChain)
        assert soc.observers is chain
        # observe() yields a fresh scope each time.
        with soc.observe() as a, soc.observe() as b:
            assert a is not b

    def test_register_post_create_reuses_existing_chain(self) -> None:
        soc: Any = type("S", (), {})()
        first = register_post_create(soc)
        again = register_post_create(soc)
        assert first is again

    def test_no_hooks_means_no_event_objects_built(
        self, wired_soc: _FakeSoc, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sanity check the documented zero-cost guarantee: with no hooks
        # registered, the chain must short-circuit before constructing an
        # Event. We monkeypatch Event.__init__ to fail loudly.
        from peakrdl_pybind11.runtime import observers as obs_mod

        sentinel: list[None] = []

        class _Tripwire:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                sentinel.append(None)

        # Replace the dataclass with a tripwire only for the duration of
        # the no-hook scenario.
        monkeypatch.setattr(obs_mod, "Event", _Tripwire)
        wired_soc.master.read(0x00, 4)
        wired_soc.master.write(0x00, 1, 4)
        assert sentinel == [], "Event constructed despite an empty chain"
