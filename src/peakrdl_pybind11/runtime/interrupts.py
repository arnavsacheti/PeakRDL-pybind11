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
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Protocol, runtime_checkable

from .errors import WaitTimeoutError

__all__ = [
    "FieldLike",
    "InterruptGroup",
    "InterruptSource",
    "InterruptTree",
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


def _lookup_or_attr_error(obj: object, attr_name: str, mapping_name: str, kind: str) -> object:
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
    ) -> None:
        self._state_field = state_field
        self._enable_field = enable_field
        self._test_field = test_field
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
        """Async variant of :meth:`wait`. Sleeps via :func:`asyncio.sleep`."""

        deadline = time.monotonic() + timeout
        last_seen = self.is_pending()
        while not last_seen:
            if time.monotonic() >= deadline:
                raise WaitTimeoutError(f"interrupts.{self._name}", True, last_seen)
            await asyncio.sleep(period)
            last_seen = self.is_pending()

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
        state: object,
        enable: object | None = None,
        test: object | None = None,
    ) -> InterruptGroup:
        """Build a group from raw register objects when auto-detect failed.

        Matches sources by field name across the trio. ``state`` is required
        (it's where the pending bits live); ``enable`` and ``test`` are
        optional and matched by best-effort name lookup so missing partners
        on individual sources don't sink the whole group.

        Each register is expected to expose a ``fields()`` iterator yielding
        objects with ``.inst_name`` (or ``.name``) — the convention shared by
        the generated pybind11 module and the systemrdl-compiler tree.
        """

        state_fields = _enumerate_fields(state)
        if not state_fields:
            raise ValueError("state register exposes no fields")
        enable_fields = _enumerate_fields(enable)
        test_fields = _enumerate_fields(test)

        sources: dict[str, InterruptSource] = {}
        for fname, sfield in state_fields.items():
            sources[fname] = InterruptSource(
                state_field=sfield,
                enable_field=enable_fields.get(fname),
                test_field=test_fields.get(fname),
                name=fname,
            )
        return cls(sources)

    # -- container surface --------------------------------------------------

    def __getattr__(self, name: str) -> InterruptSource:
        return _lookup_or_attr_error(self, name, "_sources", "interrupt group")

    def __iter__(self) -> Iterator[InterruptSource]:
        return iter(self._sources.values())

    def __len__(self) -> int:
        return len(self._sources)

    def __contains__(self, name: object) -> bool:
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


# ---------------------------------------------------------------------------
# manual() helper — accept a register-like or a mapping of fields.
# ---------------------------------------------------------------------------


def _enumerate_fields(obj: object) -> dict[str, FieldLike]:
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

    out: dict[str, FieldLike] = {}
    fields_method = getattr(obj, "fields", None)
    if callable(fields_method):
        for field in fields_method():
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
        return _lookup_or_attr_error(self, name, "_groups", "interrupts")

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
# ---------------------------------------------------------------------------

_GROUP_REGISTRY: dict[str, InterruptGroup] = {}


def register_interrupt_group(path: str, group: InterruptGroup) -> None:
    """Record a group under its dotted path so :class:`InterruptTree`
    can collect it later. Used by both auto-detection (Unit 23) and
    manual wiring."""

    _GROUP_REGISTRY[path] = group


def register_post_create_hook(soc: object) -> InterruptTree:
    """``register_post_create`` callback — attach ``soc.interrupts``.

    Walks the registry populated by Unit 23's auto-detection (and any
    manual ``register_interrupt_group`` calls), instantiates an
    :class:`InterruptTree`, and assigns it to ``soc.interrupts``. Returns
    the tree for tests/inspection.
    """

    tree = InterruptTree(dict(_GROUP_REGISTRY))
    try:
        soc.interrupts = tree
    except (AttributeError, TypeError):
        # Some generated trees use slots — callers can still pull the
        # InterruptTree from the return value.
        pass
    return tree


def register_register_enhancement_hook(register: object, info: object) -> InterruptGroup | None:
    """``register_register_enhancement`` callback — attach
    ``regfile.interrupts`` when this register is the ``state`` half of a
    detected trio.

    ``info`` is the detection metadata produced by Unit 23's exporter
    plugin: it carries the partner registers (enable/test) on attributes
    of the same name. The hook builds an :class:`InterruptGroup` from the
    trio, attaches it to the parent regfile under ``.interrupts``, and
    registers it for top-level ``soc.interrupts`` aggregation.
    """

    if info is None or not getattr(info, "is_interrupt_state", False):
        return None
    enable = getattr(info, "enable_register", None)
    test = getattr(info, "test_register", None)
    group = InterruptGroup.manual(state=register, enable=enable, test=test)

    parent = getattr(register, "parent", None) or getattr(register, "_parent", None)
    if parent is not None:
        try:
            parent.interrupts = group
        except (AttributeError, TypeError):
            pass

    path = getattr(register, "path", None) or getattr(info, "path", None) or ""
    if path:
        # Strip the trailing ``.intr_state`` (or whatever the state register
        # is named) so the group is registered under the parent block —
        # ``soc.uart.interrupts`` rather than ``soc.uart.intr_state``.
        prefix = path.rsplit(".", 1)[0] if "." in path else path
        register_interrupt_group(prefix, group)
    return group
