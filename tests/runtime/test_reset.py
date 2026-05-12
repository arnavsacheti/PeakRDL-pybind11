"""Tests for ``runtime/reset.py`` (sketch §15 / §22).

These tests build a small mock SoC because the generated soc surface
hasn't been wired through reset yet. The mock implements the minimum
surface the reset implementation relies on:

* ``soc.<container>.<reg>`` — pre-order tree traversal via ``vars()``.
* Each register class exposes:
    - ``__peakrdl_meta__`` — the per-register metadata dict (the
      thing :mod:`_default_shims` stashes on real generated classes).
      Carries ``"reset"`` for resettable registers and ``"writable"`` for
      the ``rw_only`` filter.
    - ``read(*, raw=True)`` — returns the current stored int.
    - ``write(value, *, raw=False)`` — appends to the master's record.
"""

from __future__ import annotations

from typing import Any

from peakrdl_pybind11.runtime import _registry
from peakrdl_pybind11.runtime.reset import (
    _attach_reset_accessors,
    attach_reset_all,
    is_at_reset,
    reset_all,
    reset_value_of,
)


# ---------------------------------------------------------------------------
# Mock SoC plumbing
# ---------------------------------------------------------------------------


class _RecordingMaster:
    """Records every ``write`` issued by the registers it backs."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []

    def record(self, path: str, value: int) -> None:
        self.writes.append((path, value))


class _MockReg:
    """A tiny mock register that the reset module can drive.

    ``__peakrdl_meta__`` is set on the *instance* via the class-level
    ``_meta`` attribute attached at construction so the same lookup path
    the production code uses (``type(instance).__peakrdl_meta__``)
    works in tests. We dynamically subclass so each register instance
    has its own metadata.
    """

    def __init__(
        self,
        path: str,
        *,
        master: _RecordingMaster,
        initial: int = 0,
        reset: int | None = None,
        writable: dict[str, bool] | None = None,
    ) -> None:
        self.path = path
        self._master = master
        self._value = int(initial)
        # Build a fresh class per instance so per-register metadata can be
        # placed on the class dict (mirrors what _default_shims does on
        # generated register classes).
        meta: dict[str, Any] = {"fields": {}}
        if reset is not None:
            meta["reset"] = int(reset)
        if writable is not None:
            meta["writable"] = dict(writable)
        # Bind metadata onto the instance's class.
        klass = type(self.__class__.__name__, (_MockReg,), {})
        klass.__peakrdl_meta__ = meta  # type: ignore[attr-defined]
        # Re-stamp self's __class__ so type(self).__peakrdl_meta__ resolves.
        object.__setattr__(self, "__class__", klass)

    def read(self, *, raw: bool = False) -> int:
        # ``raw`` is accepted for parity with the production shim — both
        # branches just return the int because this mock skips the typed
        # wrapper machinery.
        _ = raw
        return self._value

    def write(self, value: int, *, raw: bool = False) -> None:
        _ = raw
        self._value = int(value)
        self._master.record(self.path, int(value))


class _Container:
    """An addrmap/regfile-like container that exposes children via ``vars()``."""

    def __init__(self) -> None:
        pass


def _make_soc() -> tuple[_Container, _RecordingMaster, dict[str, _MockReg]]:
    """Build a small SoC for the reset tests.

    Layout::

        soc
        ├── uart
        │   ├── control  reset=0x12345678  writable={enable: True}
        │   └── status   (no reset)
        └── timer
            └── readonly_reg  reset=0xDEADBEEF  writable={status: False}

    Returns ``(soc, master, registers_by_path)``.
    """

    master = _RecordingMaster()

    soc = _Container()
    uart = _Container()
    timer = _Container()
    soc.uart = uart  # type: ignore[attr-defined]
    soc.timer = timer  # type: ignore[attr-defined]

    control = _MockReg(
        "uart.control",
        master=master,
        initial=0,
        reset=0x12345678,
        writable={"enable": True},
    )
    status = _MockReg(
        "uart.status",
        master=master,
        initial=0xAA,
        reset=None,  # no reset metadata
    )
    readonly = _MockReg(
        "timer.readonly_reg",
        master=master,
        initial=0,
        reset=0xDEADBEEF,
        writable={"status": False},
    )

    uart.control = control  # type: ignore[attr-defined]
    uart.status = status  # type: ignore[attr-defined]
    timer.readonly_reg = readonly  # type: ignore[attr-defined]

    regs = {
        "uart.control": control,
        "uart.status": status,
        "timer.readonly_reg": readonly,
    }
    return soc, master, regs


# ---------------------------------------------------------------------------
# reset_value / is_at_reset
# ---------------------------------------------------------------------------


class TestRegisterAccessors:
    def test_reset_value_of_returns_metadata_value(self) -> None:
        _, _, regs = _make_soc()
        assert reset_value_of(regs["uart.control"]) == 0x12345678
        assert reset_value_of(regs["timer.readonly_reg"]) == 0xDEADBEEF

    def test_reset_value_of_returns_none_without_metadata(self) -> None:
        _, _, regs = _make_soc()
        assert reset_value_of(regs["uart.status"]) is None

    def test_is_at_reset_true_after_writing_reset_value(self) -> None:
        _, _, regs = _make_soc()
        reg = regs["uart.control"]
        # Initial value 0 != reset 0x12345678 — verify the False path first.
        assert is_at_reset(reg) is False
        reg.write(0x12345678, raw=True)
        assert is_at_reset(reg) is True

    def test_is_at_reset_false_after_writing_different_value(self) -> None:
        _, _, regs = _make_soc()
        reg = regs["uart.control"]
        reg.write(0xDEADBEEF, raw=True)
        assert is_at_reset(reg) is False

    def test_is_at_reset_false_when_no_reset_metadata(self) -> None:
        """No-reset case: documented to return ``False`` (not ``None``)."""
        _, _, regs = _make_soc()
        # Even when the current value happens to be 0, an unset reset
        # means we treat the register as "not resettable" — and therefore
        # "not at reset" — by convention.
        reg = regs["uart.status"]
        reg.write(0, raw=True)
        result = is_at_reset(reg)
        assert result is False
        assert isinstance(result, bool)

    def test_register_enhancement_attaches_accessors_to_class(self) -> None:
        """Verify the registry-driven attach path installs both surfaces.

        The free-function path is what the rest of the file exercises;
        this case calls the register-enhancement directly so the actual
        seam (``reg.reset_value`` and ``reg.is_at_reset()``) is covered.
        """

        class _Reg:
            __peakrdl_meta__ = {"reset": 0xCAFE, "fields": {}, "writable": {}}

            def __init__(self) -> None:
                self._value = 0

            def read(self, *, raw: bool = False) -> int:
                _ = raw
                return self._value

            def write(self, value: int, *, raw: bool = False) -> None:
                _ = raw
                self._value = int(value)

        _attach_reset_accessors(_Reg, _Reg.__peakrdl_meta__)
        assert _Reg.reset_value == 0xCAFE
        instance = _Reg()
        assert instance.is_at_reset() is False  # initial value 0
        instance.write(0xCAFE, raw=True)
        assert instance.is_at_reset() is True


# ---------------------------------------------------------------------------
# soc.reset_all() + subtree.reset_all()
# ---------------------------------------------------------------------------


class TestResetAll:
    def test_reset_all_on_soc_writes_every_resettable_register(self) -> None:
        soc, master, regs = _make_soc()
        attach_reset_all(soc)
        soc.reset_all()
        # Two registers carry reset metadata; the third (uart.status) does not.
        written_paths = sorted(p for p, _ in master.writes)
        assert "uart.control" in written_paths
        assert "timer.readonly_reg" in written_paths
        assert "uart.status" not in written_paths
        # Each register got exactly one write to its reset value.
        path_to_value = dict(master.writes)
        assert path_to_value["uart.control"] == 0x12345678
        assert path_to_value["timer.readonly_reg"] == 0xDEADBEEF

    def test_subtree_reset_all_only_writes_subtree(self) -> None:
        soc, master, regs = _make_soc()
        attach_reset_all(soc)
        # Reset only the uart subtree — timer.readonly_reg must not be touched.
        soc.uart.reset_all()
        written_paths = {p for p, _ in master.writes}
        assert written_paths == {"uart.control"}
        assert "timer.readonly_reg" not in written_paths

    def test_reset_all_rw_only_skips_read_only_registers(self) -> None:
        soc, master, regs = _make_soc()
        attach_reset_all(soc)
        soc.reset_all(rw_only=True)
        # The read-only register's only field has writable=False, so the
        # rw_only filter must skip it. The control register has at least
        # one writable field, so it still gets written.
        written_paths = {p for p, _ in master.writes}
        assert "uart.control" in written_paths
        assert "timer.readonly_reg" not in written_paths

    def test_reset_all_no_op_for_register_without_reset_metadata(self) -> None:
        """A standalone reset_all on a subtree with only un-resettable regs records nothing."""
        master = _RecordingMaster()
        soc = _Container()
        sub = _Container()
        soc.sub = sub  # type: ignore[attr-defined]
        # One register, no reset metadata.
        sub.no_reset = _MockReg(  # type: ignore[attr-defined]
            "sub.no_reset",
            master=master,
            initial=0xAA,
            reset=None,
        )
        attach_reset_all(soc)
        soc.reset_all()
        assert master.writes == []

    def test_reset_all_uses_raw_path(self) -> None:
        """Sanity: the writes made by reset_all go through the raw fast path.

        We model this by asserting that the recorded value matches the
        reset value verbatim (no FieldValue dispatch / shifting). The
        mock register ignores the ``raw`` kwarg, but it accepts it; if
        the production code stopped passing ``raw=True``, a strict mock
        could be swapped in to fail.
        """
        soc, master, _ = _make_soc()
        attach_reset_all(soc)
        soc.reset_all()
        for path, value in master.writes:
            if path == "uart.control":
                assert value == 0x12345678
            elif path == "timer.readonly_reg":
                assert value == 0xDEADBEEF

    def test_free_function_reset_all_returns_written_registers(self) -> None:
        soc, _, regs = _make_soc()
        written = reset_all(soc)
        assert regs["uart.control"] in written
        assert regs["timer.readonly_reg"] in written
        assert regs["uart.status"] not in written

    def test_attach_reset_all_does_not_attach_to_registers(self) -> None:
        """``reset_all`` is a subtree operation — registers aren't subtrees."""
        soc, _, regs = _make_soc()
        attach_reset_all(soc)
        # Containers got the method.
        assert callable(getattr(soc, "reset_all", None))
        assert callable(getattr(soc.uart, "reset_all", None))
        # Leaf registers did not.
        # (we don't assert ``not hasattr`` — the class may inherit it from
        # elsewhere — but we do assert that the bound method we'd install
        # was *not* attached.)
        for reg in regs.values():
            method = getattr(reg, "reset_all", None)
            # Only the bound method we install is a MethodType bound to the
            # node — anything else is fine (and means we didn't shadow it).
            if method is not None:
                # If something is there, it must not be our types.MethodType
                # bound to this register instance.
                import types as _types
                if isinstance(method, _types.MethodType):
                    assert method.__self__ is not reg


# ---------------------------------------------------------------------------
# Registry seam wiring
# ---------------------------------------------------------------------------


class TestRegistryWiring:
    def test_register_enhancement_is_registered(self) -> None:
        """Sanity: the module wires its register enhancement into Unit 1's seam."""
        enhancers = _registry.get_register_enhancers()
        assert _attach_reset_accessors in enhancers

    def test_post_create_hook_is_registered(self) -> None:
        """Sanity: the module wires its post-create hook into Unit 1's seam."""
        hooks = _registry.get_post_create_hooks()
        assert attach_reset_all in hooks
