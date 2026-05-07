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

from peakrdl_pybind11.runtime.routing import (
    Router,
    RoutingError,
    attach_master,
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
