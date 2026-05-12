"""Unit-19 tests for :mod:`peakrdl_pybind11.runtime.routing`.

The router is exercised against a hand-rolled fake node tree (plain
dataclasses) so the tests don't depend on the exporter or any sibling
unit. Each test asserts the public contract spelled out in the API
sketch §13.1: where=glob, where=range, where=predicate, miss raises,
most-specific match wins.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import pytest

from peakrdl_pybind11.runtime import _registry
from peakrdl_pybind11.runtime.routing import (
    Router,
    RoutingError,
    attach_master,
    attach_router,
)


# ---------------------------------------------------------------------------
# Test scaffolding: minimal node + master fakes.
# ---------------------------------------------------------------------------
@dataclass
class FakeInfo:
    path: str
    address: int
    size: int
    is_external: bool = False


@dataclass
class FakeNode:
    info: FakeInfo
    children: list[FakeNode] = field(default_factory=list)

    def walk(self) -> Iterable[FakeNode]:
        # NodeLike contract: yield self + every descendant.
        yield self
        for child in self.children:
            yield from child.walk()


class RecordingMaster:
    """Captures every read/write so tests can assert on dispatch."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, int, int]] = []

    def read(self, address: int, width: int = 4) -> int:
        self.reads.append((address, width))
        # Encode the master identity in the readback so tests can
        # assert on it without checking ``self.reads``.
        return (id(self) & 0xFFFF_0000) | (address & 0xFFFF)

    def write(self, address: int, value: int, width: int = 4) -> None:
        self.writes.append((address, value, width))

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"RecordingMaster({self.name!r})"


# ---------------------------------------------------------------------------
# A small SoC tree the tests reuse:
#   soc                       0x4000_0000 (size 0x1_0000)
#   ├── uart                  0x4000_1000 (size 0x100)
#   │   ├── control           0x4000_1000 (size 4)
#   │   └── data              0x4000_1004 (size 4)
#   ├── ram                   0x4000_2000 (size 0x1000)
#   │   └── word0             0x4000_2000 (size 4)
#   └── ext_block (external)  0x4000_5000 (size 0x10)
# ---------------------------------------------------------------------------
def make_soc() -> FakeNode:
    uart_control = FakeNode(FakeInfo("uart.control", 0x4000_1000, 4))
    uart_data = FakeNode(FakeInfo("uart.data", 0x4000_1004, 4))
    uart = FakeNode(
        FakeInfo("uart", 0x4000_1000, 0x100),
        children=[uart_control, uart_data],
    )

    ram_word = FakeNode(FakeInfo("ram[0]", 0x4000_2000, 4))
    ram = FakeNode(
        FakeInfo("ram", 0x4000_2000, 0x1000),
        children=[ram_word],
    )

    ext = FakeNode(FakeInfo("ext_block", 0x4000_5000, 0x10, is_external=True))
    soc = FakeNode(
        FakeInfo("soc", 0x4000_0000, 0x1_0000),
        children=[uart, ram, ext],
    )
    return soc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_glob_dispatch_uart_vs_ram() -> None:
    """`attach_master(jtag, where="uart.*")` and a separate `where="ram"`
    rule must route correctly."""

    soc = make_soc()
    jtag = RecordingMaster("jtag")
    mem = RecordingMaster("mem")

    router = Router()
    router.attach_master(jtag, where="uart.*", soc=soc)
    router.attach_master(mem, where="ram", soc=soc)

    # Read on uart.control goes to jtag.
    router.read(0x4000_1000)
    assert jtag.reads == [(0x4000_1000, 4)]
    assert mem.reads == []

    # Read on ram[0] goes to mem.
    router.read(0x4000_2000)
    assert mem.reads == [(0x4000_2000, 4)]

    # Writes route the same way.
    router.write(0x4000_1004, 0xC0FFEE)
    assert jtag.writes == [(0x4000_1004, 0xC0FFEE, 4)]


def test_address_range_routing() -> None:
    """`where=(start, end)` half-open range matches addresses in range."""

    soc = make_soc()
    inside = RecordingMaster("inside")
    fallback = RecordingMaster("fallback")

    router = Router(default_master=fallback)
    router.attach_master(inside, where=(0x4000_0000, 0x4001_0000))

    # Inside [start, end): range master.
    router.read(0x4000_0000)
    router.read(0x4000_FFFC)
    assert len(inside.reads) == 2
    assert fallback.reads == []

    # `end` is exclusive — addr == end falls through to default.
    router.read(0x4001_0000)
    assert fallback.reads == [(0x4001_0000, 4)]


def test_predicate_routing_is_external() -> None:
    """`where=lambda n: n.info.is_external` matches external nodes."""

    soc = make_soc()
    ext_master = RecordingMaster("ext")
    default = RecordingMaster("default")

    router = Router(default_master=default)
    router.attach_master(
        ext_master,
        where=lambda n: n.info.is_external,
        soc=soc,
    )

    # ext_block is external — routes to ext_master.
    router.read(0x4000_5000)
    assert ext_master.reads == [(0x4000_5000, 4)]
    assert default.reads == []

    # uart.control isn't — falls back to default.
    router.read(0x4000_1000)
    assert default.reads == [(0x4000_1000, 4)]


def test_routing_miss_raises() -> None:
    """No matching rule and no catch-all -> RoutingError with the addr."""

    router = Router()  # no default
    router.attach_master(
        RecordingMaster("range"),
        where=(0x4000_0000, 0x4000_1000),
    )

    with pytest.raises(RoutingError) as exc_info:
        router.read(0x9999_9999)

    assert exc_info.value.address == 0x9999_9999
    msg = str(exc_info.value)
    assert "no master attached" in msg
    assert "9999" in msg or "0x99999999" in msg


def test_most_specific_match_wins_overlapping_ranges() -> None:
    """Smaller range beats larger range when both contain the address."""

    soc = make_soc()
    wide = RecordingMaster("wide")
    narrow = RecordingMaster("narrow")

    router = Router()
    router.attach_master(wide, where=(0x4000_0000, 0x5000_0000))
    router.attach_master(narrow, where=(0x4000_1000, 0x4000_1100))

    # 0x4000_1000 is in both ranges; narrow wins.
    router.read(0x4000_1000)
    assert narrow.reads == [(0x4000_1000, 4)]
    assert wide.reads == []

    # 0x4000_2000 is only in the wide range.
    router.read(0x4000_2000)
    assert wide.reads == [(0x4000_2000, 4)]


def test_most_specific_match_wins_overlapping_globs() -> None:
    """Longer-prefix glob beats shorter-prefix glob (segment count)."""

    soc = make_soc()
    broad = RecordingMaster("broad")
    narrow = RecordingMaster("narrow")

    router = Router()
    router.attach_master(broad, where="*", soc=soc)
    router.attach_master(narrow, where="uart.*", soc=soc)

    router.read(0x4000_1000)  # matches uart.control via uart.* AND *
    assert narrow.reads == [(0x4000_1000, 4)]
    assert broad.reads == []


def test_explicit_beats_catch_all() -> None:
    """Any `where=` beats `where=None` (the default master)."""

    soc = make_soc()
    explicit = RecordingMaster("explicit")
    default = RecordingMaster("default")

    router = Router(default_master=default)
    router.attach_master(
        explicit,
        where=lambda n: n.info.path == "uart.control",
        soc=soc,
    )

    router.read(0x4000_1000)
    assert explicit.reads == [(0x4000_1000, 4)]
    assert default.reads == []


def test_first_registered_breaks_tie() -> None:
    """When two equally-specific rules match, the first one registered wins."""

    soc = make_soc()
    first = RecordingMaster("first")
    second = RecordingMaster("second")

    router = Router()
    router.attach_master(first, where=(0x4000_0000, 0x4001_0000))
    router.attach_master(second, where=(0x4000_0000, 0x4001_0000))

    router.read(0x4000_1000)
    assert first.reads == [(0x4000_1000, 4)]
    assert second.reads == []


def test_range_beats_glob_of_same_size() -> None:
    """An explicit range tuple is treated as more specific than a glob."""

    # uart subtree: 0x4000_1000..0x4000_1100 (size 0x100), same as the
    # range tuple below.
    soc = make_soc()
    range_master = RecordingMaster("range")
    glob_master = RecordingMaster("glob")

    router = Router()
    router.attach_master(glob_master, where="uart", soc=soc)
    router.attach_master(range_master, where=(0x4000_1000, 0x4000_1100))

    router.read(0x4000_1000)
    assert range_master.reads == [(0x4000_1000, 4)]
    assert glob_master.reads == []


def test_attach_master_helper_installs_router() -> None:
    """The free function replaces ``soc.master`` with a Router."""

    soc = make_soc()
    soc.master = RecordingMaster("default")  # type: ignore[attr-defined]
    explicit = RecordingMaster("explicit")

    router = attach_master(soc, explicit, where="uart.*")
    assert soc.master is router  # type: ignore[attr-defined]

    # Calling attach_master again reuses the existing router.
    second = RecordingMaster("second")
    router2 = attach_master(soc, second, where="ram")
    assert router2 is router

    # Routing still works through the installed router.
    router.read(0x4000_1000)
    assert explicit.reads == [(0x4000_1000, 4)]
    router.read(0x4000_2000)
    assert second.reads == [(0x4000_2000, 4)]


def test_glob_resolves_only_through_walk() -> None:
    """Glob requires soc=; without it, attach_master raises."""

    router = Router()
    with pytest.raises(ValueError, match="soc="):
        router.attach_master(RecordingMaster("x"), where="uart.*")


def test_predicate_requires_soc() -> None:
    """Same for predicate-form `where=`."""

    router = Router()
    with pytest.raises(ValueError, match="soc="):
        router.attach_master(
            RecordingMaster("x"),
            where=lambda n: True,
        )


def test_invalid_where_type_raises() -> None:
    """Anything other than None/str/tuple/callable is a TypeError."""

    router = Router()
    with pytest.raises(TypeError, match="where="):
        router.attach_master(RecordingMaster("x"), where=42)  # type: ignore[arg-type]


def test_range_must_be_non_empty() -> None:
    """Empty/inverted ranges are rejected at attach time."""

    router = Router()
    with pytest.raises(ValueError):
        router.attach_master(
            RecordingMaster("x"),
            where=(0x4000_1000, 0x4000_1000),  # end == start, half-open is empty
        )


def test_master_for_returns_master() -> None:
    """`master_for(addr)` exposes the resolution without a bus call."""

    soc = make_soc()
    explicit = RecordingMaster("explicit")
    router = Router()
    router.attach_master(explicit, where="uart.*", soc=soc)

    assert router.master_for(0x4000_1000) is explicit
    with pytest.raises(RoutingError):
        router.master_for(0x4000_2000)


def test_read_many_dispatches_per_address() -> None:
    """Each entry in a batched read is dispatched to its own master."""

    from peakrdl_pybind11.masters.base import AccessOp

    soc = make_soc()
    jtag = RecordingMaster("jtag")
    mem = RecordingMaster("mem")
    router = Router()
    router.attach_master(jtag, where="uart.*", soc=soc)
    router.attach_master(mem, where="ram", soc=soc)

    ops = [
        AccessOp(address=0x4000_1000, width=4),
        AccessOp(address=0x4000_2000, width=4),
        AccessOp(address=0x4000_1004, width=4),
    ]
    out = router.read_many(ops)
    assert len(out) == 3
    assert jtag.reads == [(0x4000_1000, 4), (0x4000_1004, 4)]
    assert mem.reads == [(0x4000_2000, 4)]


def test_routing_error_carries_address_attribute() -> None:
    """The exception object exposes `.address` programmatically."""

    err = RoutingError(0xCAFEBABE)
    assert err.address == 0xCAFEBABE
    assert "0xcafebabe" in str(err)


# ---------------------------------------------------------------------------
# Post-create hook wiring (Unit 9 wire-up).
# ---------------------------------------------------------------------------
class FakeSoCWithAttach:
    """Stand-in for a generated SoC.

    Mimics the C++ ``attach_master(Master*) -> None`` shape: the only
    thing the wrapper needs from the SoC is a callable ``attach_master``
    that takes a single positional master and rejects unknown kwargs.
    Each call records the master so tests can assert on what the C++
    side ultimately saw.
    """

    def __init__(self) -> None:
        self.attached_masters: list[object] = []

    def attach_master(self, master: object) -> None:
        self.attached_masters.append(master)


def test_attach_router_is_registered_as_post_create_hook() -> None:
    """``attach_router`` is wired into the post-create registry."""

    hooks = _registry.get_post_create_hooks()
    assert attach_router in hooks


def test_attach_router_lets_attach_master_accept_where_kwarg() -> None:
    """After the hook fires, ``soc.attach_master(m, where=...)`` no longer raises."""

    soc = FakeSoCWithAttach()
    attach_router(soc)

    master = RecordingMaster("m")
    # Without the wrapper this would raise TypeError because the
    # underlying ``attach_master`` only accepts the master positionally.
    soc.attach_master(master, where="uart.*")

    # The wrapper installed a Router, not the bare master, on the SoC.
    assert len(soc.attached_masters) == 1
    assert isinstance(soc.attached_masters[0], Router)


def test_attach_router_passthrough_when_where_is_none() -> None:
    """``where=None`` (the default) hits the original C++ method directly."""

    soc = FakeSoCWithAttach()
    attach_router(soc)

    master = RecordingMaster("m")
    soc.attach_master(master)

    # Passthrough: the SoC saw the bare master, not a Router.
    assert soc.attached_masters == [master]


def test_attach_router_routes_two_masters_by_address() -> None:
    """Two ``where=`` calls with non-overlapping ranges route correctly."""

    soc = FakeSoCWithAttach()
    attach_router(soc)

    uart = RecordingMaster("uart")
    ram = RecordingMaster("ram")

    # Use range tuples so the router doesn't need to walk the SoC tree;
    # the FakeSoC stand-in deliberately has no walk()/info surface.
    soc.attach_master(uart, where=(0x4000_1000, 0x4000_2000))
    soc.attach_master(ram, where=(0x4000_2000, 0x4000_3000))

    # Both attaches re-installed the router as the SoC's master.
    router = soc.attached_masters[-1]
    assert isinstance(router, Router)
    # The same router is reused across calls (no new instance per attach).
    assert all(m is router for m in soc.attached_masters)

    # Routing works: 0x4000_1000 -> uart, 0x4000_2000 -> ram.
    router.read(0x4000_1000)
    router.read(0x4000_2000)
    assert uart.reads == [(0x4000_1000, 4)]
    assert ram.reads == [(0x4000_2000, 4)]


def test_attach_router_is_idempotent() -> None:
    """Calling the hook twice leaves a single wrapper layer."""

    soc = FakeSoCWithAttach()
    attach_router(soc)
    first_wrapper = soc.attach_master
    attach_router(soc)
    # Second call is a no-op: same wrapper, no double-wrapping.
    assert soc.attach_master is first_wrapper

    # The wrapper still works — and crucially, ``where=None`` still
    # passes the bare master through. (If we had double-wrapped, the
    # inner wrapper would treat the outer wrapper's master as a Router
    # and spin up rules.)
    master = RecordingMaster("m")
    soc.attach_master(master)
    assert soc.attached_masters == [master]


def test_attach_router_no_op_when_soc_lacks_attach_master() -> None:
    """SoCs without ``attach_master`` (e.g. pre-bind stubs) are skipped."""

    class Bare:
        pass

    soc = Bare()
    # Should not raise, should not set anything on the SoC.
    attach_router(soc)
    assert not hasattr(soc, "attach_master")


# ---------------------------------------------------------------------------
# Discovery API (§4.2): soc.find, soc.find_by_name, soc.walk
# ---------------------------------------------------------------------------
#
# These tests build a hand-rolled SoC tree that deliberately does NOT
# implement ``.walk()`` — so the duck-typed vars() traversal in
# ``_walk`` is the path under test. Registers expose ``read``/``write``
# callables and an ``.offset`` (per the task spec); fields expose
# ``.lsb`` and ``.bits``.
# ---------------------------------------------------------------------------
class _DiscField:
    def __init__(self, name: str, lsb: int, width: int = 1) -> None:
        self.name = name
        self.lsb = lsb
        self.bits = (lsb, lsb + width - 1)

    # No read/write methods on fields — the duck-typed classifier needs
    # `bits`/`lsb` to dominate over `read`/`write` so a field that
    # also exposes typed accessors wouldn't be misclassified. Bare
    # fields here keep that boundary clear.


class _DiscReg:
    def __init__(self, name: str, offset: int, fields: dict) -> None:
        self.name = name
        self.offset = offset
        for fname, fobj in fields.items():
            setattr(self, fname, fobj)

    def read(self) -> int:  # noqa: D401 — duck-typed marker
        return 0

    def write(self, value: int) -> None:  # noqa: D401 — duck-typed marker
        return None


class _DiscUart:
    def __init__(self) -> None:
        self.name = "uart"
        self.intr_state = _DiscReg(
            "intr_state",
            offset=0x10,
            fields={
                "tx_ready": _DiscField("tx_ready", 0),
                "rx_ready": _DiscField("rx_ready", 1),
            },
        )
        self.ctrl = _DiscReg(
            "ctrl",
            offset=0x14,
            fields={
                "enable": _DiscField("enable", 0),
            },
        )


class _DiscSoC:
    """Hand-rolled SoC fake for discovery tests.

    Deliberately omits ``.walk()`` so the discovery API exercises the
    duck-typed ``vars()`` traversal rather than the protocol fast-path.
    """

    def __init__(self) -> None:
        self.name = "soc"
        self.uart = _DiscUart()


def test_attach_discovery_attaches_three_methods() -> None:
    """After the post-create hook fires, all three methods are present."""

    from peakrdl_pybind11.runtime.routing import attach_discovery

    soc = _DiscSoC()
    attach_discovery(soc)
    assert callable(soc.find)
    assert callable(soc.find_by_name)
    assert callable(soc.walk)


def test_walk_yields_pre_order_sequence() -> None:
    """``soc.walk()`` yields the root, then descends in declaration order."""

    from peakrdl_pybind11.runtime.routing import attach_discovery

    soc = _DiscSoC()
    attach_discovery(soc)

    walked = list(soc.walk())
    # Pre-order: soc, uart, intr_state, tx_ready, rx_ready, ctrl, enable.
    names = [getattr(n, "name", type(n).__name__) for n in walked]
    assert names == [
        "soc",
        "uart",
        "intr_state",
        "tx_ready",
        "rx_ready",
        "ctrl",
        "enable",
    ]


def test_walk_kind_reg_filters_to_registers_only() -> None:
    """``walk(kind="reg")`` yields only register nodes."""

    from peakrdl_pybind11.runtime.routing import attach_discovery

    soc = _DiscSoC()
    attach_discovery(soc)

    regs = list(soc.walk(kind="reg"))
    names = [getattr(n, "name", type(n).__name__) for n in regs]
    assert names == ["intr_state", "ctrl"]
    # And every yielded value really is a _DiscReg.
    assert all(isinstance(r, _DiscReg) for r in regs)


def test_walk_kind_field_filters_to_fields_only() -> None:
    """``walk(kind="field")`` yields only field nodes."""

    from peakrdl_pybind11.runtime.routing import attach_discovery

    soc = _DiscSoC()
    attach_discovery(soc)

    fields = list(soc.walk(kind="field"))
    names = [getattr(n, "name", type(n).__name__) for n in fields]
    assert names == ["tx_ready", "rx_ready", "enable"]


def test_find_by_addr_returns_matching_register() -> None:
    """``soc.find(addr)`` returns the register whose ``.offset == addr``."""

    from peakrdl_pybind11.runtime.routing import attach_discovery

    soc = _DiscSoC()
    attach_discovery(soc)

    found = soc.find(0x10)
    assert found is soc.uart.intr_state
    assert found.offset == 0x10

    found2 = soc.find(0x14)
    assert found2 is soc.uart.ctrl


def test_find_by_addr_returns_none_when_no_match() -> None:
    """``soc.find(addr)`` with no matching register returns ``None``."""

    from peakrdl_pybind11.runtime.routing import attach_discovery

    soc = _DiscSoC()
    attach_discovery(soc)
    assert soc.find(0xDEAD_BEEF) is None


def test_find_by_name_case_insensitive_substring() -> None:
    """``find_by_name`` is case-insensitive and matches substrings."""

    from peakrdl_pybind11.runtime.routing import attach_discovery

    soc = _DiscSoC()
    attach_discovery(soc)

    # Exact lowercase.
    matches = soc.find_by_name("intr_state")
    assert len(matches) == 1
    assert matches[0] is soc.uart.intr_state

    # Uppercase / mixed case still matches.
    matches_upper = soc.find_by_name("INTR_STATE")
    assert len(matches_upper) == 1
    assert matches_upper[0] is soc.uart.intr_state

    # Substring: "ready" matches both tx_ready and rx_ready.
    ready_matches = soc.find_by_name("ready")
    names = [m.name for m in ready_matches]
    assert names == ["tx_ready", "rx_ready"]


def test_find_by_name_returns_empty_list_on_no_match() -> None:
    """``find_by_name`` returns an empty list (never None) when nothing hits."""

    from peakrdl_pybind11.runtime.routing import attach_discovery

    soc = _DiscSoC()
    attach_discovery(soc)
    result = soc.find_by_name("nonexistent_register")
    assert result == []
    assert isinstance(result, list)


def test_attach_discovery_is_registered_as_post_create_hook() -> None:
    """``attach_discovery`` is wired into the post-create registry."""

    from peakrdl_pybind11.runtime.routing import attach_discovery

    hooks = _registry.get_post_create_hooks()
    assert attach_discovery in hooks


def test_attach_discovery_is_idempotent() -> None:
    """Calling the hook twice does not replace existing bound methods."""

    from peakrdl_pybind11.runtime.routing import attach_discovery

    soc = _DiscSoC()
    attach_discovery(soc)
    first_find = soc.find
    attach_discovery(soc)
    assert soc.find is first_find


def test_attach_discovery_no_op_on_slotted_soc() -> None:
    """SoCs that reject ``setattr`` (slotted/pybind11) don't crash the hook."""

    class Slotted:
        __slots__ = ("name",)

        def __init__(self) -> None:
            self.name = "slotted"

    from peakrdl_pybind11.runtime.routing import attach_discovery

    soc = Slotted()
    # Must not raise.
    attach_discovery(soc)
    # Slotted class rejects setattr for unknown names.
    assert not hasattr(soc, "find")
