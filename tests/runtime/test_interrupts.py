"""Unit tests for ``runtime.interrupts`` (Unit 12)."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from peakrdl_pybind11.runtime import _registry
from peakrdl_pybind11.runtime.errors import WaitTimeoutError
from peakrdl_pybind11.runtime.interrupts import (
    InterruptGroup,
    InterruptSource,
    InterruptTree,
    interrupts_post_create,
    interrupts_register_enhancement,
    register_interrupt_group,
    register_post_create_hook,
    register_register_enhancement_hook,
)


# ---------------------------------------------------------------------------
# Test doubles
#
# We model the generated-tree contract with the smallest field/register
# stand-in that satisfies the runtime's protocols. Each ``MockField`` carries
# one bit of state and (optionally) ``info`` metadata so the runtime can
# pick the right ``clear()`` semantics.
# ---------------------------------------------------------------------------


class MockField:
    """Minimal :class:`FieldLike` stub: holds a single bit + side-effect tags.

    Tracks every read and write so tests can assert the runtime issues the
    expected sequence of operations.
    """

    def __init__(
        self,
        value: int = 0,
        *,
        on_write: str = "woclr",
        on_read: str = "",
        name: str = "",
    ) -> None:
        self._value = int(value)
        self.info = SimpleNamespace(on_write=on_write, on_read=on_read)
        self.inst_name = name
        self.reads: list[None] = []
        self.writes: list[int] = []

    def read(self) -> int:
        self.reads.append(None)
        # Honor read-clears semantics so woclr fields and rclr fields can
        # be tested through the same API.
        v = self._value
        if self.info.on_read == "rclr":
            self._value = 0
        return v

    def write(self, value: int) -> None:
        v = int(value)
        self.writes.append(v)
        if self.info.on_write == "woclr":
            # Write-1-to-clear: writing 1 clears, writing 0 is a no-op.
            if v:
                self._value = 0
        elif self.info.on_write == "wzc":
            # Write-0-to-clear.
            if not v:
                self._value = 0
        else:
            # Default: regular RW.
            self._value = v

    # Test-only accessor: lets tests poke the underlying bit (simulating
    # hardware setting INTR_STATE) without going through ``write`` and its
    # side-effect machinery.
    def set_hw(self, value: int) -> None:
        self._value = int(value)


class MockRegister:
    """A register-like object that exposes its fields via ``fields()``."""

    def __init__(self, **fields: MockField) -> None:
        for name, field in fields.items():
            field.inst_name = name
            setattr(self, name, field)
        self._fields = fields

    def fields(self) -> list[MockField]:
        return list(self._fields.values())


# ---------------------------------------------------------------------------
# Per-source behaviour
# ---------------------------------------------------------------------------


def test_is_pending_reads_state_bit() -> None:
    """``group.tx_done.is_pending()`` reads the state bit."""

    state = MockField(value=0, name="tx_done")
    group = InterruptGroup({"tx_done": InterruptSource(state, name="tx_done")})

    assert group.tx_done.is_pending() is False
    state.set_hw(1)
    assert group.tx_done.is_pending() is True
    # Each query goes through the bus; cache-free behaviour matters for HW.
    assert len(state.reads) == 2


def test_clear_writes_one_for_woclr() -> None:
    """``clear()`` honors ``info.on_write="woclr"`` and writes 1."""

    state = MockField(value=1, on_write="woclr")
    src = InterruptSource(state, name="tx_done")

    src.clear()

    assert state.writes == [1]
    assert state._value == 0  # woclr: write 1 cleared the bit.


def test_clear_writes_zero_for_wzc() -> None:
    """``clear()`` writes 0 for the (rare) ``wzc`` semantic."""

    state = MockField(value=1, on_write="wzc")
    src = InterruptSource(state, name="tx_done")

    src.clear()

    assert state.writes == [0]
    assert state._value == 0


def test_clear_reads_for_rclr() -> None:
    """``clear()`` triggers a read when the field is read-to-clear."""

    state = MockField(value=1, on_write="rw", on_read="rclr")
    src = InterruptSource(state, name="tx_done")

    src.clear()

    assert state.reads == [None]
    assert state.writes == []
    assert state._value == 0


def test_acknowledge_is_alias_for_clear() -> None:
    """The §9.1 spelling: ``ack()`` and ``clear()`` are interchangeable."""

    state = MockField(value=1)
    src = InterruptSource(state, name="tx_done")

    src.acknowledge()

    assert state.writes == [1]


def test_fire_writes_test_bit() -> None:
    """``fire()`` writes 1 to the test partner — the SW self-trigger."""

    state = MockField(value=0)
    test = MockField(value=0, on_write="rw")
    src = InterruptSource(state, test_field=test, name="tx_done")

    src.fire()

    assert test.writes == [1]


def test_fire_without_test_field_raises() -> None:
    """Sources with no test partner can't be SW-triggered."""

    state = MockField(value=0)
    src = InterruptSource(state, name="tx_done")

    with pytest.raises(NotImplementedError):
        src.fire()


def test_enable_disable_round_trip() -> None:
    """``enable()`` / ``disable()`` set and clear the enable bit."""

    state = MockField(value=0)
    enable = MockField(value=0, on_write="rw")
    src = InterruptSource(state, enable_field=enable, name="tx_done")

    assert src.is_enabled() is False
    src.enable()
    assert enable.writes == [1]
    assert src.is_enabled() is True
    src.disable()
    assert enable.writes == [1, 0]


def test_is_enabled_without_partner_is_true() -> None:
    """Sources lacking an enable bit are reported as always enabled."""

    state = MockField(value=0)
    src = InterruptSource(state, name="tx_done")

    assert src.is_enabled() is True


# ---------------------------------------------------------------------------
# Group surface
# ---------------------------------------------------------------------------


def _build_group() -> tuple[
    InterruptGroup, dict[str, MockField], dict[str, MockField]
]:
    """Three-source group with state + enable + test partners."""

    state_fields = {
        "tx_done": MockField(value=0, name="tx_done"),
        "rx_overflow": MockField(value=0, name="rx_overflow"),
        "parity_err": MockField(value=0, name="parity_err"),
    }
    enable_fields = {
        n: MockField(value=0, on_write="rw") for n in state_fields
    }
    test_fields = {
        n: MockField(value=0, on_write="rw") for n in state_fields
    }
    sources = {
        n: InterruptSource(
            state_fields[n], enable_fields[n], test_fields[n], name=n
        )
        for n in state_fields
    }
    return InterruptGroup(sources), state_fields, enable_fields


def test_group_getattr_returns_named_source() -> None:
    """``group.tx_done`` returns the matching :class:`InterruptSource`."""

    group, _, _ = _build_group()
    assert isinstance(group.tx_done, InterruptSource)
    assert group.tx_done.name == "tx_done"


def test_group_getattr_unknown_raises_attribute_error() -> None:
    """Mistyped names produce an actionable :class:`AttributeError`."""

    group, _, _ = _build_group()
    with pytest.raises(AttributeError) as exc:
        _ = group.nonexistent  # noqa: F841
    assert "nonexistent" in str(exc.value)


def test_group_iter_yields_sources() -> None:
    """Iterating the group yields each source exactly once."""

    group, _, _ = _build_group()
    names = sorted(s.name for s in group)
    assert names == ["parity_err", "rx_overflow", "tx_done"]
    assert "tx_done" in group
    assert len(group) == 3


def test_group_pending_returns_frozenset() -> None:
    """``group.pending()`` returns a frozenset of currently-pending sources."""

    group, state_fields, _ = _build_group()
    state_fields["tx_done"].set_hw(1)
    state_fields["parity_err"].set_hw(1)

    pending = group.pending()
    assert isinstance(pending, frozenset)
    names = {s.name for s in pending}
    assert names == {"tx_done", "parity_err"}


def test_group_enabled_returns_frozenset() -> None:
    """``group.enabled()`` mirrors ``pending()`` for the enable column."""

    group, _, enable_fields = _build_group()
    enable_fields["tx_done"].set_hw(1)
    enable_fields["rx_overflow"].set_hw(1)

    enabled = group.enabled()
    names = {s.name for s in enabled}
    assert names == {"tx_done", "rx_overflow"}


def test_group_clear_all_writes_one_to_each_state() -> None:
    """``clear_all()`` issues one write per source."""

    group, state_fields, _ = _build_group()
    for f in state_fields.values():
        f.set_hw(1)

    group.clear_all()

    for f in state_fields.values():
        assert f.writes == [1]
        assert f._value == 0


def test_group_disable_all_clears_each_enable_bit() -> None:
    """``disable_all()`` writes 0 to every enable partner."""

    group, _, enable_fields = _build_group()
    for f in enable_fields.values():
        f.set_hw(1)

    group.disable_all()

    for f in enable_fields.values():
        assert f.writes == [0]


def test_group_enable_subset() -> None:
    """``enable(set_={...})`` enables only the named sources."""

    group, _, enable_fields = _build_group()

    group.enable(set_={"tx_done", "parity_err"})

    assert enable_fields["tx_done"].writes == [1]
    assert enable_fields["parity_err"].writes == [1]
    assert enable_fields["rx_overflow"].writes == []


def test_group_enable_no_set_enables_all() -> None:
    """``enable()`` with no argument enables every source."""

    group, _, enable_fields = _build_group()

    group.enable()

    for f in enable_fields.values():
        assert f.writes == [1]


def test_group_enable_unknown_name_raises() -> None:
    """Unknown source name in ``enable(set_=...)`` raises ``KeyError``."""

    group, _, _ = _build_group()
    with pytest.raises(KeyError):
        group.enable(set_={"missing"})


def test_group_snapshot_returns_state_enable_pairs() -> None:
    """``snapshot()`` shape matches the §9.2 sketch."""

    group, state_fields, enable_fields = _build_group()
    state_fields["tx_done"].set_hw(1)
    enable_fields["tx_done"].set_hw(1)
    enable_fields["rx_overflow"].set_hw(1)

    snap = group.snapshot()

    assert snap == {
        "tx_done": (1, 1),
        "rx_overflow": (0, 1),
        "parity_err": (0, 0),
    }


def test_group_snapshot_without_enable_partner_reports_one() -> None:
    """Sources lacking an enable partner snapshot as enabled."""

    state = MockField(value=1, name="lone")
    group = InterruptGroup({"lone": InterruptSource(state, name="lone")})

    assert group.snapshot() == {"lone": (1, 1)}


# ---------------------------------------------------------------------------
# manual() factory
# ---------------------------------------------------------------------------


def test_manual_builds_group_from_registers() -> None:
    """``InterruptGroup.manual`` glues the trio into a :class:`InterruptGroup`."""

    state = MockRegister(
        tx_done=MockField(value=1),
        rx_overflow=MockField(value=0),
    )
    enable = MockRegister(
        tx_done=MockField(value=1, on_write="rw"),
        rx_overflow=MockField(value=0, on_write="rw"),
    )
    test = MockRegister(
        tx_done=MockField(value=0, on_write="rw"),
        rx_overflow=MockField(value=0, on_write="rw"),
    )

    group = InterruptGroup.manual(state=state, enable=enable, test=test)

    assert sorted(s.name for s in group) == ["rx_overflow", "tx_done"]
    assert group.tx_done.is_pending() is True
    assert group.tx_done.is_enabled() is True


def test_manual_tolerates_missing_enable_test() -> None:
    """Manual wiring works with state alone — partners are optional."""

    state = MockRegister(tx_done=MockField(value=0))
    group = InterruptGroup.manual(state=state)

    assert group.tx_done.enable_field is None
    assert group.tx_done.test_field is None
    # No enable partner → always reported enabled.
    assert group.tx_done.is_enabled() is True


def test_manual_state_register_with_no_fields_raises() -> None:
    """``manual`` rejects an empty state register — there's nothing to wrap."""

    state = MockRegister()
    with pytest.raises(ValueError):
        InterruptGroup.manual(state=state)


def test_manual_accepts_field_mapping() -> None:
    """Passing a plain ``{name: field}`` dict instead of a register works."""

    state = {"tx_done": MockField(value=1)}
    group = InterruptGroup.manual(state=state)
    assert group.tx_done.is_pending() is True


# ---------------------------------------------------------------------------
# Wait family
# ---------------------------------------------------------------------------


def test_wait_returns_when_pending() -> None:
    """``wait()`` returns once the bit goes high."""

    state = MockField(value=0)
    src = InterruptSource(state, name="tx_done")

    def _flip_after_delay() -> None:
        # Tiny delay so the wait actually has to poll once.
        time.sleep(0.01)
        state.set_hw(1)

    threading.Thread(target=_flip_after_delay, daemon=True).start()
    # Should return cleanly — no exception.
    src.wait(timeout=1.0, period=0.001)


def test_wait_timeout_raises_wait_timeout_error() -> None:
    """``wait()`` raises :class:`WaitTimeoutError` when nothing fires."""

    state = MockField(value=0)
    src = InterruptSource(state, name="tx_done")

    with pytest.raises(WaitTimeoutError) as exc:
        src.wait(timeout=0.05, period=0.001)
    assert "tx_done" in str(exc.value)
    assert exc.value.expected is True
    assert exc.value.last_seen is False


def test_wait_clear_returns_when_no_longer_pending() -> None:
    """``wait_clear()`` is the falling-edge analogue of ``wait()``."""

    state = MockField(value=1)
    src = InterruptSource(state, name="tx_done")

    def _clear_after_delay() -> None:
        time.sleep(0.01)
        state.set_hw(0)

    threading.Thread(target=_clear_after_delay, daemon=True).start()
    src.wait_clear(timeout=1.0, period=0.001)


def test_wait_clear_timeout_raises() -> None:
    """``wait_clear()`` raises if the bit stays high."""

    state = MockField(value=1)
    src = InterruptSource(state, name="tx_done")

    with pytest.raises(WaitTimeoutError):
        src.wait_clear(timeout=0.02, period=0.001)


def test_poll_alias_returns_when_pending() -> None:
    """``poll(period=, timeout=)`` matches §9.1 with the period knob first."""

    state = MockField(value=0)
    src = InterruptSource(state, name="tx_done")

    threading.Timer(0.01, lambda: state.set_hw(1)).start()
    src.poll(period=0.001, timeout=1.0)


def test_aiowait_async_path() -> None:
    """``aiowait`` is the asyncio dual; it returns when pending."""

    state = MockField(value=0)
    src = InterruptSource(state, name="tx_done")

    async def _runner() -> None:
        async def flipper() -> None:
            await asyncio.sleep(0.01)
            state.set_hw(1)

        await asyncio.gather(flipper(), src.aiowait(timeout=1.0, period=0.001))

    asyncio.run(_runner())


def test_aiowait_timeout() -> None:
    """``aiowait`` raises :class:`WaitTimeoutError` on timeout."""

    state = MockField(value=0)
    src = InterruptSource(state, name="tx_done")

    async def _runner() -> None:
        await src.aiowait(timeout=0.02, period=0.001)

    with pytest.raises(WaitTimeoutError):
        asyncio.run(_runner())


class TestAiowait:
    """Extra coverage for the asyncio dual of :meth:`InterruptSource.wait`.

    Complements the two function-level tests above (``async_path`` /
    ``timeout``) with the cases called out by §9.1: immediate-pending
    fast path, mid-wait flip via a sibling task, and cancellation
    cleanup so a dropped ``aiowait`` doesn't leak the poll loop.
    """

    def test_aiowait_returns_immediately_when_already_pending(self) -> None:
        """No polling delay when the source is already pending at call time."""

        state = MockField(value=1)
        src = InterruptSource(state, name="tx_done")

        async def _runner() -> None:
            # Use a non-trivial poll period so a buggy implementation that
            # waits before checking would visibly slow this down.
            await src.aiowait(timeout=1.0, period=0.5)

        start = time.monotonic()
        asyncio.run(_runner())
        elapsed = time.monotonic() - start
        # Allow generous slack for CI jitter; a non-fast-path implementation
        # would have to wait ~0.5s before returning.
        assert elapsed < 0.1, f"aiowait took {elapsed:.3f}s when source already pending"

    def test_aiowait_resolves_when_flipped_mid_wait(self) -> None:
        """A sibling task flipping the bit mid-wait wakes ``aiowait`` up."""

        state = MockField(value=0)
        src = InterruptSource(state, name="tx_done")

        async def _runner() -> None:
            async def _flipper() -> None:
                # Sleep a few poll periods so the wait actually polls before
                # the flip — exercises the rising-edge path, not the fast
                # path of test_aiowait_returns_immediately_when_already_pending.
                await asyncio.sleep(0.01)
                state.set_hw(1)

            flipper_task = asyncio.create_task(_flipper())
            await src.aiowait(timeout=1.0, period=0.001)
            await flipper_task  # ensure clean shutdown

        asyncio.run(_runner())
        assert state._value == 1

    def test_aiowait_timeout_raises_wait_timeout_error(self) -> None:
        """The 50 ms timeout case from §9.1: never asserts → ``WaitTimeoutError``."""

        state = MockField(value=0)
        src = InterruptSource(state, name="tx_done")

        async def _runner() -> None:
            await src.aiowait(timeout=0.05, period=0.001)

        with pytest.raises(WaitTimeoutError) as exc:
            asyncio.run(_runner())
        # Same fields as the sync ``wait()`` timeout — the async path
        # should carry the same diagnostics.
        assert exc.value.expected is True
        assert exc.value.last_seen is False
        assert "tx_done" in str(exc.value)

    def test_aiowait_cancellation_propagates_and_leaves_no_leak(self) -> None:
        """Cancelling the awaiting task is clean: ``CancelledError`` propagates
        and the source/event loop are left in a usable state.

        The poll loop lives inside the cancelled task — cancelling the task
        is enough to tear it down. We assert (1) the task is marked cancelled
        and (2) a subsequent ``aiowait`` on the *same* source still works,
        which would fail if any module-level state had been wedged by the
        cancellation.
        """

        state = MockField(value=0)
        src = InterruptSource(state, name="tx_done")

        async def _runner() -> bool:
            task = asyncio.create_task(src.aiowait(timeout=1.0, period=0.001))
            # Let the poll loop spin at least one iteration.
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return task.cancelled()

        was_cancelled = asyncio.run(_runner())
        assert was_cancelled

        # State integrity check: a fresh aiowait on the same source still
        # works, proving the cancellation didn't wedge any shared state.
        state.set_hw(1)

        async def _post_cancel() -> None:
            await src.aiowait(timeout=1.0, period=0.001)

        asyncio.run(_post_cancel())


# ---------------------------------------------------------------------------
# on_fire subscription
# ---------------------------------------------------------------------------


def test_on_fire_callback_runs_on_rising_edge() -> None:
    """The callback fires when the state bit transitions 0 → 1."""

    state = MockField(value=0)
    src = InterruptSource(state, name="tx_done")

    fired = threading.Event()
    calls: list[None] = []

    def _cb() -> None:
        calls.append(None)
        fired.set()

    unsub = src.on_fire(_cb, period=0.001)
    try:
        # Briefly idle, then assert.
        time.sleep(0.01)
        assert not calls  # Stayed at 0 — no firings yet.

        state.set_hw(1)
        assert fired.wait(timeout=1.0)
        assert len(calls) == 1
    finally:
        unsub()


def test_on_fire_unsubscribe_stops_callbacks() -> None:
    """The unsubscribe handle stops further callbacks."""

    state = MockField(value=0)
    src = InterruptSource(state, name="tx_done")

    calls: list[None] = []
    unsub = src.on_fire(lambda: calls.append(None), period=0.001)
    unsub()

    # Give the poller a generous grace window to die out.
    time.sleep(0.02)
    state.set_hw(1)
    time.sleep(0.02)

    assert calls == []


def test_on_fire_no_double_fire_while_held_high() -> None:
    """Holding the source asserted yields a single rising-edge callback."""

    state = MockField(value=0)
    src = InterruptSource(state, name="tx_done")

    calls: list[None] = []
    unsub = src.on_fire(lambda: calls.append(None), period=0.001)
    try:
        state.set_hw(1)
        time.sleep(0.05)  # Multiple poll cycles, but one edge.
        assert len(calls) == 1
    finally:
        unsub()


# ---------------------------------------------------------------------------
# InterruptTree (top-level soc.interrupts)
# ---------------------------------------------------------------------------


def test_tree_aggregates_groups() -> None:
    """``InterruptTree`` exposes groups by attribute and iteration."""

    g1, _, _ = _build_group()
    g2 = InterruptGroup({"alone": InterruptSource(MockField(0), name="alone")})

    tree = InterruptTree({"uart": g1, "spi": g2})

    assert tree.uart is g1
    assert tree.spi is g2
    # InterruptGroup is unhashable-ordered; iterate by identity.
    iterated = list(tree)
    assert iterated == [g1, g2]
    assert len(tree) == 2


def test_tree_pending_unions_across_groups() -> None:
    """Top-level ``pending()`` is the union across every group."""

    g1, state1, _ = _build_group()
    state1["tx_done"].set_hw(1)
    g2_state = MockField(value=1, name="alone")
    g2 = InterruptGroup({"alone": InterruptSource(g2_state, name="alone")})

    tree = InterruptTree({"uart": g1, "spi": g2})
    pending = tree.pending()
    names = {s.name for s in pending}
    assert names == {"tx_done", "alone"}


def test_tree_wait_any_returns_first_pending() -> None:
    """``wait_any`` returns the first observed pending source."""

    g, state_fields, _ = _build_group()
    tree = InterruptTree({"uart": g})

    threading.Timer(0.01, lambda: state_fields["rx_overflow"].set_hw(1)).start()

    src = tree.wait_any(timeout=1.0, period=0.001)
    assert src.name == "rx_overflow"


def test_tree_wait_any_timeout_raises() -> None:
    """``wait_any`` raises ``WaitTimeoutError`` when nothing fires."""

    g, _, _ = _build_group()
    tree = InterruptTree({"uart": g})

    with pytest.raises(WaitTimeoutError):
        tree.wait_any(timeout=0.02, period=0.001)


def test_tree_tree_renders_status() -> None:
    """``tree()`` renders a human-readable dump of every group."""

    g, state_fields, enable_fields = _build_group()
    state_fields["tx_done"].set_hw(1)
    enable_fields["tx_done"].set_hw(1)
    tree = InterruptTree({"uart": g})

    text = tree.tree()
    assert "uart:" in text
    assert "tx_done" in text
    assert "state=1" in text
    assert "enable=1" in text


# ---------------------------------------------------------------------------
# Hooks (Unit 1 / Unit 23 seams)
# ---------------------------------------------------------------------------


def test_register_post_create_hook_attaches_tree() -> None:
    """``register_post_create_hook`` attaches ``soc.interrupts``."""

    g = InterruptGroup({"tx_done": InterruptSource(MockField(0), name="tx_done")})
    register_interrupt_group("uart", g)

    soc: Any = SimpleNamespace()
    tree = register_post_create_hook(soc)

    assert isinstance(tree, InterruptTree)
    assert soc.interrupts is tree
    assert tree.uart is g

    # Reset for other tests.
    from peakrdl_pybind11.runtime import interrupts

    interrupts._GROUP_REGISTRY.clear()


def test_register_register_enhancement_hook_builds_group() -> None:
    """The detection-metadata hook synthesises an :class:`InterruptGroup`."""

    state_reg = MockRegister(
        tx_done=MockField(value=1),
        rx_overflow=MockField(value=0),
    )
    enable_reg = MockRegister(
        tx_done=MockField(value=1, on_write="rw"),
        rx_overflow=MockField(value=0, on_write="rw"),
    )
    test_reg = MockRegister(
        tx_done=MockField(value=0, on_write="rw"),
        rx_overflow=MockField(value=0, on_write="rw"),
    )
    parent = SimpleNamespace()
    state_reg.parent = parent  # type: ignore[attr-defined]
    state_reg.path = "uart.intr_state"  # type: ignore[attr-defined]

    info = SimpleNamespace(
        is_interrupt_state=True,
        enable_register=enable_reg,
        test_register=test_reg,
    )

    group = register_register_enhancement_hook(state_reg, info)

    assert isinstance(group, InterruptGroup)
    assert parent.interrupts is group
    assert group.tx_done.is_pending() is True

    # Reset for other tests.
    from peakrdl_pybind11.runtime import interrupts

    interrupts._GROUP_REGISTRY.clear()


def test_register_register_enhancement_hook_skips_non_interrupt() -> None:
    """Non-interrupt registers are passed through untouched."""

    reg = MockRegister(field=MockField(0))
    info = SimpleNamespace(is_interrupt_state=False)

    assert register_register_enhancement_hook(reg, info) is None


# ---------------------------------------------------------------------------
# Registry-fired hooks (auto-attach via Unit 1's _registry seams)
# ---------------------------------------------------------------------------


def test_interrupt_hooks_are_registered_with_registry() -> None:
    """Module import should wire both hooks into Unit 1's registry seams.

    The closer's smoke check verifies the same property by scanning
    ``__qualname__`` for the substring ``"interrupts"``; we assert the
    stronger identity match here so a future rename catches the test
    instead of breaking the smoke check.
    """

    post_create_hooks = _registry.get_post_create_hooks()
    register_enhancers = _registry.get_register_enhancers()

    assert interrupts_post_create in post_create_hooks
    assert interrupts_register_enhancement in register_enhancers
    # The smoke-check property the closer relies on.
    assert any("interrupts" in fn.__qualname__ for fn in post_create_hooks)
    assert any("interrupts" in fn.__qualname__ for fn in register_enhancers)


class _FakeSoc:
    """Hand-rolled SoC stand-in.

    Plain ``SimpleNamespace`` won't do here: the module-resolution path in
    :func:`interrupts._load_detected_groups` calls ``type(soc).__module__``
    and we need to make that point at our fake package without polluting
    every other test that uses ``SimpleNamespace``.
    """


def test_post_create_attaches_empty_tree_when_no_groups() -> None:
    """With no detection metadata and no manual groups, ``soc.interrupts``
    is still attached as an empty :class:`InterruptTree` rather than
    raising.

    The contract — laid out in the unit task — is that
    :func:`interrupts_post_create` is permissive: a SoC without detected
    interrupts must still expose ``soc.interrupts`` so callers can write
    ``len(soc.interrupts) == 0`` instead of probing for the attribute.
    """

    from peakrdl_pybind11.runtime import interrupts

    interrupts._GROUP_REGISTRY.clear()
    soc: Any = _FakeSoc()
    soc._module_name = "peakrdl_pybind11_no_such_module_xyzzy"

    tree = interrupts_post_create(soc)
    assert isinstance(tree, InterruptTree)
    assert soc.interrupts is tree
    assert len(tree) == 0
    # Pretty-printer survives an empty tree.
    rendered = tree.tree()
    assert "interrupts" in rendered


def test_post_create_attaches_tree_via_registry_fire() -> None:
    """End-to-end: pre-register a group, fire post-create through the
    registry, and walk ``soc.interrupts.tree()``.

    This exercises the same path the generated ``runtime.py.jinja``
    triggers from ``create()`` — a clean integration smoke test that
    ensures the module-level :func:`_registry.register_post_create` call
    actually wires the hook into the seam.
    """

    from peakrdl_pybind11.runtime import interrupts

    interrupts._GROUP_REGISTRY.clear()

    state = MockField(value=1, name="tx_done")
    enable = MockField(value=1, on_write="rw", name="tx_done")
    group = InterruptGroup(
        {"tx_done": InterruptSource(state, enable_field=enable, name="tx_done")}
    )
    register_interrupt_group("uart", group)

    soc: Any = _FakeSoc()
    soc._module_name = "peakrdl_pybind11_no_such_module_xyzzy"

    _registry.fire_post_create_hooks(soc)

    assert isinstance(soc.interrupts, InterruptTree)
    rendered = soc.interrupts.tree()
    assert "uart:" in rendered
    assert "tx_done" in rendered
    assert "state=1" in rendered

    interrupts._GROUP_REGISTRY.clear()


def test_register_enhancement_stashes_interrupt_metadata() -> None:
    """``interrupts_register_enhancement`` records interrupt metadata for
    later use by the post-create walk.

    Forward-compat: today Unit 23's exporter plugin emits
    ``interrupts_detected.py`` rather than threading metadata through the
    registry, so this stash sits idle. But the seam needs to work so a
    future Unit 23 revision can switch routes without another runtime
    change.
    """

    from peakrdl_pybind11.runtime import interrupts

    class _DummyReg:
        pass

    interrupts._CLASS_INTERRUPT_METADATA.pop(_DummyReg, None)
    payload = {"sources": ["tx_done"], "state_reg": "soc.uart.INTR_STATE"}
    interrupts_register_enhancement(_DummyReg, {"interrupts": payload})

    assert interrupts._CLASS_INTERRUPT_METADATA[_DummyReg] == payload

    # Non-interrupt classes are a no-op — the stash stays untouched.
    class _OtherReg:
        pass

    interrupts_register_enhancement(_OtherReg, {"writable": {"a": True}})
    assert _OtherReg not in interrupts._CLASS_INTERRUPT_METADATA

    interrupts._CLASS_INTERRUPT_METADATA.pop(_DummyReg, None)


def test_post_create_loads_detected_groups_from_module(monkeypatch: Any) -> None:
    """``interrupts_post_create`` imports ``<soc_pkg>.interrupts_detected``
    and builds groups from the detected-trio payload.

    The fake module mirrors the shape Unit 23 emits: a module-level
    ``interrupt_groups`` list of dicts with ``state_reg`` / ``enable_reg``
    / ``test_reg`` paths.
    """

    import sys
    from peakrdl_pybind11.runtime import interrupts

    interrupts._GROUP_REGISTRY.clear()

    # Build a fake SoC tree: ``soc.uart.INTR_STATE`` and friends, each
    # carrying mock fields the runtime can read/write.
    intr_state = MockRegister(
        tx_done=MockField(value=1),
        rx_overflow=MockField(value=0),
    )
    intr_enable = MockRegister(
        tx_done=MockField(value=1, on_write="rw"),
        rx_overflow=MockField(value=0, on_write="rw"),
    )
    intr_test = MockRegister(
        tx_done=MockField(value=0, on_write="rw"),
        rx_overflow=MockField(value=0, on_write="rw"),
    )
    uart = SimpleNamespace(
        INTR_STATE=intr_state,
        INTR_ENABLE=intr_enable,
        INTR_TEST=intr_test,
    )
    soc: Any = _FakeSoc()
    soc.uart = uart

    # Plant a fake module exposing the detection payload.
    fake_pkg_name = "peakrdl_pybind11_test_fake_soc_pkg"
    detected_module_name = f"{fake_pkg_name}.interrupts_detected"
    import types as _types

    pkg = _types.ModuleType(fake_pkg_name)
    pkg.__path__ = []  # type: ignore[attr-defined]  # pretend it's a package
    detected = _types.ModuleType(detected_module_name)
    detected.interrupt_groups = [  # type: ignore[attr-defined]
        {
            "path": "soc.uart",
            "state_reg": "soc.uart.INTR_STATE",
            "enable_reg": "soc.uart.INTR_ENABLE",
            "test_reg": "soc.uart.INTR_TEST",
            "sources": ["tx_done", "rx_overflow"],
        }
    ]
    monkeypatch.setitem(sys.modules, fake_pkg_name, pkg)
    monkeypatch.setitem(sys.modules, detected_module_name, detected)
    soc._module_name = fake_pkg_name

    tree = interrupts_post_create(soc)

    assert isinstance(tree, InterruptTree)
    # Group registered under leaf-of-parent-path: ``soc.uart`` -> ``"uart"``.
    assert "uart" in {g for g in tree.groups}
    rendered = tree.tree()
    assert "tx_done" in rendered
    # ``soc.uart.interrupts`` got the per-regfile attachment too.
    assert isinstance(uart.interrupts, InterruptGroup)

    interrupts._GROUP_REGISTRY.clear()
