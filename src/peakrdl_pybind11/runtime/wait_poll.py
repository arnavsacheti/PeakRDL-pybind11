"""
Polling toolkit for the test author.

Implements §14 of ``docs/IDEAL_API_SKETCH.md``. The single most common test
idiom — *"wait until this bit becomes set"* — should be one method call, not
a hand-rolled loop with off-by-one timeouts and forgotten ``time.sleep``
calls. This module provides that one method and its close cousins:

* :func:`wait_for` — block until a node's read equals a target value.
* :func:`wait_until` — block until a predicate returns truthy on a fresh
  read of the node.
* :func:`sample` — capture *n* fresh reads and return them as an
  :class:`numpy.ndarray`.
* :func:`histogram` — sample *n* reads and return a
  :class:`collections.Counter` keyed by value.
* The async duals :func:`await_for`, :func:`await_until`, :func:`aiowait`.

A small enhancer wires every public function as a method onto the generated
``Register`` and ``Field`` classes via the ``register_register_enhancement`` /
``register_field_enhancement`` seam in :mod:`._registry`, so that user code
writes ``soc.uart.status.tx_ready.wait_for(True, timeout=1.0)`` directly.

Design notes
------------

* All sleeps use :func:`time.monotonic` for deadline arithmetic to be safe
  against system clock changes.
* ``jitter=True`` perturbs each sleep by ``random.uniform(0.8, 1.2)``.
* On timeout the wait raises :class:`peakrdl_pybind11.errors.WaitTimeoutError`
  with ``last_seen`` populated, and ``samples`` populated only when the
  caller passed ``capture=True``.
* The async functions accept a ``loop=`` parameter for API symmetry with
  legacy asyncio code paths but do not forward it to :func:`asyncio.sleep`
  (deprecated/removed since Python 3.10). The argument is ignored other
  than that.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from ..errors import WaitTimeoutError
from ._registry import register_field_enhancement, register_register_enhancement

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from numpy.typing import NDArray

__all__ = [
    "aiowait",
    "await_for",
    "await_until",
    "histogram",
    "sample",
    "wait_for",
    "wait_until",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _node_path(node: Any) -> str:
    """Return a human-readable path for *node* for use in error messages."""

    info = getattr(node, "info", None)
    if info is not None:
        path = getattr(info, "path", None)
        if path:
            return str(path)
    name = getattr(node, "name", None)
    if name:
        return str(name)
    return repr(node)


def _next_period(period: float, jitter: bool) -> float:
    """Return the next sleep duration, optionally jittered."""

    if not jitter:
        return period
    # Non-cryptographic jitter; ±20% spread keeps callers from aliasing with
    # periodic hardware events.
    return period * random.uniform(0.8, 1.2)


def _values_match(actual: Any, expected: Any) -> bool:
    """Return ``True`` when *actual* equals *expected*.

    Tolerates exotic comparators (NumPy ndarray, third-party value wrappers)
    that may raise on ``==``: such comparisons count as a non-match so the
    wait keeps polling instead of crashing the test author's loop.
    """

    try:
        return bool(actual == expected)
    except Exception:
        return False


def _predicate_label(predicate: Callable[[Any], Any]) -> str:
    """Best-effort string describing *predicate* for error messages."""

    return getattr(predicate, "__name__", None) or "<predicate>"


# ---------------------------------------------------------------------------
# core poll loops
# ---------------------------------------------------------------------------
#
# The sync and async polling loops differ only in two places: how they sleep
# (``time.sleep`` vs ``await asyncio.sleep``) and whether they're declared
# ``def`` vs ``async def``. Rather than write four near-identical loops we
# parametrise the *match* (value-equality vs predicate) and keep two loops
# total — one sync, one async.


def _poll_sync(
    node: Any,
    matches: Callable[[Any], bool],
    expected_for_error: Any,
    *,
    timeout: float,
    period: float,
    jitter: bool,
    capture: bool,
) -> Any:
    deadline = time.monotonic() + timeout
    samples: list[Any] | None = [] if capture else None
    polls = 0
    last_seen: Any = None

    while True:
        last_seen = node.read()
        polls += 1
        if samples is not None:
            samples.append(last_seen)
        if matches(last_seen):
            return last_seen
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WaitTimeoutError(
                _node_path(node),
                expected=expected_for_error,
                last_seen=last_seen,
                samples=samples,
                timeout=timeout,
                polls=polls,
            )
        sleep_for = min(_next_period(period, jitter), remaining)
        if sleep_for > 0:
            time.sleep(sleep_for)


async def _poll_async(
    node: Any,
    matches: Callable[[Any], bool],
    expected_for_error: Any,
    *,
    timeout: float,
    period: float,
    jitter: bool,
    capture: bool,
) -> Any:
    deadline = time.monotonic() + timeout
    samples: list[Any] | None = [] if capture else None
    polls = 0
    last_seen: Any = None

    while True:
        last_seen = node.read()
        polls += 1
        if samples is not None:
            samples.append(last_seen)
        if matches(last_seen):
            return last_seen
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WaitTimeoutError(
                _node_path(node),
                expected=expected_for_error,
                last_seen=last_seen,
                samples=samples,
                timeout=timeout,
                polls=polls,
            )
        sleep_for = min(_next_period(period, jitter), remaining)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)


# ---------------------------------------------------------------------------
# sync API
# ---------------------------------------------------------------------------


def wait_for(
    node: Any,
    value: Any,
    *,
    timeout: float = 1.0,
    period: float = 0.001,
    jitter: bool = False,
    capture: bool = False,
) -> Any:
    """Poll ``node.read()`` until it equals *value* or *timeout* elapses.

    Parameters
    ----------
    node
        The descriptor to poll. Must expose ``read()`` and (optionally)
        ``info.path`` for error messages.
    value
        The target value. ``actual == value`` is checked on every poll.
    timeout
        Hard deadline in seconds.
    period
        Nominal time between polls. Smaller is more responsive, larger is
        gentler on the bus.
    jitter
        When ``True``, multiply each sleep by ``random.uniform(0.8, 1.2)``.
    capture
        When ``True``, attach the full list of sampled values to the
        :class:`WaitTimeoutError` raised on timeout.

    Returns
    -------
    The matched value (whatever ``node.read()`` returned).

    Raises
    ------
    WaitTimeoutError
        If the deadline elapsed without a match. ``last_seen`` is populated
        with the most recent read; ``samples`` is populated when
        ``capture=True``.
    """

    return _poll_sync(
        node,
        lambda actual: _values_match(actual, value),
        value,
        timeout=timeout,
        period=period,
        jitter=jitter,
        capture=capture,
    )


def wait_until(
    node: Any,
    predicate: Callable[[Any], Any],
    *,
    timeout: float = 1.0,
    period: float = 0.001,
    jitter: bool = False,
    capture: bool = False,
) -> Any:
    """Poll ``node.read()`` until *predicate* returns truthy.

    The predicate receives whatever ``node.read()`` produced — typically a
    :class:`RegisterValue` whose field accessors return the same typed
    values you would get from a direct field read.
    """

    return _poll_sync(
        node,
        lambda actual: bool(predicate(actual)),
        _predicate_label(predicate),
        timeout=timeout,
        period=period,
        jitter=jitter,
        capture=capture,
    )


def sample(node: Any, n: int) -> NDArray[Any]:
    """Issue *n* fresh reads of *node* and return the values as ndarray.

    The returned array's dtype is whatever NumPy infers from the readings.
    Callers that need a specific dtype should cast explicitly.
    """

    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    return np.array([node.read() for _ in range(n)])


def histogram(node: Any, n: int) -> Counter[Any]:
    """Sample *n* fresh reads of *node* and bucket them into a ``Counter``.

    Avoids materialising an intermediate ndarray so it works with read
    payloads that are unhashable as a NumPy dtype but hashable as Python
    objects (e.g. enum members).
    """

    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    counter: Counter[Any] = Counter()
    for _ in range(n):
        counter[node.read()] += 1
    return counter


# ---------------------------------------------------------------------------
# async API
# ---------------------------------------------------------------------------


async def await_for(
    node: Any,
    value: Any,
    *,
    timeout: float = 1.0,
    period: float = 0.001,
    jitter: bool = False,
    capture: bool = False,
    loop: asyncio.AbstractEventLoop | None = None,
) -> Any:
    """Async dual of :func:`wait_for`.

    The ``loop`` argument is accepted for API symmetry with legacy asyncio
    code but is not forwarded to :func:`asyncio.sleep` — the explicit-loop
    parameter was removed from most asyncio APIs in Python 3.10+.
    """

    return await _poll_async(
        node,
        lambda actual: _values_match(actual, value),
        value,
        timeout=timeout,
        period=period,
        jitter=jitter,
        capture=capture,
    )


async def await_until(
    node: Any,
    predicate: Callable[[Any], Any],
    *,
    timeout: float = 1.0,
    period: float = 0.001,
    jitter: bool = False,
    capture: bool = False,
    loop: asyncio.AbstractEventLoop | None = None,
) -> Any:
    """Async dual of :func:`wait_until`."""

    return await _poll_async(
        node,
        lambda actual: bool(predicate(actual)),
        _predicate_label(predicate),
        timeout=timeout,
        period=period,
        jitter=jitter,
        capture=capture,
    )


async def aiowait(
    node: Any,
    value: Any = True,
    *,
    timeout: float = 1.0,
    period: float = 0.001,
    jitter: bool = False,
    capture: bool = False,
    loop: asyncio.AbstractEventLoop | None = None,
) -> Any:
    """Async wait shortcut, primarily for IRQ sources.

    Equivalent to ``await await_for(node, value, ...)`` but takes a default
    target of ``True`` so the most common interrupt-pending wait reads as
    ``await soc.uart.interrupts.tx_done.aiowait(timeout=...)``.
    """

    return await await_for(
        node,
        value,
        timeout=timeout,
        period=period,
        jitter=jitter,
        capture=capture,
        loop=loop,
    )


# ---------------------------------------------------------------------------
# enhancers — wire helpers as methods on generated classes
# ---------------------------------------------------------------------------


_POLL_METHODS: tuple[tuple[str, Any], ...] = (
    ("wait_for", wait_for),
    ("wait_until", wait_until),
    ("sample", sample),
    ("histogram", histogram),
    ("await_for", await_for),
    ("await_until", await_until),
    ("aiowait", aiowait),
)


def _attach_poll_methods(cls: type) -> None:
    for name, fn in _POLL_METHODS:
        setattr(cls, name, fn)


@register_register_enhancement
def _enhance_register(cls: type, _metadata: dict[str, Any]) -> None:
    """Attach the polling toolkit to a generated Register class."""

    _attach_poll_methods(cls)


@register_field_enhancement
def _enhance_field(cls: type, _metadata: dict[str, Any]) -> None:
    """Attach the polling toolkit to a generated Field class.

    Predicate-style ``wait_until`` is exposed here too: tests routinely
    write ``field.wait_until(lambda v: v > 16)`` and the implementation
    handles it identically to register-scoped predicates.
    """

    _attach_poll_methods(cls)
