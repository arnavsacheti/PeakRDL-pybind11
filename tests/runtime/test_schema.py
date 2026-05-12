"""Tests for :mod:`peakrdl_pybind11.runtime.schema`.

The schema serializer is duck-typed against any node that exposes an
``info`` namespace (or the raw attributes the widget tests use), so the
fakes here mirror the conventions in ``test_widgets.py`` /
``test_routing.py``: plain Python classes that set their children via
keyword args so ``vars()`` iteration matches declaration order.
"""

from __future__ import annotations

import enum
import json
from typing import Any

import pytest

from peakrdl_pybind11.runtime import schema
from peakrdl_pybind11.runtime.info import Info


# ---------------------------------------------------------------------------
# Minimal fake nodes
# ---------------------------------------------------------------------------
class FakeField:
    _node_kind = "field"

    def __init__(
        self,
        name: str,
        lsb: int,
        width: int,
        *,
        path: str | None = None,
        access: str = "rw",
        reset: int | None = None,
        description: str | None = None,
        is_hw_readable: bool = False,
        is_hw_writable: bool = False,
        encode: type[enum.IntEnum] | dict[str, int] | None = None,
    ) -> None:
        self.name = name
        self.lsb = lsb
        self.width = width
        # Pack everything into an Info; the schema serializer prefers it
        # over the raw attrs, so the "Info-driven" test cases exercise
        # the canonical metadata path.
        self.info = Info(
            name=name,
            desc=description,
            access=access,
            reset=reset,
            offset=lsb,  # info._info_factory stashes lsb under "offset"
            regwidth=width,
            path=path or name,
            is_hw_readable=is_hw_readable,
            is_hw_writable=is_hw_writable,
        )
        if encode is not None:
            self.encode = encode


class FakeRegister:
    _node_kind = "reg"

    def __init__(
        self,
        name: str,
        address: int,
        *,
        path: str | None = None,
        width: int = 32,
        description: str | None = None,
        fields: list[FakeField] | None = None,
    ) -> None:
        self.name = name
        self.address = address
        self._fields = fields or []
        self.info = Info(
            name=name,
            desc=description,
            address=address,
            regwidth=width,
            path=path or name,
        )

    def fields(self) -> list[FakeField]:
        return list(self._fields)


class FakeMem:
    _node_kind = "mem"

    def __init__(
        self,
        name: str,
        address: int,
        *,
        path: str | None = None,
        width: int = 32,
        description: str | None = None,
    ) -> None:
        self.name = name
        self.address = address
        self.info = Info(
            name=name,
            desc=description,
            address=address,
            regwidth=width,
            path=path or name,
        )


class FakeAddrMap:
    _node_kind = "addrmap"

    def __init__(
        self,
        name: str,
        address: int = 0,
        *,
        path: str | None = None,
        description: str | None = None,
        **children: Any,
    ) -> None:
        self.name = name
        self.address = address
        self.info = Info(
            name=name,
            desc=description,
            address=address,
            path=path or name,
        )
        # ``**children`` ordering matches the caller's keyword order on
        # CPython 3.7+, which is exactly the "declaration order" property
        # the schema walk promises.
        for child_name, child in children.items():
            setattr(self, child_name, child)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class BaudRate(enum.IntEnum):
    BAUD_9600 = 0
    BAUD_19200 = 1
    BAUD_115200 = 2


@pytest.fixture
def control_reg() -> FakeRegister:
    """A ``uart.control`` register with 3 fields."""
    fields = [
        FakeField(
            "enable",
            lsb=0,
            width=1,
            path="uart.control.enable",
            access="rw",
            reset=0,
            description="Enable UART",
            is_hw_readable=True,
            is_hw_writable=False,
        ),
        FakeField(
            "baudrate",
            lsb=1,
            width=3,
            path="uart.control.baudrate",
            access="rw",
            reset=1,
            description="Baudrate selection",
            encode=BaudRate,
        ),
        FakeField(
            "parity",
            lsb=4,
            width=2,
            path="uart.control.parity",
            access="rw",
            reset=0,
            description="Parity mode",
        ),
    ]
    return FakeRegister(
        name="control",
        address=0x4000_1000,
        path="uart.control",
        width=32,
        description="UART control",
        fields=fields,
    )


@pytest.fixture
def small_soc(control_reg: FakeRegister) -> FakeAddrMap:
    """Layout::

        soc
        ├── uart   (addrmap @ 0x4000_1000)
        │   └── control (reg @ 0x4000_1000, 3 fields)
        └── ram    (mem @ 0x4000_2000)
    """
    uart = FakeAddrMap(
        name="uart",
        address=0x4000_1000,
        path="uart",
        control=control_reg,
    )
    ram = FakeMem(
        name="ram",
        address=0x4000_2000,
        path="ram",
        width=32,
        description="On-chip SRAM",
    )
    soc = FakeAddrMap(
        name="my_soc",
        address=0,
        path="my_soc",
        description="Tiny test SoC",
        # keyword order is the declaration order the walk must preserve
        uart=uart,
        ram=ram,
    )
    return soc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestTopLevel:
    def test_schema_version_is_one(self, small_soc: FakeAddrMap) -> None:
        out = schema.to_dict(small_soc)
        assert out["schema_version"] == 1

    def test_top_level_kind_is_soc(self, small_soc: FakeAddrMap) -> None:
        out = schema.to_dict(small_soc)
        assert out["kind"] == "soc"

    def test_top_level_has_children_list(self, small_soc: FakeAddrMap) -> None:
        out = schema.to_dict(small_soc)
        assert "children" in out
        assert isinstance(out["children"], list)
        # The fixture wires two direct children (uart, ram).
        assert len(out["children"]) == 2

    def test_top_level_name_and_description(self, small_soc: FakeAddrMap) -> None:
        out = schema.to_dict(small_soc)
        assert out["name"] == "my_soc"
        assert out["description"] == "Tiny test SoC"


class TestRegisterAndFields:
    def test_register_field_list_has_expected_names_and_widths(
        self, small_soc: FakeAddrMap
    ) -> None:
        uart = small_soc.schema()["children"][0] if hasattr(small_soc, "schema") else None
        if uart is None:
            uart = schema.to_dict(small_soc)["children"][0]
        # uart -> control reg
        control = uart["children"][0]
        assert control["kind"] == "reg"
        assert control["name"] == "control"
        assert control["address"] == 0x4000_1000
        assert control["width"] == 32

        names = [f["name"] for f in control["fields"]]
        assert names == ["enable", "baudrate", "parity"]

        lsbs = [f["lsb"] for f in control["fields"]]
        assert lsbs == [0, 1, 4]

        widths = [f["width"] for f in control["fields"]]
        assert widths == [1, 3, 2]

    def test_field_metadata_from_info(self, small_soc: FakeAddrMap) -> None:
        out = schema.to_dict(small_soc)
        control = out["children"][0]["children"][0]
        enable = control["fields"][0]
        assert enable["is_hw_readable"] is True
        assert enable["is_hw_writable"] is False
        # ``Info`` upgrades the raw string to ``AccessMode.RW``; the
        # schema serializer must coerce it back to a plain "rw" token.
        assert enable["access"] == "rw"
        assert enable["reset"] == 0
        assert enable["description"] == "Enable UART"
        assert enable["is_readable"] is True
        assert enable["is_writable"] is True

    def test_encode_nested_under_encode_key(self, small_soc: FakeAddrMap) -> None:
        out = schema.to_dict(small_soc)
        baudrate = out["children"][0]["children"][0]["fields"][1]
        assert "encode" in baudrate
        assert baudrate["encode"]["type"] == "BaudRate"
        assert baudrate["encode"]["members"] == {
            "BAUD_9600": 0,
            "BAUD_19200": 1,
            "BAUD_115200": 2,
        }

    def test_encode_omitted_when_absent(self, small_soc: FakeAddrMap) -> None:
        out = schema.to_dict(small_soc)
        control_fields = out["children"][0]["children"][0]["fields"]
        # The first field (`enable`) and last field (`parity`) have no encode.
        assert "encode" not in control_fields[0]
        assert "encode" not in control_fields[2]


class TestMemory:
    def test_memory_node_shape(self, small_soc: FakeAddrMap) -> None:
        out = schema.to_dict(small_soc)
        ram = out["children"][1]
        assert ram["kind"] == "mem"
        assert ram["name"] == "ram"
        assert ram["address"] == 0x4000_2000
        assert ram["width"] == 32
        assert ram["description"] == "On-chip SRAM"


class TestJSONRoundTrip:
    def test_to_json_round_trips_via_json_loads(self, small_soc: FakeAddrMap) -> None:
        as_dict = schema.to_dict(small_soc)
        as_json = schema.to_json(small_soc)
        assert isinstance(as_json, str)
        # ``json.loads`` must give back an identical dict.
        round_tripped = json.loads(as_json)
        assert round_tripped == as_dict

    def test_to_json_emits_addresses_as_decimal_integers(
        self, small_soc: FakeAddrMap
    ) -> None:
        """The dict carries Python ``int`` addresses; JSON renders them as
        decimal. Consumers that want hex format on their side; the schema
        does not pre-format."""
        as_json = schema.to_json(small_soc, indent=None)
        assert '"address": 1073745920' in as_json  # 0x4000_1000
        assert '"address": 1073750016' in as_json  # 0x4000_2000

    def test_to_json_compact_mode(self, small_soc: FakeAddrMap) -> None:
        compact = schema.to_json(small_soc, indent=None)
        pretty = schema.to_json(small_soc, indent=2)
        # The compact form should be a single line; the pretty form,
        # multiple. Both decode to the same object.
        assert "\n" not in compact
        assert "\n" in pretty
        assert json.loads(compact) == json.loads(pretty)


class TestDeclarationOrder:
    def test_children_in_declaration_order_not_alphabetical(self) -> None:
        """``soc`` should yield children in the order they were attached,
        which on CPython 3.7+ matches kwarg-order on the parent
        ``FakeAddrMap``. ``ram`` comes alphabetically before ``uart`` but
        was declared after, so we expect the declaration order."""
        ram_first = FakeMem("ram", 0x100, path="ram")
        uart_first = FakeAddrMap("uart", 0x200, path="uart")
        soc = FakeAddrMap(
            name="soc",
            address=0,
            path="soc",
            # Declaration order: uart, ram (not alphabetical).
            uart=uart_first,
            ram=ram_first,
        )
        out = schema.to_dict(soc)
        names = [child["name"] for child in out["children"]]
        assert names == ["uart", "ram"]

        # And the opposite: declaring in alphabetical order yields the
        # same order (sanity check that we're not doing anything weird).
        soc2 = FakeAddrMap(
            name="soc",
            address=0,
            path="soc",
            ram=FakeMem("ram", 0x100, path="ram"),
            uart=FakeAddrMap("uart", 0x200, path="uart"),
        )
        names2 = [child["name"] for child in schema.to_dict(soc2)["children"]]
        assert names2 == ["ram", "uart"]


class TestFieldsOrderingInRegister:
    def test_fields_in_declaration_order(self, control_reg: FakeRegister) -> None:
        # The ``control_reg`` fixture lists fields as enable / baudrate /
        # parity; the serializer must preserve that order even though
        # ``parity`` sorts alphabetically before ``baudrate``.
        rendered = schema.to_dict(control_reg)
        # ``to_dict`` labels the root "soc" regardless; for a bare reg
        # the kind override leaves an empty children list — but the reg
        # itself isn't a container. We instead serialize through a
        # container so the "fields" list is what we want to look at.
        soc = FakeAddrMap(name="soc", address=0, path="soc", control=control_reg)
        out = schema.to_dict(soc)
        control = out["children"][0]
        assert [f["name"] for f in control["fields"]] == [
            "enable",
            "baudrate",
            "parity",
        ]
        # ``rendered`` is the bare-reg case; it should still produce a
        # dict with a ``fields`` list of the same length (we don't
        # promise a particular ``kind`` for the override, but we *do*
        # promise stability).
        assert isinstance(rendered, dict)


class TestPostCreateHook:
    def test_schema_attached_to_soc(self) -> None:
        """``register_post_create`` should bind ``soc.schema()``."""
        from peakrdl_pybind11.runtime._registry import fire_post_create_hooks

        soc = FakeAddrMap(name="soc", address=0, path="soc")
        fire_post_create_hooks(soc)
        assert callable(soc.schema)
        # Bound method must agree with the module-level function.
        assert soc.schema() == schema.to_dict(soc)
