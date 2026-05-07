"""
Tests for ``peakrdl_pybind11.runtime.widgets`` (Unit 15).

These tests use lightweight stand-ins for the generated SoC classes so
that we can exercise the rich-display surface without depending on a
compiled ``_native.so``. The widgets module is pure-Python and has only
duck-typed expectations of the node objects it renders, so this is the
right level to test it at.
"""

from __future__ import annotations

import time
import types
from typing import Any

import pytest

from peakrdl_pybind11.runtime import widgets


# ---------------------------------------------------------------------------
# Minimal generated-node fakes
# ---------------------------------------------------------------------------
class FakeField:
    def __init__(
        self,
        name: str,
        lsb: int,
        width: int,
        access: str = "rw",
        on_read: str = "",
        on_write: str = "",
        desc: str = "",
        value: int = 0,
        rclr: bool = False,
        singlepulse: bool = False,
        sticky: bool = False,
        is_volatile: bool = False,
    ) -> None:
        self.name = name
        self.lsb = lsb
        self.width = width
        self.msb = lsb + width - 1
        self.access = access
        self.on_read = on_read
        self.on_write = on_write
        self.desc = desc
        self._value = value
        self.rclr = rclr
        self.singlepulse = singlepulse
        self.sticky = sticky
        self.is_volatile = is_volatile

    def read(self) -> int:
        return self._value

    @property
    def is_readable(self) -> bool:
        return "r" in self.access

    @property
    def is_writable(self) -> bool:
        return "w" in self.access


class FakeRegister:
    """Stand-in that exposes the same surface as a generated reg class."""

    def __init__(
        self,
        name: str = "uart.control",
        address: int = 0x4000_1000,
        access: str = "rw",
        fields: list[FakeField] | None = None,
        desc: str = "Control register",
    ) -> None:
        self.name = name
        self.address = address
        self.access = access
        self._fields = fields or []
        self.desc = desc

    def fields(self) -> list[FakeField]:
        return list(self._fields)


class FakeAddrMap:
    """A container that exposes child registers as plain attributes."""

    _node_kind = "addrmap"

    def __init__(self, name: str, address: int, **children: Any) -> None:
        self.name = name
        self.address = address
        for key, value in children.items():
            setattr(self, key, value)


class FakeMemView:
    _widgets_kind = "memview"

    def __init__(self, base_address: int, data: bytes, name: str = "ram") -> None:
        self.base_address = base_address
        self._data = bytes(data)
        self.name = name

    def to_bytes(self) -> bytes:
        return self._data


class FakeSnapshotDiff:
    _widgets_kind = "snapshotdiff"

    def __init__(self, entries: list[tuple[str, Any, Any]]) -> None:
        self.entries = entries


class FakeIRQSource:
    def __init__(
        self,
        name: str,
        pending: bool = False,
        enabled: bool = False,
        test: bool = False,
    ) -> None:
        self.name = name
        self._pending = pending
        self._enabled = enabled
        self.test = test

    def is_pending(self) -> bool:
        return self._pending

    def is_enabled(self) -> bool:
        return self._enabled


class FakeInterruptGroup:
    _widgets_kind = "interruptgroup"

    def __init__(self, sources: list[FakeIRQSource], name: str = "uart.interrupts") -> None:
        self.sources = sources
        self.name = name


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def control_reg() -> FakeRegister:
    fields = [
        FakeField("enable", lsb=0, width=1, value=1, desc="Enable UART"),
        FakeField(
            "baudrate",
            lsb=1,
            width=3,
            value=1,
            desc="Baudrate selection",
        ),
        FakeField("parity", lsb=4, width=2, value=0, desc="Parity mode"),
    ]
    return FakeRegister(
        name="uart.control", address=0x4000_1000, fields=fields, desc="UART control"
    )


@pytest.fixture
def reset_status_reg() -> FakeRegister:
    fields = [
        FakeField(
            "por_flag",
            lsb=0,
            width=1,
            access="ro",
            on_read="rclr",
            rclr=True,
            desc="Power-on reset latched flag",
        ),
        FakeField(
            "wdt_flag",
            lsb=1,
            width=1,
            access="ro",
            on_read="rclr",
            rclr=True,
            desc="Watchdog reset flag",
        ),
    ]
    return FakeRegister(
        name="system.reset_status", address=0x4000_0018, access="ro", fields=fields
    )


# ---------------------------------------------------------------------------
# _repr_html_ on Reg / Field / RegFile / AddrMap / Mem
# ---------------------------------------------------------------------------
class TestRepresentationHTML:
    def test_register_html_contains_table_and_field_names(self, control_reg: FakeRegister) -> None:
        html = widgets.render_html(control_reg)
        assert "<table" in html
        assert "<thead" in html
        assert "</table>" in html
        for col in ("Bits", "Field", "Value", "Access", "Description"):
            assert col in html
        for name in ("enable", "baudrate", "parity"):
            assert name in html
        assert "uart.control" in html
        assert "0x40001000" in html

    def test_register_html_color_codes_access(self, control_reg: FakeRegister) -> None:
        html = widgets.render_html(control_reg)
        # rw should resolve to a non-empty colour style.
        assert "#1565c0" in html

    def test_register_html_renders_badges(self, reset_status_reg: FakeRegister) -> None:
        html = widgets.render_html(reset_status_reg)
        assert widgets.SIDE_EFFECT_BADGES["rclr"] in html

    def test_field_html_contains_table(self, control_reg: FakeRegister) -> None:
        field = control_reg.fields()[0]
        html = widgets.render_html(field)
        assert "<table" in html
        assert "enable" in html

    def test_addrmap_html_lists_children(self, control_reg: FakeRegister) -> None:
        amap = FakeAddrMap(name="uart", address=0x4000_1000, control=control_reg)
        html = widgets.render_html(amap)
        assert "<table" in html
        assert "uart.control" in html

    def test_render_html_handles_unknown_object(self) -> None:
        sentinel = types.SimpleNamespace(name="weird")
        html = widgets.render_html(sentinel)
        assert html.startswith("<div>")
        assert "weird" in html

    def test_register_html_does_not_run_destructive_reads(self) -> None:
        """Rendering must never call ``read()`` on an rclr field."""

        class TrackingField(FakeField):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.read_count = 0

            def read(self) -> int:
                self.read_count += 1
                return super().read()

        rclr_field = TrackingField(
            "por_flag", lsb=0, width=1, access="ro", on_read="rclr", rclr=True
        )
        plain_field = TrackingField("counter", lsb=8, width=8, value=42)
        reg = FakeRegister(
            name="system.status", address=0x40000000, fields=[rclr_field, plain_field]
        )

        html = widgets.render_html(reg)

        assert rclr_field.read_count == 0, "rclr field must not be read by the renderer"
        assert plain_field.read_count >= 1, "non-destructive field should be read once"
        assert widgets.SIDE_EFFECT_BADGES["rclr"] in html
        assert "por_flag" in html and "counter" in html


# ---------------------------------------------------------------------------
# _repr_html_ on MemView / SnapshotDiff / InterruptGroup
# ---------------------------------------------------------------------------
class TestSpecialRenderers:
    def test_memview_html(self) -> None:
        view = FakeMemView(base_address=0x40000, data=bytes(range(32)))
        html = widgets.render_html(view)
        assert "<table" in html
        # 16-byte rows, so 32 bytes -> two rows. Each row's first byte
        # address is rendered: 0x00040000 and 0x00040010.
        assert "0x00040000" in html
        assert "0x00040010" in html
        # Hex chars present.
        assert "0f" in html and "10" in html

    def test_snapshotdiff_html(self) -> None:
        diff = FakeSnapshotDiff(
            [
                ("uart.control", 0x0, 0x22),
                ("uart.status.tx_ready", 0, 1),
            ]
        )
        html = widgets.render_html(diff)
        assert "<table" in html
        assert "uart.control" in html
        assert "0x22" in html or "34" in html
        # Highlighted cells indicate "before vs after" change.
        assert "background:" in html or "background: #fff8c4" in html

    def test_interruptgroup_html(self) -> None:
        group = FakeInterruptGroup(
            sources=[
                FakeIRQSource("tx_done", pending=True, enabled=True),
                FakeIRQSource("rx_overflow", pending=False, enabled=True),
            ]
        )
        html = widgets.render_html(group)
        assert "<table" in html
        assert "tx_done" in html
        for column in ("State", "Enable", "Test", "Pending"):
            assert column in html


# ---------------------------------------------------------------------------
# _repr_pretty_
# ---------------------------------------------------------------------------
class TestReprPretty:
    def test_pretty_register_does_not_crash(self, control_reg: FakeRegister) -> None:
        text = widgets.render_pretty(control_reg)
        assert "uart.control" in text
        for name in ("enable", "baudrate", "parity"):
            assert name in text

    def test_pretty_field(self, control_reg: FakeRegister) -> None:
        field = control_reg.fields()[0]
        text = widgets.render_pretty(field)
        assert "enable" in text

    def test_ipython_pretty_protocol(self, control_reg: FakeRegister) -> None:
        # IPython hands p to _repr_pretty_; we mimic that with a small
        # collector. The protocol requires a .text(str) method.
        class FakeP:
            def __init__(self) -> None:
                self.parts: list[str] = []

            def text(self, value: str) -> None:
                self.parts.append(value)

        p = FakeP()
        widgets._ipython_pretty(control_reg, p, cycle=False)
        joined = "".join(p.parts)
        assert "enable" in joined

    def test_ipython_pretty_handles_cycle(self, control_reg: FakeRegister) -> None:
        class FakeP:
            def __init__(self) -> None:
                self.parts: list[str] = []

            def text(self, value: str) -> None:
                self.parts.append(value)

        p = FakeP()
        widgets._ipython_pretty(control_reg, p, cycle=True)
        # On a cycle we just emit repr; the only requirement is no crash.
        assert p.parts


# ---------------------------------------------------------------------------
# watch()
# ---------------------------------------------------------------------------
class TestWatch:
    def test_watch_raises_when_ipywidgets_missing(
        self, control_reg: FakeRegister, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(widgets, "ipywidgets", None)
        with pytest.raises(widgets.NotSupportedError) as excinfo:
            widgets.watch(control_reg)
        assert "ipywidgets" in str(excinfo.value)

    def test_watch_refuses_destructive_reads(
        self, reset_status_reg: FakeRegister, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Provide a fake ipywidgets so we get past the import check; the
        # destructive guard should still fire.
        fake_widgets = types.SimpleNamespace(HTML=lambda value="": types.SimpleNamespace(value=value))
        monkeypatch.setattr(widgets, "ipywidgets", fake_widgets)
        with pytest.raises(widgets.NotSupportedError) as excinfo:
            widgets.watch(reset_status_reg)
        assert "rclr" in str(excinfo.value).lower()

    def test_watch_destructive_with_allow(
        self, reset_status_reg: FakeRegister, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeHTML:
            def __init__(self, value: str = "") -> None:
                self.value = value

        fake_widgets = types.SimpleNamespace(HTML=FakeHTML)
        monkeypatch.setattr(widgets, "ipywidgets", fake_widgets)
        watcher = widgets.watch(
            reset_status_reg, period=0.05, allow_destructive=True, autostart=False
        )
        assert isinstance(watcher, widgets.Watcher)
        # Without autostart, no thread is alive.
        assert not watcher.is_running

    def test_watch_returns_running_watcher_and_stop_works(
        self, control_reg: FakeRegister, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeHTML:
            def __init__(self, value: str = "") -> None:
                self.value = value

        fake_widgets = types.SimpleNamespace(HTML=FakeHTML)
        monkeypatch.setattr(widgets, "ipywidgets", fake_widgets)

        watcher = widgets.watch(control_reg, period=0.02)
        try:
            assert isinstance(watcher, widgets.Watcher)
            assert watcher.is_running
            # Wait a few cycles so the thread has rendered at least once.
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and not watcher.widget.value:
                time.sleep(0.01)
            assert "<table" in watcher.widget.value
        finally:
            watcher.stop()
        assert not watcher.is_running

    def test_watch_period_must_be_positive(
        self, control_reg: FakeRegister, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeHTML:
            def __init__(self, value: str = "") -> None:
                self.value = value

        fake_widgets = types.SimpleNamespace(HTML=FakeHTML)
        monkeypatch.setattr(widgets, "ipywidgets", fake_widgets)
        with pytest.raises(ValueError):
            widgets.watch(control_reg, period=0)


# ---------------------------------------------------------------------------
# attach_widgets
# ---------------------------------------------------------------------------
class TestAttach:
    def test_attach_adds_repr_html_and_pretty(self) -> None:
        class Dummy:
            name = "dummy"
            address = 0
            access = "rw"

            def fields(self) -> list[Any]:
                return []

        widgets.attach_widgets(Dummy)
        instance = Dummy()
        assert hasattr(instance, "_repr_html_")
        assert hasattr(instance, "_repr_pretty_")
        # The HTML repr should at least produce a string.
        assert isinstance(instance._repr_html_(), str)

    def test_attach_is_idempotent(self) -> None:
        class Dummy:
            name = "dummy"
            address = 0
            access = "rw"

            def fields(self) -> list[Any]:
                return []

        widgets.attach_widgets(Dummy)
        first_repr = Dummy._repr_html_
        widgets.attach_widgets(Dummy)
        # Second call should be a no-op; same callable still attached.
        assert Dummy._repr_html_ is first_repr

    def test_attach_adds_watch_method(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeHTML:
            def __init__(self, value: str = "") -> None:
                self.value = value

        fake_widgets = types.SimpleNamespace(HTML=FakeHTML)
        monkeypatch.setattr(widgets, "ipywidgets", fake_widgets)

        class Dummy:
            name = "dummy"
            address = 0
            access = "rw"

            def fields(self) -> list[Any]:
                return []

        widgets.attach_widgets(Dummy)
        instance = Dummy()
        watcher = instance.watch(period=0.05, autostart=False)
        try:
            assert isinstance(watcher, widgets.Watcher)
        finally:
            watcher.stop()
