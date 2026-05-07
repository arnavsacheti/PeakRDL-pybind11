"""Tests for ``peakrdl_pybind11.runtime.aliases`` (sketch §10).

These tests stand in for Unit 23's exporter-side detection: each test
synthesises a tiny pair of register *classes* (one canonical, one alias)
with just enough metadata for the runtime helpers to consume, then drives
:func:`peakrdl_pybind11.runtime.aliases.apply_alias_relationship` to wire
the relationship.

Reads and writes go through a shared :class:`_FakeMaster` so the assertion
"both sides hit the same address" can be checked at the bus boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from peakrdl_pybind11.runtime.aliases import (
    apply_alias_relationship,
    register_register_enhancement,
    registered_enhancements,
    reset_enhancements,
)


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


class _FakeMaster:
    """Minimal master capturing every read/write for cross-checks.

    Stores last value per address; reads return whatever was last written
    (default 0). Records every operation so tests can assert "both sides
    hit the same address" at the bus boundary.
    """

    def __init__(self) -> None:
        self.reads: list[int] = []
        self.writes: list[tuple[int, int]] = []
        self.memory: dict[int, int] = {}

    def read(self, addr: int, _width: int = 32) -> int:
        self.reads.append(addr)
        return self.memory.get(addr, 0)

    def write(self, addr: int, val: int, _width: int = 32) -> None:
        self.writes.append((addr, val))
        self.memory[addr] = val


def _make_register_class(
    *,
    name: str,
    path: str,
    address: int,
    access: str = "rw",
) -> type:
    """Spin up a fresh register class for one test.

    Each call returns a *new* class so that tests don't bleed state through
    class-level monkey patches.
    """

    info = SimpleNamespace(
        name=name,
        path=path,
        address=address,
        access=access,
        alias_kind=None,
    )

    class Register:
        # Class-level metadata, the way generated code exposes it.
        info = None  # populated below
        _kind_label = "Reg"

        def __init__(self, master: _FakeMaster) -> None:
            self._master = master
            self._address = address

        def read(self) -> int:
            return self._master.read(self._address)

        def write(self, value: int) -> None:
            self._master.write(self._address, value)

    Register.__name__ = name
    Register.__qualname__ = name
    Register.info = info
    return Register


# ---------------------------------------------------------------------------
# Wiring: alias relationship attaches both sides
# ---------------------------------------------------------------------------


class TestRelationshipWiring:
    def test_alt_points_at_primary(self) -> None:
        primary = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )
        alt = _make_register_class(
            name="UartControlAlt", path="uart.control_alt", address=0x40001000
        )

        apply_alias_relationship(alt, primary, kind="full")

        assert alt.target is primary
        assert alt.is_alias is True

    def test_primary_lists_alias_in_aliases_tuple(self) -> None:
        primary = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )
        alt = _make_register_class(
            name="UartControlAlt", path="uart.control_alt", address=0x40001000
        )

        apply_alias_relationship(alt, primary, kind="full")

        # Sketch §10 example: ``soc.uart.control.aliases  # (soc.uart.control_alt,)``
        assert primary.aliases == (alt,)
        assert isinstance(primary.aliases, tuple)
        # Canonical reports False per docs/concepts/aliases.rst.
        assert primary.is_alias is False
        # Canonical's target is itself so callers can use ``.target`` uniformly.
        assert primary.target is primary

    def test_alias_has_empty_aliases_tuple(self) -> None:
        primary = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )
        alt = _make_register_class(
            name="UartControlAlt", path="uart.control_alt", address=0x40001000
        )

        apply_alias_relationship(alt, primary, kind="full")

        # Aliases don't chain — an alias has no aliases of its own.
        assert alt.aliases == ()

    def test_multiple_aliases_accumulate_in_order(self) -> None:
        primary = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )
        first = _make_register_class(
            name="UartControlAlt1", path="uart.control_alt1", address=0x40001000
        )
        second = _make_register_class(
            name="UartControlAlt2", path="uart.control_alt2", address=0x40001000
        )

        apply_alias_relationship(first, primary, kind="full")
        apply_alias_relationship(second, primary, kind="sw_view")

        assert primary.aliases == (first, second)
        assert first.target is primary
        assert second.target is primary

    def test_idempotent_wiring_does_not_duplicate(self) -> None:
        primary = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )
        alt = _make_register_class(
            name="UartControlAlt", path="uart.control_alt", address=0x40001000
        )

        apply_alias_relationship(alt, primary, kind="full")
        apply_alias_relationship(alt, primary, kind="full")

        assert primary.aliases == (alt,)
        assert alt.target is primary

    def test_self_alias_raises(self) -> None:
        primary = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )

        with pytest.raises(ValueError, match="must differ"):
            apply_alias_relationship(primary, primary, kind="full")


# ---------------------------------------------------------------------------
# Bus behaviour: alias and primary share an address
# ---------------------------------------------------------------------------


class TestBusBehaviour:
    def test_alias_read_hits_primary_address(self) -> None:
        primary_cls = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )
        alt_cls = _make_register_class(
            name="UartControlAlt", path="uart.control_alt", address=0x40001000
        )
        apply_alias_relationship(alt_cls, primary_cls, kind="full")

        master = _FakeMaster()
        master.memory[0x40001000] = 0xCAFEBABE

        primary = primary_cls(master)
        alt = alt_cls(master)

        assert primary.read() == 0xCAFEBABE
        assert alt.read() == 0xCAFEBABE
        # Both reads landed on the same address.
        assert master.reads == [0x40001000, 0x40001000]

    def test_alias_write_hits_primary_address(self) -> None:
        primary_cls = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )
        alt_cls = _make_register_class(
            name="UartControlAlt", path="uart.control_alt", address=0x40001000
        )
        apply_alias_relationship(alt_cls, primary_cls, kind="full")

        master = _FakeMaster()
        primary = primary_cls(master)
        alt = alt_cls(master)

        alt.write(0xDEADBEEF)
        # Reading via the primary sees the alias's write — same address.
        assert primary.read() == 0xDEADBEEF
        assert master.writes == [(0x40001000, 0xDEADBEEF)]


# ---------------------------------------------------------------------------
# Repr override
# ---------------------------------------------------------------------------


class TestAliasRepr:
    def test_repr_includes_alias_of_marker(self) -> None:
        primary = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )
        alt = _make_register_class(
            name="UartControlAlt", path="uart.control_alt", address=0x40001000
        )
        apply_alias_relationship(alt, primary, kind="full")

        master = _FakeMaster()
        rendered = repr(alt(master))

        # The sketch §10 example:
        #   <Reg uart.control_alt @ 0x40001000  alias-of=uart.control  rw>
        assert rendered == (
            "<Reg uart.control_alt @ 0x40001000  alias-of=uart.control  rw>"
        )

    def test_repr_components_present(self) -> None:
        primary = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )
        alt = _make_register_class(
            name="UartControlAlt", path="uart.control_alt", address=0x40001000
        )
        apply_alias_relationship(alt, primary, kind="full")

        master = _FakeMaster()
        rendered = repr(alt(master))

        assert "alias-of=uart.control" in rendered
        assert "uart.control_alt" in rendered
        assert "0x40001000" in rendered

    def test_repr_uses_access_from_info(self) -> None:
        primary = _make_register_class(
            name="ScrambleSrc", path="otp.src", address=0x40005000, access="rw"
        )
        alt = _make_register_class(
            name="ScrambleView",
            path="otp.src_scrambled",
            address=0x40005000,
            access="ro",
        )
        apply_alias_relationship(alt, primary, kind="scrambled")

        master = _FakeMaster()
        rendered = repr(alt(master))

        # Access label flows through from ``info.access``.
        assert rendered.endswith("  ro>")
        assert "alias-of=otp.src" in rendered

    def test_repr_handles_enum_value_access(self) -> None:
        # Some Info implementations store access as an enum member with a
        # ``.value`` attribute. The repr should pick the short token.
        access = SimpleNamespace(value="wo")
        primary = _make_register_class(
            name="CmdReg", path="dma.cmd", address=0x40006000
        )
        alt = _make_register_class(
            name="CmdRegAlt", path="dma.cmd_alt", address=0x40006000
        )
        alt.info.access = access  # type: ignore[attr-defined]
        apply_alias_relationship(alt, primary, kind="full")

        rendered = repr(alt(_FakeMaster()))
        assert rendered.endswith("  wo>")


# ---------------------------------------------------------------------------
# Info.alias_kind backfill (Unit 4 lives downstream)
# ---------------------------------------------------------------------------


class TestAliasKindMetadata:
    def test_kind_backfilled_when_info_slot_empty(self) -> None:
        primary = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )
        alt = _make_register_class(
            name="UartControlAlt", path="uart.control_alt", address=0x40001000
        )

        apply_alias_relationship(alt, primary, kind="sw_view")

        assert alt.info.alias_kind == "sw_view"  # type: ignore[attr-defined]

    def test_kind_does_not_overwrite_existing(self) -> None:
        primary = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )
        alt = _make_register_class(
            name="UartControlAlt", path="uart.control_alt", address=0x40001000
        )
        # Exporter pre-populated the slot — Unit 23's job in production.
        alt.info.alias_kind = "scrambled"  # type: ignore[attr-defined]

        apply_alias_relationship(alt, primary, kind="full")

        # Runtime did not overwrite exporter-supplied metadata.
        assert alt.info.alias_kind == "scrambled"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# register_register_enhancement seam (Unit 1 lives elsewhere)
# ---------------------------------------------------------------------------


class TestRegisterEnhancementSeam:
    def setup_method(self) -> None:
        reset_enhancements()

    def teardown_method(self) -> None:
        reset_enhancements()

    def test_register_returns_callable(self) -> None:
        def enhancer(_cls: type) -> None:
            return None

        returned = register_register_enhancement(enhancer)
        assert returned is enhancer
        assert registered_enhancements() == (enhancer,)

    def test_register_can_be_used_as_decorator(self) -> None:
        @register_register_enhancement
        def enhancer(_cls: type) -> None:
            return None

        assert enhancer in registered_enhancements()

    def test_alias_metadata_drives_enhancement(self) -> None:
        # Simulates how the per-SoC runtime template will eventually iterate
        # over the metadata that Unit 23 emits and call into Unit 13's
        # helpers via Unit 1's registry seam.
        primary = _make_register_class(
            name="UartControl", path="uart.control", address=0x40001000
        )
        alt = _make_register_class(
            name="UartControlAlt", path="uart.control_alt", address=0x40001000
        )

        # Per-class metadata as Unit 23 would emit it. The runtime decides
        # what to do with it; this test mirrors that wiring step.
        metadata: dict[type, dict[str, Any]] = {
            alt: {
                "is_alias": True,
                "target_cls": primary,
                "alias_kind": "full",
            },
        }

        @register_register_enhancement
        def alias_wirer(cls: type) -> None:
            entry = metadata.get(cls)
            if entry is None or not entry.get("is_alias"):
                return
            apply_alias_relationship(
                cls, entry["target_cls"], kind=entry.get("alias_kind")
            )

        # Exercise the seam the way the runtime template would.
        for enhancement in registered_enhancements():
            for cls in (primary, alt):
                enhancement(cls)

        assert alt.is_alias is True
        assert alt.target is primary
        assert primary.aliases == (alt,)
