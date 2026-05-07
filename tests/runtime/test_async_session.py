"""Unit tests for ``peakrdl_pybind11.runtime.async_session`` (Unit 22).

These tests exercise the async dual surface entirely with a Python
``_FakeSoc``: a hand-rolled tree of accessor nodes whose sync ``read`` /
``write`` / ``modify`` / ``wait`` methods record what they were called
with. No generated module, no native master -- async is sync wrapped in
a thread executor, so the dual is fully exercisable in pure Python.

Coverage matches the task brief:

1. ``async with soc.async_session() as s: await s.uart.control.aread()``
   returns the same value as the sync ``read()``.
2. ``await s.uart.control.awrite(0x42)`` issues a write.
3. Multiple awaits run concurrently in the executor (verified with both
   ``asyncio.gather`` against a single-worker default *and* a multi-
   worker pool that overlaps ``time.sleep``).
4. The thread pool is properly shut down on ``__aexit__``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from peakrdl_pybind11.runtime.async_session import (
    AsyncSession,
    register_post_create,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeReg:
    """Minimal stand-in for a generated register accessor."""

    def __init__(self, path: str, value: int = 0, *, work_seconds: float = 0.0) -> None:
        self.path = path
        self._value = value
        self._work_seconds = work_seconds
        self.read_calls = 0
        self.write_calls: list[tuple[int, dict[str, Any]]] = []
        self.modify_calls: list[dict[str, Any]] = []
        self.wait_calls: list[dict[str, Any]] = []
        self.read_threads: list[int] = []

    # The four sync ops the dual mirrors, plus path/address introspection.
    def read(self) -> int:
        self.read_calls += 1
        self.read_threads.append(threading.get_ident())
        if self._work_seconds:
            time.sleep(self._work_seconds)
        return self._value

    def write(self, value: int, *, mask: int | None = None) -> None:
        self.write_calls.append((value, {"mask": mask}))
        if mask is None:
            self._value = value
        else:
            self._value = (self._value & ~mask) | (value & mask)

    def modify(self, **fields: int) -> int:
        self.modify_calls.append(dict(fields))
        # Pretend each field is one bit; just OR them in for the test.
        for v in fields.values():
            self._value |= int(v)
        return self._value

    def wait(self, *, timeout: float = 0.0) -> bool:
        self.wait_calls.append({"timeout": timeout})
        return True

    @property
    def value(self) -> int:
        return self._value


class _FakeUart:
    """Container for a couple of registers; mimics a regfile."""

    def __init__(self) -> None:
        self.control = _FakeReg("uart.control", value=0x1234)
        self.status = _FakeReg("uart.status", value=0xCAFE)


class _FakeSoc:
    """Minimal SoC analogue with two regfiles -- enough to chain."""

    def __init__(self) -> None:
        self.uart = _FakeUart()
        self.spi = _FakeUart()
        # A scalar attribute to confirm the dual passes primitives through
        # without wrapping them as proxies.
        self.name: str = "fake_soc"


@pytest.fixture
def soc() -> _FakeSoc:
    return register_post_create(_FakeSoc())


# ---------------------------------------------------------------------------
# Wiring & context manager protocol
# ---------------------------------------------------------------------------


class TestRegisterPostCreate:
    def test_attaches_async_session_factory(self, soc: _FakeSoc) -> None:
        # The seam binds a callable named ``async_session``.
        assert callable(soc.async_session)  # type: ignore[attr-defined]
        session = soc.async_session()  # type: ignore[attr-defined]
        assert isinstance(session, AsyncSession)

    def test_factory_returns_a_new_session_each_call(self, soc: _FakeSoc) -> None:
        s1 = soc.async_session()  # type: ignore[attr-defined]
        s2 = soc.async_session()  # type: ignore[attr-defined]
        assert s1 is not s2

    def test_re_register_is_idempotent(self, soc: _FakeSoc) -> None:
        # Re-binding shouldn't break or pile up state.
        register_post_create(soc)
        register_post_create(soc)
        assert callable(soc.async_session)  # type: ignore[attr-defined]


class TestContextManager:
    def test_enter_returns_self(self, soc: _FakeSoc) -> None:
        async def _run() -> None:
            session = soc.async_session()  # type: ignore[attr-defined]
            entered = await session.__aenter__()
            try:
                assert entered is session
            finally:
                await session.__aexit__(None, None, None)

        asyncio.run(_run())

    def test_async_with_yields_session(self, soc: _FakeSoc) -> None:
        async def _run() -> AsyncSession:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                return s

        s = asyncio.run(_run())
        assert isinstance(s, AsyncSession)


# ---------------------------------------------------------------------------
# Read / write / modify / wait forwarding
# ---------------------------------------------------------------------------


class TestForwardedOps:
    def test_aread_returns_same_value_as_sync_read(self, soc: _FakeSoc) -> None:
        async def _run() -> int:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                return await s.uart.control.aread()

        result = asyncio.run(_run())
        assert result == soc.uart.control.read()
        # ``read()`` was called once via the dual and once just now.
        assert soc.uart.control.read_calls == 2

    def test_awrite_issues_write(self, soc: _FakeSoc) -> None:
        async def _run() -> None:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                await s.uart.control.awrite(0x42)

        asyncio.run(_run())
        assert soc.uart.control.write_calls == [(0x42, {"mask": None})]
        assert soc.uart.control.value == 0x42

    def test_awrite_passes_kwargs_through(self, soc: _FakeSoc) -> None:
        async def _run() -> None:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                await s.uart.control.awrite(0xFF, mask=0x0F)

        asyncio.run(_run())
        assert soc.uart.control.write_calls == [(0xFF, {"mask": 0x0F})]

    def test_amodify_passes_kwargs_through(self, soc: _FakeSoc) -> None:
        async def _run() -> int:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                return await s.uart.control.amodify(enable=1, parity=2)

        result = asyncio.run(_run())
        assert soc.uart.control.modify_calls == [{"enable": 1, "parity": 2}]
        # The fake's modify ORs each value into its register; result is
        # the post-modify value the sync surface returns.
        assert result == soc.uart.control.value

    def test_aiowait_forwards_to_sync_wait(self, soc: _FakeSoc) -> None:
        async def _run() -> bool:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                return await s.uart.control.aiowait(timeout=0.5)

        result = asyncio.run(_run())
        assert result is True
        assert soc.uart.control.wait_calls == [{"timeout": 0.5}]


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_gather_collects_all_results_with_default_executor(
        self, soc: _FakeSoc
    ) -> None:
        # Default executor is single-worker; ``gather`` still must not
        # deadlock or drop results.
        async def _run() -> tuple[int, int]:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                return await asyncio.gather(  # type: ignore[return-value]
                    s.uart.control.aread(),
                    s.uart.status.aread(),
                )

        a, b = asyncio.run(_run())
        assert a == soc.uart.control.value
        assert b == soc.uart.status.value

    def test_multi_worker_pool_overlaps_blocking_work(self) -> None:
        # With four workers and a blocking sync sleep, two awaits should
        # finish in roughly half the wall clock of running them serially.
        # Relax the bound generously to keep CI happy.
        soc = register_post_create(_FakeSoc())
        soc.uart.control = _FakeReg("uart.control", value=1, work_seconds=0.1)  # type: ignore[attr-defined]
        soc.uart.status = _FakeReg("uart.status", value=2, work_seconds=0.1)  # type: ignore[attr-defined]

        executor = ThreadPoolExecutor(max_workers=4)

        async def _run() -> tuple[float, list[int]]:
            async with soc.async_session(executor=executor) as s:  # type: ignore[attr-defined]
                start = time.monotonic()
                values = await asyncio.gather(
                    s.uart.control.aread(),
                    s.uart.status.aread(),
                )
                elapsed = time.monotonic() - start
                return elapsed, list(values)

        try:
            elapsed, values = asyncio.run(_run())
        finally:
            # We own ``executor``; the session must not have shut it
            # down. (Asserted explicitly below.)
            executor.shutdown(wait=True)
        assert values == [1, 2]
        # 0.10s * 2 serial vs ~0.10s parallel -> bound at 0.18s.
        assert elapsed < 0.18, f"expected concurrent execution, got {elapsed:.3f}s"

    def test_runs_on_executor_thread_not_event_loop_thread(
        self, soc: _FakeSoc
    ) -> None:
        async def _run() -> int:
            loop_thread = threading.get_ident()
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                await s.uart.control.aread()
            return loop_thread

        loop_thread = asyncio.run(_run())
        assert soc.uart.control.read_threads, "read should have been recorded"
        # Every recorded read must have happened off the event-loop thread.
        assert all(t != loop_thread for t in soc.uart.control.read_threads)


# ---------------------------------------------------------------------------
# Executor lifecycle
# ---------------------------------------------------------------------------


class TestExecutorLifecycle:
    def test_default_executor_is_shut_down_on_aexit(self, soc: _FakeSoc) -> None:
        captured: dict[str, Any] = {}

        async def _run() -> None:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                captured["executor"] = s.executor
                # Do at least one op so the worker thread is alive.
                await s.uart.control.aread()

        asyncio.run(_run())
        executor: ThreadPoolExecutor = captured["executor"]
        # ``submit`` raises ``RuntimeError`` once an executor has been
        # shut down. (Confirmed on stdlib in 3.10+.)
        with pytest.raises(RuntimeError):
            executor.submit(lambda: None)

    def test_caller_owned_executor_is_left_running(self, soc: _FakeSoc) -> None:
        executor = ThreadPoolExecutor(max_workers=2)

        async def _run() -> None:
            async with soc.async_session(executor=executor) as s:  # type: ignore[attr-defined]
                await s.uart.control.aread()

        try:
            asyncio.run(_run())
            # Executor should still accept work after the session closes.
            future = executor.submit(lambda: 42)
            assert future.result(timeout=1.0) == 42
        finally:
            executor.shutdown(wait=True)

    def test_aexit_idempotent_on_repeat(self, soc: _FakeSoc) -> None:
        # If a misbehaving caller manually invokes ``__aexit__`` twice,
        # the second call must not re-shutdown a now-dead executor.
        async def _run() -> None:
            session = soc.async_session()  # type: ignore[attr-defined]
            await session.__aenter__()
            await session.__aexit__(None, None, None)
            await session.__aexit__(None, None, None)

        asyncio.run(_run())  # must not raise


# ---------------------------------------------------------------------------
# Lazy mirror & introspection
# ---------------------------------------------------------------------------


class TestLazyMirror:
    def test_attribute_chain_walks_lazily(self, soc: _FakeSoc) -> None:
        async def _run() -> None:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                # Touching ``uart`` shouldn't force ``spi`` to materialize.
                node = s.uart
                assert "spi" not in s._node_cache  # noqa: SLF001
                # Subsequent access returns the same proxy.
                assert s.uart is node

        asyncio.run(_run())

    def test_dir_mirrors_underlying_attrs(self, soc: _FakeSoc) -> None:
        async def _run() -> list[str]:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                return dir(s)

        names = asyncio.run(_run())
        assert "uart" in names
        assert "spi" in names

    def test_dir_on_node_includes_async_methods(self, soc: _FakeSoc) -> None:
        async def _run() -> list[str]:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                return dir(s.uart.control)

        names = asyncio.run(_run())
        assert {"aread", "awrite", "amodify", "aiowait"}.issubset(set(names))

    def test_unknown_attribute_raises_attribute_error(
        self, soc: _FakeSoc
    ) -> None:
        async def _run() -> None:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                with pytest.raises(AttributeError):
                    _ = s.does_not_exist

        asyncio.run(_run())

    def test_scalar_attribute_passes_through(self, soc: _FakeSoc) -> None:
        # ``soc.name`` is a plain string; the proxy must not wrap it.
        async def _run() -> Any:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                return s.name

        # ``name`` lives directly on the SoC, so the dual returns the str.
        result = asyncio.run(_run())
        assert result == "fake_soc"

    def test_node_sync_property_returns_underlying(self, soc: _FakeSoc) -> None:
        async def _run() -> Any:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                return s.uart.control.sync

        underlying = asyncio.run(_run())
        assert underlying is soc.uart.control

    def test_session_soc_property(self, soc: _FakeSoc) -> None:
        async def _run() -> Any:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                return s.soc

        assert asyncio.run(_run()) is soc

    def test_missing_sync_method_surfaces_on_async(self, soc: _FakeSoc) -> None:
        # If the underlying node doesn't have ``modify``, the async
        # surface should report it cleanly rather than failing inside the
        # executor.
        class _NoModifyReg:
            def read(self) -> int:
                return 7

        soc.special = _NoModifyReg()  # type: ignore[attr-defined]

        async def _run() -> None:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                with pytest.raises(AttributeError, match="modify"):
                    _ = s.special.amodify

        asyncio.run(_run())

    def test_async_forwarder_is_cached_per_node(self, soc: _FakeSoc) -> None:
        # A poll loop calling ``s.uart.control.aread()`` 1000 times must
        # not rebuild the forwarder on each access; reuse the cached
        # closure so attribute-access cost stays O(1).
        async def _run() -> tuple[Any, Any, Any, Any]:
            async with soc.async_session() as s:  # type: ignore[attr-defined]
                ctrl = s.uart.control
                a1 = ctrl.aread
                a2 = ctrl.aread
                # Cross-method: each async name caches its own forwarder.
                w1 = ctrl.awrite
                w2 = ctrl.awrite
                return a1, a2, w1, w2

        a1, a2, w1, w2 = asyncio.run(_run())
        assert a1 is a2
        assert w1 is w2
        assert a1 is not w1
