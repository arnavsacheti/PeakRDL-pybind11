"""
Tests for ``peakrdl_pybind11.runtime.wait_poll``.

These tests are pure-Python and never import a generated chip module.
A small ``FakeNode`` exposes the same surface (``read()`` plus ``info.path``)
that the polling toolkit relies on.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterable
from typing import Any

import numpy as np
import pytest

from peakrdl_pybind11.errors import WaitTimeoutError
from peakrdl_pybind11.runtime import _registry as enhancement_registry
from peakrdl_pybind11.runtime import wait_poll
from peakrdl_pybind11.runtime.wait_poll import (
    aiowait,
    await_for,
    await_until,
    histogram,
    sample,
    wait_for,
    wait_until,
)


class _Info:
    """Minimal stand-in for the generated ``info`` object on a node."""

    def __init__(self, path: str) -> None:
        self.path = path


class FakeNode:
    """Test-double that mimics a Register/Field node's read surface.

    *values* is the canned sequence the node returns from ``read()``. When
    the sequence is exhausted, the last value is returned forever (a common
    "the bit eventually pins true" scenario).
    """

    def __init__(self, values: Iterable[Any], *, path: str = "soc.fake.node") -> None:
        self._values = list(values)
        if not self._values:
            raise ValueError("FakeNode needs at least one canned value")
        self._idx = 0
        self.info = _Info(path)
        self.read_count = 0

    def read(self) -> Any:
        self.read_count += 1
        v = self._values[min(self._idx, len(self._values) - 1)]
        self._idx += 1
        return v


# ---------------------------------------------------------------------------
# wait_for
# ---------------------------------------------------------------------------


class TestWaitFor:
    def test_returns_matched_value_first_poll(self) -> None:
        node = FakeNode([1])
        assert wait_for(node, 1, timeout=0.5) == 1
        assert node.read_count == 1

    def test_returns_matched_value_after_polls(self) -> None:
        node = FakeNode([0, 0, 0, 1, 99])
        result = wait_for(node, 1, timeout=1.0, period=0.0001)
        assert result == 1
        assert node.read_count == 4  # stopped at first match

    def test_timeout_raises_with_last_seen(self) -> None:
        node = FakeNode([0])
        with pytest.raises(WaitTimeoutError) as exc_info:
            wait_for(node, 1, timeout=0.05, period=0.005)
        err = exc_info.value
        assert err.path == "soc.fake.node"
        assert err.expected == 1
        assert err.last_seen == 0
        assert err.samples is None  # capture defaults False
        assert err.timeout == pytest.approx(0.05)
        assert err.polls is not None and err.polls >= 1

    def test_capture_true_populates_samples(self) -> None:
        node = FakeNode([0])
        with pytest.raises(WaitTimeoutError) as exc_info:
            wait_for(node, 1, timeout=0.05, period=0.005, capture=True)
        err = exc_info.value
        assert err.samples is not None
        assert isinstance(err.samples, list)
        assert len(err.samples) >= 1
        assert all(s == 0 for s in err.samples)
        assert err.polls == len(err.samples)

    def test_jitter_does_not_block(self) -> None:
        # Jitter should still respect the deadline; a never-matching wait
        # must terminate within ~timeout, not loop forever.
        node = FakeNode([0])
        with pytest.raises(WaitTimeoutError):
            wait_for(node, 1, timeout=0.05, period=0.001, jitter=True)

    def test_value_is_returned_unchanged(self) -> None:
        # The returned value is whatever read() produced, not a coerced int.
        sentinel = object()

        class WrapNode:
            def __init__(self) -> None:
                self.info = _Info("wrap.node")

            def read(self) -> object:
                return sentinel

        result = wait_for(WrapNode(), sentinel, timeout=0.1)
        assert result is sentinel


# ---------------------------------------------------------------------------
# wait_until
# ---------------------------------------------------------------------------


class TestWaitUntil:
    def test_returns_at_first_match(self) -> None:
        node = FakeNode([1, 2, 3, 17, 4])
        result = wait_until(node, lambda v: v >= 17, timeout=1.0, period=0.0001)
        assert result == 17
        assert node.read_count == 4

    def test_predicate_receives_value(self) -> None:
        seen: list[Any] = []
        node = FakeNode([5, 10])

        def pred(v: Any) -> bool:
            seen.append(v)
            return v == 10

        wait_until(node, pred, timeout=1.0, period=0.0001)
        assert seen == [5, 10]

    def test_timeout_raises(self) -> None:
        node = FakeNode([0])
        with pytest.raises(WaitTimeoutError) as exc_info:
            wait_until(node, lambda v: v == 99, timeout=0.05, period=0.005)
        assert exc_info.value.last_seen == 0
        assert exc_info.value.samples is None

    def test_capture_populates_samples(self) -> None:
        node = FakeNode([0])
        with pytest.raises(WaitTimeoutError) as exc_info:
            wait_until(
                node,
                lambda v: v == 99,
                timeout=0.05,
                period=0.005,
                capture=True,
            )
        err = exc_info.value
        assert err.samples is not None
        assert len(err.samples) >= 1


# ---------------------------------------------------------------------------
# sample / histogram
# ---------------------------------------------------------------------------


class TestSample:
    def test_returns_ndarray_of_length_n(self) -> None:
        node = FakeNode([7])  # always returns 7
        arr = sample(node, n=10)
        assert isinstance(arr, np.ndarray)
        assert arr.shape == (10,)
        assert (arr == 7).all()
        assert node.read_count == 10

    def test_samples_in_order(self) -> None:
        node = FakeNode([1, 2, 3, 4, 5])
        arr = sample(node, n=5)
        assert arr.tolist() == [1, 2, 3, 4, 5]

    def test_n_zero_returns_empty(self) -> None:
        node = FakeNode([1])
        arr = sample(node, n=0)
        assert isinstance(arr, np.ndarray)
        assert arr.shape == (0,)
        assert node.read_count == 0

    def test_n_negative_raises(self) -> None:
        node = FakeNode([1])
        with pytest.raises(ValueError):
            sample(node, n=-1)


class TestHistogram:
    def test_returns_counter(self) -> None:
        node = FakeNode([1, 2, 1, 2, 1])
        hist = histogram(node, n=5)
        assert isinstance(hist, Counter)
        assert hist[1] == 3
        assert hist[2] == 2

    def test_n_zero_returns_empty_counter(self) -> None:
        node = FakeNode([1])
        hist = histogram(node, n=0)
        assert isinstance(hist, Counter)
        assert sum(hist.values()) == 0


# ---------------------------------------------------------------------------
# async API
# ---------------------------------------------------------------------------


class TestAwaitFor:
    def test_returns_matched_value(self) -> None:
        node = FakeNode([0, 0, 1])

        async def runner() -> Any:
            return await await_for(node, 1, timeout=1.0, period=0.0001)

        assert asyncio.run(runner()) == 1

    def test_timeout_raises_inside_asyncio_run(self) -> None:
        node = FakeNode([0])

        async def runner() -> None:
            await await_for(node, 1, timeout=0.05, period=0.005)

        with pytest.raises(WaitTimeoutError) as exc_info:
            asyncio.run(runner())
        assert exc_info.value.last_seen == 0

    def test_loop_kwarg_is_accepted(self) -> None:
        # The ``loop=`` kwarg must be accepted for API compat with legacy
        # asyncio code, even though the current implementation ignores it.
        node = FakeNode([1])

        async def runner() -> Any:
            return await await_for(node, 1, timeout=0.5, loop=None)

        assert asyncio.run(runner()) == 1

    def test_capture_populates_samples(self) -> None:
        node = FakeNode([0])

        async def runner() -> None:
            await await_for(
                node, 1, timeout=0.05, period=0.005, capture=True
            )

        with pytest.raises(WaitTimeoutError) as exc_info:
            asyncio.run(runner())
        assert exc_info.value.samples is not None
        assert len(exc_info.value.samples) >= 1


class TestAwaitUntil:
    def test_returns_at_first_match(self) -> None:
        node = FakeNode([0, 5, 10])

        async def runner() -> Any:
            return await await_until(
                node, lambda v: v >= 5, timeout=1.0, period=0.0001
            )

        assert asyncio.run(runner()) == 5


class TestAioWait:
    def test_default_value_is_true(self) -> None:
        node = FakeNode([False, True])

        async def runner() -> Any:
            return await aiowait(node, timeout=1.0, period=0.0001)

        assert asyncio.run(runner()) is True

    def test_explicit_value(self) -> None:
        node = FakeNode([0, 1, 2, 3])

        async def runner() -> Any:
            return await aiowait(node, value=2, timeout=1.0, period=0.0001)

        assert asyncio.run(runner()) == 2


# ---------------------------------------------------------------------------
# enhancer wiring (Unit 1 seam)
# ---------------------------------------------------------------------------


class TestEnhancementRegistry:
    def test_register_enhancer_present(self) -> None:
        # Importing wait_poll should have registered enhancers on both the
        # Register and Field registries.
        register_enhancers = enhancement_registry.get_register_enhancers()
        field_enhancers = enhancement_registry.get_field_enhancers()
        assert wait_poll._enhance_register in register_enhancers
        assert wait_poll._enhance_field in field_enhancers

    def test_enhancer_attaches_methods(self) -> None:
        class FakeRegisterCls:
            pass

        wait_poll._enhance_register(FakeRegisterCls, {"path": "x"})

        # All public sync helpers attached.
        assert FakeRegisterCls.wait_for is wait_for
        assert FakeRegisterCls.wait_until is wait_until
        assert FakeRegisterCls.sample is sample
        assert FakeRegisterCls.histogram is histogram

        # Async helpers attached too.
        assert FakeRegisterCls.await_for is await_for
        assert FakeRegisterCls.await_until is await_until
        assert FakeRegisterCls.aiowait is aiowait

    def test_enhancer_attaches_to_field(self) -> None:
        class FakeFieldCls:
            pass

        wait_poll._enhance_field(FakeFieldCls, {"path": "x"})
        assert FakeFieldCls.wait_for is wait_for
        assert FakeFieldCls.sample is sample
