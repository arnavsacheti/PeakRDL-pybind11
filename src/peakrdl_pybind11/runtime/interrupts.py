"""Interrupt runtime — Unit 12.

Implements the marquee runtime described in ``docs/IDEAL_API_SKETCH.md`` §9:
:class:`InterruptSource` wraps a single ``intr`` field and the optional
enable/test partners; :class:`InterruptGroup` groups sources that live under
the same trio (``INTR_STATE`` / ``INTR_ENABLE`` / ``INTR_TEST``).

Detection of the trio (the exporter plugin work) is Unit 23. This module
consumes the detected metadata and is also usable manually via
:meth:`InterruptGroup.manual` for the (rare) chips where auto-detection
fails.

The runtime is bus-aware via the field objects it is handed — every read
and write goes through the same masters as the rest of the API. There is no
direct register/master wiring here; all I/O is delegated to the field
objects passed in. That keeps the unit testable with a tiny mock instead of
the full generated tree.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Protocol, cast, runtime_checkable

from .errors import NotSupportedError, WaitTimeoutError

logger = logging.getLogger("peakrdl_pybind11.runtime.interrupts")

__all__ = [
    "FieldLike",
    "InterruptGroup",
    "InterruptSource",
    "InterruptTree",
    "interrupts_post_create",
    "interrupts_register_enhancement",
    "register_interrupt_group",
    "register_post_create_hook",
    "register_register_enhancement_hook",
]


# ---------------------------------------------------------------------------
# Protocol — what we need from a field object.
#
# The generated tree's field classes already expose ``read()``/``write()`` and
# carry ``info`` metadata under Unit 4; we duck-type against that so this
# unit can be tested with a trivial mock and still slot into the real tree
# without ceremony. The minimum surface is intentionally small.
# ---------------------------------------------------------------------------


@runtime_checkable
class FieldLike(Protocol):
    """Subset of the generated field API consumed by the interrupt runtime.

    Every interrupt-bearing field on the generated tree satisfies this
    protocol; tests stub it with a minimal mock.
    """

    def read(self) -> int: ...

    def write(self, value: int) -> None: ...


def _lookup_or_attr_error(obj: Any, attr_name: str, mapping_name: str, kind: str) -> object:
    """Resolve ``obj.<attr_name>`` against the mapping at ``obj.<mapping_name>``.

    Shared by :class:`InterruptGroup` and :class:`InterruptTree` to give a
    consistent ``did-you-mean``-style :class:`AttributeError` and to dodge
    recursion during partial init (Python pings ``__getattr__`` for dunder
    lookups before ``__init__`` finishes).
    """

    if attr_name.startswith("_"):
        raise AttributeError(attr_name)
    try:
        mapping = object.__getattribute__(obj, mapping_name)
    except AttributeError as exc:
        raise AttributeError(attr_name) from exc
    try:
        return mapping[attr_name]
    except KeyError as exc:
        raise AttributeError(f"{kind} has no {attr_name!r}; known: {sorted(mapping)}") from exc


def _info_tag(field: FieldLike | None, key: str, default: str = "") -> str:
    """Best-effort extraction of an ``info.<key>`` side-effect tag.

    The Unit 4 ``info`` block exposes SystemRDL ``onwrite``/``onread`` as
    ``info.on_write`` / ``info.on_read`` (e.g. ``"woclr"`` for write-1-to-
    clear). Tolerates fields without ``info`` and returns ``default``,
    which lets the runtime stay useful before Unit 4's seam reaches every
    field.
    """

    if field is None:
        return ""
    info = getattr(field, "info", None)
    if info is None:
        return default
    value = getattr(info, key, None)
    return value if isinstance(value, str) else default


# ---------------------------------------------------------------------------
# InterruptSource
# ---------------------------------------------------------------------------


class InterruptSource:
    """A single interrupt — typically one bit of an ``INTR_STATE`` register.

    Wraps the state field plus the optional enable and test partner fields.
    Mirrors the surface in §9.1: ``is_pending``/``is_enabled``/``enable``/
    ``disable``/``clear``/``acknowledge``/``fire`` plus the wait family
    (``wait``, ``wait_clear``, ``aiowait``, ``poll``) and the subscription
    helper ``on_fire``.
    """

    def __init__(
        self,
        state_field: FieldLike,
        enable_field: FieldLike | None = None,
        test_field: FieldLike | None = None,
        name: str = "",
        *,
        mask_field: FieldLike | None = None,
        haltmask_field: FieldLike | None = None,
        haltenable_field: FieldLike | None = None,
    ) -> None:
        self._state_field = state_field
        self._enable_field = enable_field
        self._test_field = test_field
        self._mask_field = mask_field
        self._haltmask_field = haltmask_field
        self._haltenable_field = haltenable_field
        self._name = name

    # -- introspection ------------------------------------------------------

    @property
    def name(self) -> str:
        """Source name as seen on the parent group (e.g. ``"tx_done"``)."""

        return self._name

    @property
    def state_field(self) -> FieldLike:
        return self._state_field

    @property
    def enable_field(self) -> FieldLike | None:
        return self._enable_field

    @property
    def test_field(self) -> FieldLike | None:
        return self._test_field

    @property
    def mask_field(self) -> FieldLike | None:
        return self._mask_field

    @property
    def haltmask_field(self) -> FieldLike | None:
        return self._haltmask_field

    @property
    def haltenable_field(self) -> FieldLike | None:
        return self._haltenable_field

    def __repr__(self) -> str:
        return f"InterruptSource(name={self._name!r})"

    # -- state queries ------------------------------------------------------

    def is_pending(self) -> bool:
        """``True`` iff the state bit currently reads as 1."""

        return bool(self._state_field.read())

    def is_enabled(self) -> bool:
        """``True`` iff the enable bit reads as 1, or no enable partner exists.

        Sources without an enable partner are always considered enabled —
        there is no way to mask them, so reporting them as disabled would be
        misleading.
        """

        if self._enable_field is None:
            return True
        return bool(self._enable_field.read())

    # -- mutators -----------------------------------------------------------

    def enable(self) -> None:
        """Set the enable bit. Raises if there is no enable partner."""

        if self._enable_field is None:
            raise NotImplementedError(f"interrupt {self._name!r} has no enable field")
        self._enable_field.write(1)

    def disable(self) -> None:
        """Clear the enable bit. Raises if there is no enable partner."""

        if self._enable_field is None:
            raise NotImplementedError(f"interrupt {self._name!r} has no enable field")
        self._enable_field.write(0)

    # -- mask / haltmask / haltenable partners (§9.4) -----------------------
    #
    # Three companion registers extend the basic ENABLE/STATE/TEST trio:
    # ``INTR_MASK`` (and ``MSK``/``MASK`` variants) disables specific
    # interrupts even when ENABLE is set; ``INTR_HALTMASK`` /
    # ``INTR_HALTENABLE`` are debug-halt variants. The runtime exposes
    # symmetric mutators for each, and raises :class:`NotSupportedError`
    # when the matching partner field is absent on this source.

    def mask(self) -> None:
        """Set the mask bit (block this interrupt). Raises if no mask partner."""

        if self._mask_field is None:
            raise NotSupportedError(f"interrupt {self._name!r} has no INTR_MASK companion")
        self._mask_field.write(1)

    def unmask(self) -> None:
        """Clear the mask bit (unblock). Raises if no mask partner."""

        if self._mask_field is None:
            raise NotSupportedError(f"interrupt {self._name!r} has no INTR_MASK companion")
        self._mask_field.write(0)

    def halt_mask(self) -> None:
        """Set the debug-halt mask bit. Raises if no haltmask partner."""

        if self._haltmask_field is None:
            raise NotSupportedError(f"interrupt {self._name!r} has no INTR_HALTMASK companion")
        self._haltmask_field.write(1)

    def halt_unmask(self) -> None:
        """Clear the debug-halt mask bit. Raises if no haltmask partner."""

        if self._haltmask_field is None:
            raise NotSupportedError(f"interrupt {self._name!r} has no INTR_HALTMASK companion")
        self._haltmask_field.write(0)

    def halt_enable(self) -> None:
        """Set the debug-halt enable bit. Raises if no haltenable partner."""

        if self._haltenable_field is None:
            raise NotSupportedError(f"interrupt {self._name!r} has no INTR_HALTENABLE companion")
        self._haltenable_field.write(1)

    def halt_disable(self) -> None:
        """Clear the debug-halt enable bit. Raises if no haltenable partner."""

        if self._haltenable_field is None:
            raise NotSupportedError(f"interrupt {self._name!r} has no INTR_HALTENABLE companion")
        self._haltenable_field.write(0)

    def clear(self) -> None:
        """Clear the pending state bit, doing the right thing per RDL.

        - ``woclr`` (write-1-to-clear, OpenTitan default): write 1.
        - ``wzc`` (write-0-to-clear, rare): write 0.
        - ``rclr`` (read-to-clear): the bit clears on read, so just read it.
        - Anything else falls through to the OpenTitan default of writing 1,
          which is what the overwhelming majority of vendor RDL emits.
        """

        on_write = _info_tag(self._state_field, "on_write", "woclr")
        on_read = _info_tag(self._state_field, "on_read")
        if on_write == "wzc":
            self._state_field.write(0)
            return
        if on_read in ("rclr", "rset"):
            # Read is the documented clear mechanism; no write needed.
            self._state_field.read()
            return
        # woclr (the OpenTitan default) and any unspecified field.
        self._state_field.write(1)

    def acknowledge(self) -> None:
        """Alias for :meth:`clear`. Matches the §9.1 sketch."""

        self.clear()

    def fire(self) -> None:
        """Software self-trigger via the test partner (write 1).

        Raises if there is no test partner — there's no portable way to
        synthesize a hardware interrupt without one.
        """

        if self._test_field is None:
            raise NotImplementedError(f"interrupt {self._name!r} has no test field")
        self._test_field.write(1)

    # -- wait / poll --------------------------------------------------------

    def wait(self, timeout: float = 1.0, period: float = 0.001) -> None:
        """Block until the source is pending or ``timeout`` elapses.

        Raises :class:`WaitTimeoutError` on timeout. ``period`` is the polling
        interval; the default of 1 ms balances bus traffic against latency.
        """

        self._wait_for(True, timeout=timeout, period=period)

    def wait_clear(self, timeout: float = 1.0, period: float = 0.001) -> None:
        """Block until the source is *not* pending or ``timeout`` elapses."""

        self._wait_for(False, timeout=timeout, period=period)

    async def aiowait(self, timeout: float = 1.0, period: float = 0.001) -> None:
        """Async variant of :meth:`wait`. Sleeps via :func:`asyncio.sleep`.

        Uses :func:`asyncio.wait_for` for the timeout shape; the inner
        coroutine polls :meth:`is_pending` with ``asyncio.sleep(period)``
        between checks so cancellation propagates cleanly.

        Raises :class:`WaitTimeoutError` on timeout — matches the synchronous
        :meth:`wait` for consistency with the rest of the wait family. The
        underlying ``asyncio.TimeoutError`` is translated so callers don't
        have to special-case the async path.
        """

        try:
            await asyncio.wait_for(self._aio_poll_until_pending(period), timeout=timeout)
        except asyncio.TimeoutError:
            raise WaitTimeoutError(f"interrupts.{self._name}", True, self.is_pending()) from None

    async def _aio_poll_until_pending(self, period: float) -> None:
        """Poll :meth:`is_pending` with async sleeps until the bit is set.

        Pulled out as a helper so :meth:`aiowait` can wrap it with
        :func:`asyncio.wait_for` for the timeout shape. Cancellation
        propagates through :func:`asyncio.sleep`, which is the standard
        asyncio cancellation point.
        """

        while not self.is_pending():
            await asyncio.sleep(period)

    def poll(self, period: float = 0.001, timeout: float = 1.0) -> None:
        """Explicit-period poll; alias for :meth:`wait` with both knobs.

        Argument order matches §9.1 (``period`` first), distinct from
        :meth:`wait` whose primary knob is ``timeout``.
        """

        self._wait_for(True, timeout=timeout, period=period)

    def _wait_for(self, target: bool, *, timeout: float, period: float) -> None:
        """Polling loop shared by ``wait`` / ``wait_clear`` / ``poll``.

        Matches the §14 contract: returns once the target value is observed,
        else raises :class:`WaitTimeoutError` carrying the last value seen.
        """

        deadline = time.monotonic() + timeout
        last_seen = self.is_pending()
        while last_seen != target:
            if time.monotonic() >= deadline:
                raise WaitTimeoutError(f"interrupts.{self._name}", target, last_seen)
            time.sleep(period)
            last_seen = self.is_pending()

    # -- subscription -------------------------------------------------------

    def on_fire(
        self,
        callback: Callable[[], None],
        *,
        period: float = 0.001,
    ) -> Callable[[], None]:
        """Register ``callback`` to fire when the source becomes pending.

        Polls in a daemon thread, debouncing on the 0→1 edge so the callback
        fires once per assertion (not once per poll while the bit is high).
        Returns an unsubscribe callable.

        Master-driven IRQ delivery is intentionally a future hook: backends
        that natively expose interrupts will swap out this body, but the
        polling fallback works against any master that can read the state
        register.
        """

        # Backends with native IRQ delivery install themselves via the
        # package-level ``_irq_backend``; polling is the fallback.
        if _irq_backend is not None:
            return _irq_backend(self, callback)

        stop = threading.Event()
        prev_pending = self.is_pending()

        def _loop() -> None:
            nonlocal prev_pending
            while not stop.is_set():
                try:
                    pending_now = self.is_pending()
                except Exception:
                    # Backend hiccup; stop the poller silently.
                    return
                # Rising-edge only — don't spam while the source stays asserted.
                if pending_now and not prev_pending:
                    try:
                        callback()
                    except Exception:
                        # Never let a callback crash kill the poller thread.
                        pass
                prev_pending = pending_now
                if stop.wait(period):
                    return

        thread = threading.Thread(
            target=_loop,
            name=f"on_fire[{self._name or 'interrupt'}]",
            daemon=True,
        )
        thread.start()

        def _unsubscribe() -> None:
            stop.set()

        return _unsubscribe


# ---------------------------------------------------------------------------
# Backend hook seam — for native IRQ-capable masters (no installer here).
#
# Masters that learn to wake on real interrupts (rather than poll) install
# themselves by overwriting ``_irq_backend``. The hook receives ``(source,
# callback)`` and returns an unsubscribe — same shape as the polling
# fallback returns from :meth:`InterruptSource.on_fire`.
# ---------------------------------------------------------------------------

_IRQBackendHook = Callable[["InterruptSource", Callable[[], None]], Callable[[], None]]
_irq_backend: _IRQBackendHook | None = None


# ---------------------------------------------------------------------------
# InterruptGroup
# ---------------------------------------------------------------------------


class InterruptGroup:
    """A group of related :class:`InterruptSource` objects.

    Typical case: every field of one ``INTR_STATE`` register lives in one
    group — that's how OpenTitan, ARM, RISC-V CLIC, etc. lay out their IRQs.

    Sources are reachable both as named attributes (``group.tx_done``) and
    by iteration. The §9.2 group helpers (``pending``, ``enabled``,
    ``clear_all``, ``disable_all``, ``enable``, ``snapshot``) operate on
    the union.
    """

    def __init__(self, sources: Mapping[str, InterruptSource]) -> None:
        # Stamp the canonical name on each source so ``InterruptSource.name``
        # reflects the key the user reaches them under, even when callers
        # pass freshly-constructed sources without ``name=`` set.
        validated: dict[str, InterruptSource] = {}
        for key, source in sources.items():
            if not source.name:
                source._name = key
            validated[key] = source
        self._sources: dict[str, InterruptSource] = validated

    # -- factories ----------------------------------------------------------

    @classmethod
    def manual(
        cls,
        state: Any,
        enable: object | None = None,
        test: object | None = None,
        *,
        mask: object | None = None,
        haltmask: object | None = None,
        haltenable: object | None = None,
    ) -> InterruptGroup:
        """Build a group from raw register objects when auto-detect failed.

        Matches sources by field name across the trio plus the §9.4
        companion partners. ``state`` is required (it's where the pending
        bits live); every other partner is optional and matched by
        best-effort name lookup so missing partners on individual sources
        don't sink the whole group.

        Each register is expected to expose a ``fields()`` iterator yielding
        objects with ``.inst_name`` (or ``.name``) — the convention shared by
        the generated pybind11 module and the systemrdl-compiler tree.
        """

        state_fields = _enumerate_fields(state)
        if not state_fields:
            raise ValueError("state register exposes no fields")
        enable_fields = _enumerate_fields(enable)
        test_fields = _enumerate_fields(test)
        mask_fields = _enumerate_fields(mask)
        haltmask_fields = _enumerate_fields(haltmask)
        haltenable_fields = _enumerate_fields(haltenable)

        sources: dict[str, InterruptSource] = {}
        for fname, sfield in state_fields.items():
            sources[fname] = InterruptSource(
                state_field=sfield,
                enable_field=enable_fields.get(fname),
                test_field=test_fields.get(fname),
                name=fname,
                mask_field=mask_fields.get(fname),
                haltmask_field=haltmask_fields.get(fname),
                haltenable_field=haltenable_fields.get(fname),
            )
        return cls(sources)

    # -- container surface --------------------------------------------------

    def __getattr__(self, name: str) -> InterruptSource:
        return cast("InterruptSource", _lookup_or_attr_error(self, name, "_sources", "interrupt group"))

    def __iter__(self) -> Iterator[InterruptSource]:
        return iter(self._sources.values())

    def __len__(self) -> int:
        return len(self._sources)

    def __contains__(self, name: Any) -> bool:
        return name in self._sources

    def __repr__(self) -> str:
        return f"InterruptGroup(sources={sorted(self._sources)!r})"

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._sources)

    # -- group queries ------------------------------------------------------

    def pending(self) -> frozenset[InterruptSource]:
        """Frozenset of sources whose state bit currently reads as 1."""

        return frozenset(s for s in self._sources.values() if s.is_pending())

    def enabled(self) -> frozenset[InterruptSource]:
        """Frozenset of sources whose enable bit currently reads as 1.

        Sources with no enable partner are reported as enabled (see
        :meth:`InterruptSource.is_enabled`).
        """

        return frozenset(s for s in self._sources.values() if s.is_enabled())

    def snapshot(self) -> dict[str, tuple[int, int]]:
        """Per-source ``(state, enable)`` pairs at this instant.

        Matches the §9.2 sketch — handy for golden-state assertions and the
        post-mortem dumps that follow a flaky test.
        """

        out: dict[str, tuple[int, int]] = {}
        for name, source in self._sources.items():
            state = int(source.state_field.read())
            enable = int(source.enable_field.read()) if source.enable_field is not None else 1
            out[name] = (state, enable)
        return out

    # -- group mutators -----------------------------------------------------

    def clear_all(self) -> None:
        """Clear every pending source. Safe to call when none are pending —
        write-1-to-clear is idempotent on a 0 bit."""

        for source in self._sources.values():
            source.clear()

    def disable_all(self) -> None:
        """Disable every source that exposes an enable partner."""

        for source in self._sources.values():
            if source.enable_field is not None:
                source.disable()

    def enable(self, set_: Iterable[str] | None = None) -> None:
        """Enable a subset of sources by name (or all, if ``set_`` is None).

        Unknown names raise :class:`KeyError`. Known sources that lack an
        enable partner are silently skipped — there is no enable bit to
        toggle, so requesting their enable is a no-op rather than an error.
        """

        targets = self._sources.keys() if set_ is None else set_
        for name in targets:
            source = self._sources.get(name)
            if source is None:
                raise KeyError(f"interrupt group has no source {name!r}; known: {sorted(self._sources)}")
            if source.enable_field is not None:
                source.enable()

    # -- §9.4 companion-partner group mutators ------------------------------
    #
    # Symmetric helpers that mirror ``enable()``/``disable_all()`` for the
    # mask / haltmask / haltenable partners. Calling them on a group whose
    # detection didn't surface the corresponding partner raises
    # :class:`NotSupportedError` — the per-source method that gets
    # delegated to does the actual raising. Per-source skip-when-absent
    # (matching the ``enable()`` semantics) is preserved by the
    # ``_for_each_source_with_partner`` helper below: it only raises when
    # *no* source in the group carries the partner.

    def mask(self, *sources: str) -> None:
        """Mask one or more sources (or all, if ``sources`` is empty)."""

        self._for_each_source_with_partner(sources, "mask_field", "mask", "INTR_MASK")

    def unmask(self, *sources: str) -> None:
        """Unmask one or more sources (or all, if ``sources`` is empty)."""

        self._for_each_source_with_partner(sources, "mask_field", "unmask", "INTR_MASK")

    def halt_mask(self, *sources: str) -> None:
        """Set the debug-halt mask bit for the named sources (or all)."""

        self._for_each_source_with_partner(sources, "haltmask_field", "halt_mask", "INTR_HALTMASK")

    def halt_unmask(self, *sources: str) -> None:
        """Clear the debug-halt mask bit for the named sources (or all)."""

        self._for_each_source_with_partner(sources, "haltmask_field", "halt_unmask", "INTR_HALTMASK")

    def halt_enable(self, *sources: str) -> None:
        """Set the debug-halt enable bit for the named sources (or all)."""

        self._for_each_source_with_partner(sources, "haltenable_field", "halt_enable", "INTR_HALTENABLE")

    def halt_disable(self, *sources: str) -> None:
        """Clear the debug-halt enable bit for the named sources (or all)."""

        self._for_each_source_with_partner(sources, "haltenable_field", "halt_disable", "INTR_HALTENABLE")

    def _for_each_source_with_partner(
        self,
        sources: tuple[str, ...],
        partner_attr: str,
        method: str,
        partner_label: str,
    ) -> None:
        """Dispatch a per-source method, surfacing missing-partner errors.

        - If ``sources`` is empty, walk every source in the group; if *none*
          of them carry ``partner_attr``, raise :class:`NotSupportedError`
          (the group has no partner at all). Otherwise call the method on
          each source that does, silently skipping those that lack one —
          matches the per-source skip semantic ``enable()`` uses.
        - If ``sources`` is non-empty, raise :class:`KeyError` for unknown
          names and propagate the per-source method's own
          :class:`NotSupportedError` for sources missing the partner.
        """

        if not sources:
            with_partner = [s for s in self._sources.values() if getattr(s, partner_attr) is not None]
            if not with_partner:
                raise NotSupportedError(f"interrupt group has no {partner_label} companion")
            for source in with_partner:
                getattr(source, method)()
            return

        for name in sources:
            source = self._sources.get(name)
            if source is None:
                raise KeyError(f"interrupt group has no source {name!r}; known: {sorted(self._sources)}")
            getattr(source, method)()


# ---------------------------------------------------------------------------
# manual() helper — accept a register-like or a mapping of fields.
# ---------------------------------------------------------------------------


def _enumerate_fields(obj: Any) -> dict[str, FieldLike]:
    """Return a ``{name: field}`` map for a register-like ``obj``.

    Tolerates four shapes so :meth:`InterruptGroup.manual` is forgiving:

    1. ``None`` → empty dict (the partner is absent).
    2. ``Mapping[str, FieldLike]`` → returned as a plain dict.
    3. An object with ``fields()`` → SystemRDL/pybind11 generated registers.
    4. An object whose attributes look like fields (``read``/``write``) —
       last-resort fallback. We exclude private names and methods.
    """

    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return {str(k): v for k, v in obj.items()}

    out: dict[str, Any] = {}
    fields_method = getattr(obj, "fields", None)
    if callable(fields_method):
        for field in cast(Iterable[Any], fields_method()):
            name = getattr(field, "inst_name", None) or getattr(field, "name", None) or ""
            if name:
                out[str(name)] = field
        if out:
            return out

    # Attribute scan — the slowest path, kept for hand-built test doubles.
    for name in dir(obj):
        if name.startswith("_"):
            continue
        attr = getattr(obj, name, None)
        if attr is obj:
            continue
        if hasattr(attr, "read") and hasattr(attr, "write") and not callable(attr):
            out[name] = attr
    return out


# ---------------------------------------------------------------------------
# InterruptTree — top-level ``soc.interrupts`` namespace.
# ---------------------------------------------------------------------------


class InterruptTree:
    """Aggregate of every :class:`InterruptGroup` on the SoC tree.

    Wired by Unit 1's ``register_post_create`` seam: as the generated
    ``Soc`` finishes constructing, post-create walks the tree, collects
    every :class:`InterruptGroup`, and instantiates one of these. The
    sketch §9.3 surface (``tree``, ``pending``, ``wait_any``) lives here.
    """

    def __init__(self, groups: Mapping[str, InterruptGroup]) -> None:
        self._groups: dict[str, InterruptGroup] = dict(groups)

    @property
    def groups(self) -> dict[str, InterruptGroup]:
        # Defensive copy — callers shouldn't mutate the underlying dict.
        return dict(self._groups)

    def __getattr__(self, name: str) -> InterruptGroup:
        return cast("InterruptGroup", _lookup_or_attr_error(self, name, "_groups", "interrupts"))

    def __iter__(self) -> Iterator[InterruptGroup]:
        return iter(self._groups.values())

    def __len__(self) -> int:
        return len(self._groups)

    def __repr__(self) -> str:
        return f"InterruptTree(groups={sorted(self._groups)!r})"

    def pending(self) -> frozenset[InterruptSource]:
        """Frozenset of pending sources across every group."""

        out: set[InterruptSource] = set()
        for group in self._groups.values():
            out.update(group.pending())
        return frozenset(out)

    def wait_any(self, timeout: float = 1.0, period: float = 0.001) -> InterruptSource:
        """Return the first source observed pending, or raise on timeout.

        The polling order is deterministic (group insertion order, then
        source insertion order) so flaky tests get reproducible failures.
        """

        deadline = time.monotonic() + timeout
        while True:
            for group in self._groups.values():
                for source in group:
                    if source.is_pending():
                        return source
            if time.monotonic() >= deadline:
                raise WaitTimeoutError("soc.interrupts", "any pending", None)
            time.sleep(period)

    def tree(self) -> str:
        """Pretty-print every group + source with its current status.

        Returns the rendered string (and prints it) so callers can capture
        the dump for golden-file checks. Reading every state/enable bit
        triggers real bus traffic — the call is intentionally not free,
        same as ``snapshot()``.
        """

        lines: list[str] = ["interrupts:"]
        for gname, group in self._groups.items():
            lines.append(f"  {gname}:")
            for sname, (state, enable) in group.snapshot().items():
                marker = "*" if state and enable else " "
                lines.append(f"    {marker} {sname:<20} state={state} enable={enable}")
        text = "\n".join(lines)
        print(text)
        return text


# ---------------------------------------------------------------------------
# Hook seams — Unit 1 owns the formal seams; we expose tiny shims so the
# real ``register_post_create`` / ``register_register_enhancement`` from
# the scaffolding land in this same module without a rewrite. Every hook
# is idempotent and has no effect until the seam plugs them in.
#
# Naming: the public hook entry points are named ``interrupts_*`` so the
# closer's smoke check (``'interrupts' in fn.__qualname__``) discriminates
# them from sibling hooks registered against the same seams.
# ---------------------------------------------------------------------------

_GROUP_REGISTRY: dict[str, InterruptGroup] = {}

# Stash for class-level metadata captured by the register-enhancement hook.
# Today Unit 23 doesn't actually populate the registry with interrupt
# metadata, but the hook is forward-compatible: when (cls, metadata) carries
# an ``"interrupts"`` or ``"interrupt_group"`` key, the post-create walk
# uses it to wire instance-level groups. Keyed by class identity so a
# repeated import doesn't double-stash.
_CLASS_INTERRUPT_METADATA: dict[type, dict[str, Any]] = {}


def register_interrupt_group(path: str, group: InterruptGroup) -> None:
    """Record a group under its dotted path so :class:`InterruptTree`
    can collect it later. Used by both auto-detection (Unit 23) and
    manual wiring."""

    _GROUP_REGISTRY[path] = group


def _build_group_from_register(
    state_reg: Any,
    enable_reg: Any | None = None,
    test_reg: Any | None = None,
    *,
    mask_reg: Any | None = None,
    haltmask_reg: Any | None = None,
    haltenable_reg: Any | None = None,
) -> InterruptGroup:
    """Build an :class:`InterruptGroup` from instance-level register objects.

    Shared helper used by both the legacy register-enhancement entry point
    (kept under :func:`register_register_enhancement_hook` for callers that
    drive the wiring manually) and the post-create walk. Delegates to
    :meth:`InterruptGroup.manual`, which is the canonical instance-level
    constructor.
    """

    return InterruptGroup.manual(
        state=state_reg,
        enable=enable_reg,
        test=test_reg,
        mask=mask_reg,
        haltmask=haltmask_reg,
        haltenable=haltenable_reg,
    )


def _attach_group_as_interrupts(target: Any, group: InterruptGroup) -> None:
    """Best-effort ``target.interrupts = group`` assignment.

    Tolerates slotted/frozen targets — both regfile parents (in
    :func:`register_register_enhancement_hook`) and the resolved parent
    objects in :func:`_build_groups_from_detected` need this same forgiving
    semantic, so it lives as a shared helper. Aggregation under
    :class:`InterruptTree` is unaffected by the assignment failing.
    """

    if target is None:
        return
    try:
        target.interrupts = group
    except (AttributeError, TypeError):
        pass


def register_register_enhancement_hook(register: Any, info: Any) -> InterruptGroup | None:
    """Legacy instance-level entry point for manually wiring a detected trio.

    ``info`` is a per-register descriptor (typically produced by callers
    that want to plug detection results in by hand): it must expose
    ``is_interrupt_state`` and may carry ``enable_register`` /
    ``test_register`` attributes pointing at the partner registers. The
    hook builds an :class:`InterruptGroup`, attaches it to the parent
    regfile under ``.interrupts``, and registers it for top-level
    ``soc.interrupts`` aggregation.

    The actual registry wiring is in :func:`interrupts_register_enhancement`
    (which receives ``(cls, metadata)`` per the Unit 1 contract); this
    helper survives because tests and manual callers still want a one-shot
    instance-level entry point.
    """

    if info is None or not getattr(info, "is_interrupt_state", False):
        return None
    enable = getattr(info, "enable_register", None)
    test = getattr(info, "test_register", None)
    mask = getattr(info, "mask_register", None)
    haltmask = getattr(info, "haltmask_register", None)
    haltenable = getattr(info, "haltenable_register", None)
    group = _build_group_from_register(
        register,
        enable,
        test,
        mask_reg=mask,
        haltmask_reg=haltmask,
        haltenable_reg=haltenable,
    )

    parent = getattr(register, "parent", None) or getattr(register, "_parent", None)
    _attach_group_as_interrupts(parent, group)

    # Stash under the parent's dotted path so :class:`InterruptTree` picks
    # the group up — strip the trailing register name so we land on the
    # regfile (``soc.uart``) rather than the register itself.
    path = getattr(register, "path", None) or getattr(info, "path", None) or ""
    if path:
        prefix = path.rsplit(".", 1)[0] if "." in path else path
        register_interrupt_group(prefix, group)
    return group


def interrupts_register_enhancement(cls: type, metadata: dict[str, Any]) -> None:
    """Register-enhancement hook fired by :data:`_registry` for every class.

    The :data:`_registry.register_register_enhancement` seam invokes
    enhancement callables with ``(cls, metadata)`` at *class* construction
    time — there are no register instances yet, so the heavy lifting
    (path resolution, parent attachment) happens later in
    :func:`interrupts_post_create`.

    What this hook does today: stash any interrupt metadata associated with
    ``cls`` so the post-create walk can pick it up. Unit 23's exporter
    plugin currently writes a sibling ``interrupts_detected.py`` instead of
    routing detection through the registry, so in practice this stash is
    rarely populated — but keeping the seam wired keeps the door open for
    metadata-driven enhancement without another runtime change.
    """

    payload = metadata.get("interrupts") or metadata.get("interrupt_group")
    if payload is None:
        return
    if not isinstance(payload, Mapping):
        logger.debug("ignoring non-mapping interrupt metadata on %r: %r", cls, payload)
        return
    _CLASS_INTERRUPT_METADATA[cls] = dict(payload)


def _resolve_path(soc: Any, path: str) -> Any | None:
    """Resolve a dotted SystemRDL path against ``soc``.

    Path strings emitted by Unit 23's ``interrupts_detected.py`` look like
    ``"top.regfile.REG_NAME"``; the leading ``top`` segment is the SoC's
    own instance name and the rest are attribute lookups on the generated
    tree. We try every possible prefix-strip so the resolution survives
    when the top-level name doesn't match the runtime ``soc`` handle.
    """

    if not path:
        return None
    parts = path.split(".")
    for skip in range(len(parts)):
        cur: Any = soc
        for component in parts[skip:]:
            cur = getattr(cur, component, None)
            if cur is None:
                break
        else:
            # All components resolved; reject the no-skip-no-walk identity.
            if cur is not soc:
                return cur
    return None


def _load_detected_groups(soc: Any) -> list[Mapping[str, Any]]:
    """Load Unit 23's ``interrupts_detected.py`` from the SoC's package.

    Returns the ``interrupt_groups`` list (or an empty list if the module
    isn't present, can't be imported, or doesn't expose the expected
    attribute). Never raises — the post-create hook needs to attach an
    empty :class:`InterruptTree` even when detection metadata is missing,
    so callers should treat any failure as "no groups detected".
    """

    module_name = getattr(soc, "_module_name", None) or type(soc).__module__
    if not module_name or module_name == "__main__":
        return []
    try:
        detected = importlib.import_module(f"{module_name}.interrupts_detected")
    except ImportError:
        return []
    except Exception:
        # A broken auto-generated file shouldn't take down post-create —
        # log and fall back to the empty tree.
        logger.exception("could not import detection metadata for %r", soc)
        return []
    payload = getattr(detected, "interrupt_groups", None)
    if not isinstance(payload, list):
        return []
    out: list[Mapping[str, Any]] = []
    for entry in payload:
        if isinstance(entry, Mapping):
            out.append(entry)
    return out


def _build_groups_from_detected(
    soc: Any,
    detected: Iterable[Mapping[str, Any]],
) -> dict[str, InterruptGroup]:
    """Walk Unit 23's detected-group payload and build :class:`InterruptGroup`s.

    Each detection entry carries the full SystemRDL paths of the state /
    enable / test registers; we resolve those against ``soc`` and feed the
    resulting register objects into :meth:`InterruptGroup.manual`. Entries
    whose state register can't be resolved are silently skipped — partial
    failures shouldn't crash the post-create hook.
    """

    out: dict[str, InterruptGroup] = {}
    for entry in detected:
        state_path = entry.get("state_reg")
        if not state_path:
            continue
        state_reg = _resolve_path(soc, state_path)
        if state_reg is None:
            logger.debug("interrupts: could not resolve state register %r", state_path)
            continue
        enable_reg = _resolve_path(soc, entry.get("enable_reg") or "")
        test_reg = _resolve_path(soc, entry.get("test_reg") or "")
        mask_reg = _resolve_path(soc, entry.get("mask_reg") or "")
        haltmask_reg = _resolve_path(soc, entry.get("haltmask_reg") or "")
        haltenable_reg = _resolve_path(soc, entry.get("haltenable_reg") or "")
        try:
            group = _build_group_from_register(
                state_reg,
                enable_reg,
                test_reg,
                mask_reg=mask_reg,
                haltmask_reg=haltmask_reg,
                haltenable_reg=haltenable_reg,
            )
        except (ValueError, TypeError):
            logger.exception("interrupts: failed to build group for %r", state_path)
            continue
        # Unit 23 emits ``path`` for the parent regfile; we fall back to
        # stripping the state register's tail. The leaf becomes the key so
        # ``soc.interrupts.uart`` is reachable in the common case.
        parent_path = entry.get("path") or state_path.rsplit(".", 1)[0]
        out[parent_path.rsplit(".", 1)[-1]] = group
        # Mirror the group on the regfile so ``soc.uart.interrupts`` works
        # alongside ``soc.interrupts.uart``.
        _attach_group_as_interrupts(_resolve_path(soc, parent_path), group)
    return out


def interrupts_post_create(soc: Any) -> InterruptTree:
    """Post-create hook fired by :data:`_registry` to attach ``soc.interrupts``.

    Three sources of groups feed the tree, in order:

    1. Manual ``register_interrupt_group(path, group)`` calls (from tests
       or hand-rolled wiring).
    2. Unit 23's ``interrupts_detected.py`` if present alongside the SoC's
       generated module.
    3. Per-class metadata stashed by :func:`interrupts_register_enhancement`
       (forward-compat — Unit 23 doesn't currently feed this path).

    The hook never raises: if no detection metadata is available the SoC
    still gets a permissive empty :class:`InterruptTree` so attribute
    accesses on ``soc.interrupts`` produce a deterministic
    :class:`AttributeError` pointing at the empty group set.
    """

    groups: dict[str, InterruptGroup] = dict(_GROUP_REGISTRY)
    detected = _load_detected_groups(soc)
    if detected:
        groups.update(_build_groups_from_detected(soc, detected))
    tree = InterruptTree(groups)
    try:
        soc.interrupts = tree
    except (AttributeError, TypeError):
        # Some generated trees use slots — callers can still pull the
        # InterruptTree from the return value.
        pass
    return tree


# Backwards-compatible alias: older docs/tests refer to the post-create
# hook by its previous name. Pointing the alias at the new function keeps
# manual callers working without forcing a rename in every external doc.
register_post_create_hook = interrupts_post_create


# ---------------------------------------------------------------------------
# Registry wiring (sibling-dep: Unit 1's runtime/_registry).
#
# Mirrors the pattern established in ``runtime/trace.py`` (lines 198-214):
# a ``try/except ImportError`` shields this module from circular-import
# failures during early test bring-up, and the post-import branch wires
# both seams. ``_registry`` deduplicates by callable identity, so a
# re-import of this module is a no-op.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - depends on Unit 1 landing order
    from . import _registry  # type: ignore[attr-defined]
except ImportError:
    _registry = None  # type: ignore[assignment]

if _registry is not None:
    if hasattr(_registry, "register_post_create"):
        # ``interrupts_post_create`` returns ``InterruptTree`` (tests rely on
        # the return value); the registry contract types the slot as
        # ``Callable[[Any], None]``. Same suppression style used in
        # ``runtime/specialized.py``.
        _registry.register_post_create(interrupts_post_create)  # type: ignore[arg-type]
    if hasattr(_registry, "register_register_enhancement"):
        _registry.register_register_enhancement(interrupts_register_enhancement)
