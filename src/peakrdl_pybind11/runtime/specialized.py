"""Helpers for specialized SystemRDL register kinds.

Implements §12 of ``docs/IDEAL_API_SKETCH.md``: counter wrappers, singlepulse
``pulse()``, reset semantics (``reset_value`` / ``is_at_reset()`` /
``reset_all()``), and lock-key sequences.

Each helper is a tiny class or free-function that wraps an existing
register / field node. Wrappers consume duck-typed objects -- anything with
the standard ``read()`` / ``write()`` surface plus an ``info`` namespace --
so they are unit-testable without generated bindings.

Wiring into generated bindings happens via :func:`attach_specialized` (called
from the seam :data:`peakrdl_pybind11.runtime._registry.register_register_enhancement`)
and :func:`attach_post_create` (the SoC-level seam). Both use a graceful
import: when Unit 1's ``_registry`` module is not available the helpers are
still importable in isolation.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterable, Sequence
from typing import Any

# Graceful sibling-unit imports. ``runtime.errors`` and ``runtime.info`` may
# not be merged into ``exp/api_overhaul`` yet; in that case we fall back to a
# local definition that satisfies the same contract. Once those units land
# the local fallback is shadowed by the canonical implementation.
try:  # pragma: no cover - import shim
    from peakrdl_pybind11.runtime.errors import NotSupportedError
except ImportError:  # pragma: no cover - executed only when Unit 2 is absent

    class NotSupportedError(Exception):  # type: ignore[no-redef]
        """Raised when a feature is not implemented by the active master/transport.

        Local fallback used until ``runtime.errors`` (Unit 2) is merged.
        """

        def __init__(self, message: str) -> None:
            self.message = message
            super().__init__(message)


__all__ = [
    "Counter",
    "LockController",
    "NotSupportedError",
    "ResetMixin",
    "attach_counter",
    "attach_lock_controller",
    "attach_post_create",
    "attach_pulse",
    "attach_regfile_reset_all",
    "attach_reset_helpers",
    "attach_soc_reset_all",
    "attach_specialized",
    "is_at_reset",
    "pulse",
    "register_post_create",
    "register_register_enhancement",
    "reset_all_regfile",
    "reset_all_soc",
    "reset_value",
]


# ---------------------------------------------------------------------------
# Helpers shared across wrappers
# ---------------------------------------------------------------------------


class _EmptyTags:
    """Permissive namespace returning ``None`` for any UDP attribute access."""

    def __getattr__(self, name: str) -> object | None:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return None


class _EmptyInfo:
    """Stand-in for an ``Info`` instance when the host node has none."""

    name: str = ""
    path: str = ""
    address: int = 0
    reset: int | None = None
    access: str | None = None
    fields: dict[str, object] = {}  # noqa: RUF012 - shared empty mapping is intentional

    def __init__(self) -> None:
        self.tags = _EmptyTags()


def _info_of(node: object) -> object:
    """Return ``node.info`` if present, else an empty namespace.

    Falling back to an empty namespace makes the wrappers work on stub /
    mock objects in tests without forcing every test to construct an
    ``Info`` explicitly.
    """
    return getattr(node, "info", _EmptyInfo())


def _path_of(node: object) -> str:
    """Best-effort dotted path for a node, used in error messages."""
    info = _info_of(node)
    path = getattr(info, "path", "") or getattr(info, "name", "")
    return path or type(node).__name__


# ---------------------------------------------------------------------------
# Counter
# ---------------------------------------------------------------------------


class Counter:
    """Wrap a counter register/field with the §12.1 counter API.

    The wrapper is constructed with the host node (a register or field) and
    a small spec that captures the RDL semantics relevant to software:

    * ``can_increment`` / ``can_decrement`` — whether the counter exposes a
      software increment / decrement path.
    * ``saturate`` / ``threshold`` — whether ``incrsaturate`` /
      ``incrthreshold`` are declared.
    * ``incrvalue_field`` / ``decrvalue_field`` — the names of the fields to
      write when increment / decrement is supported. ``None`` means the
      hardware exposes a write-1-to-increment field (``host`` itself).
    * ``threshold_value`` — the static threshold from the RDL
      ``incrthreshold`` property, if any.
    * ``saturate_value`` — the static saturation ceiling from
      ``incrsaturate``, if any.

    The host node must expose ``read()`` (returning an int-compatible value)
    and ``write(value)``. ``reset()`` uses the host's ``write(0)`` path
    unless ``reset_method`` is supplied.

    Methods that require an absent capability raise :class:`NotSupportedError`
    rather than silently no-op'ing -- per the sketch §12.1 contract that "a
    typo in user code is an AttributeError at the call site". Generated code
    can hide these methods entirely; this wrapper exists so the *Python*
    runtime can still surface a clean error when the wrapper is constructed
    by hand.
    """

    __slots__ = (
        "_can_decrement",
        "_can_increment",
        "_decrvalue_field",
        "_host",
        "_incrvalue_field",
        "_reset_method",
        "_saturate",
        "_saturate_value",
        "_threshold",
        "_threshold_value",
    )

    def __init__(
        self,
        host: object,
        *,
        can_increment: bool = False,
        can_decrement: bool = False,
        saturate: bool = False,
        threshold: bool = False,
        incrvalue_field: str | None = None,
        decrvalue_field: str | None = None,
        threshold_value: int | None = None,
        saturate_value: int | None = None,
        reset_method: Callable[[], None] | None = None,
    ) -> None:
        self._host = host
        self._can_increment = can_increment
        self._can_decrement = can_decrement
        self._saturate = saturate
        self._threshold = threshold
        self._incrvalue_field = incrvalue_field
        self._decrvalue_field = decrvalue_field
        self._threshold_value = threshold_value
        self._saturate_value = saturate_value
        self._reset_method = reset_method

    # ----- introspection ---------------------------------------------------

    @property
    def host(self) -> object:
        """The wrapped register/field node."""
        return self._host

    def __repr__(self) -> str:
        caps: list[str] = []
        if self._can_increment:
            caps.append("incr")
        if self._can_decrement:
            caps.append("decr")
        if self._saturate:
            caps.append("sat")
        if self._threshold:
            caps.append("thr")
        cap_str = "|".join(caps) or "ro"
        return f"<Counter {_path_of(self._host)} caps={cap_str}>"

    # ----- ops --------------------------------------------------------------

    def value(self) -> int:
        """Return the current count (one bus read)."""
        return int(self._host.read())

    def reset(self) -> None:
        """Clear the counter.

        Uses ``reset_method`` when supplied (e.g. for ``wclr`` semantics that
        require a specific value/mask). Otherwise writes ``0`` to the host
        node, which corresponds to plain "reset value" semantics.
        """
        if self._reset_method is not None:
            self._reset_method()
            return
        self._host.write(0)

    def threshold(self) -> int:
        """Return the configured ``incrthreshold``.

        Raises :class:`NotSupportedError` when no threshold is declared.
        """
        if not self._threshold:
            raise NotSupportedError(f"{_path_of(self._host)}: counter has no incrthreshold")
        if self._threshold_value is None:
            raise NotSupportedError(
                f"{_path_of(self._host)}: incrthreshold is dynamic; read it via the field directly"
            )
        return int(self._threshold_value)

    def is_saturated(self) -> bool:
        """Return ``True`` when the counter has hit its saturation ceiling.

        Raises :class:`NotSupportedError` when ``incrsaturate`` is not set.
        """
        if not self._saturate:
            raise NotSupportedError(f"{_path_of(self._host)}: counter has no incrsaturate")
        if self._saturate_value is None:
            # No static ceiling — best-effort: compare against the largest
            # representable value implied by regwidth, if known.
            regwidth = getattr(_info_of(self._host), "regwidth", None)
            if not isinstance(regwidth, int) or regwidth <= 0:
                raise NotSupportedError(f"{_path_of(self._host)}: incrsaturate has no static ceiling")
            ceiling = (1 << regwidth) - 1
        else:
            ceiling = int(self._saturate_value)
        return int(self._host.read()) >= ceiling

    def increment(self, by: int = 1) -> None:
        """Increment the counter by ``by`` (writes the ``incrvalue`` field).

        Raises :class:`NotSupportedError` when software increment is not
        supported by the underlying RDL.
        """
        if not self._can_increment:
            raise NotSupportedError(f"{_path_of(self._host)}: counter does not support software increment")
        if by < 0:
            raise ValueError("Counter.increment(by=...) requires a non-negative amount")
        self._write_step(self._incrvalue_field, by)

    def decrement(self, by: int = 1) -> None:
        """Decrement the counter by ``by``.

        Raises :class:`NotSupportedError` when software decrement is not
        supported by the underlying RDL.
        """
        if not self._can_decrement:
            raise NotSupportedError(f"{_path_of(self._host)}: counter does not support software decrement")
        if by < 0:
            raise ValueError("Counter.decrement(by=...) requires a non-negative amount")
        self._write_step(self._decrvalue_field, by)

    def _write_step(self, field_name: str | None, amount: int) -> None:
        """Write ``amount`` either to a specific field or to the host directly.

        When ``field_name`` is ``None`` the host node itself is the target
        (e.g. a single-bit increment-strobe field). When the host exposes a
        ``modify(**kwargs)`` shim the named-field path is used; otherwise we
        fall back to attribute-style child access (``host.<field>.write``).
        """
        if field_name is None:
            self._host.write(amount)
            return

        modify = getattr(self._host, "modify", None)
        if callable(modify):
            modify(**{field_name: amount})
            return

        target = getattr(self._host, field_name, None)
        if target is not None and hasattr(target, "write"):
            target.write(amount)
            return

        raise NotSupportedError(f"{_path_of(self._host)}: cannot reach field {field_name!r} for counter step")


# ---------------------------------------------------------------------------
# Singlepulse
# ---------------------------------------------------------------------------


def pulse(field: object) -> None:
    """Issue the §12.2 singlepulse write (``write(1)``) to ``field``.

    Free-function form used by tests and ad-hoc callers. Generated field
    classes get a method-form via :func:`attach_pulse`.
    """
    field.write(1)


def attach_pulse(field_class: type) -> bool:
    """Attach a ``pulse()`` method to a singlepulse field class.

    Returns ``True`` if the method was attached, ``False`` if the class
    already has one or the field is not declared singlepulse. Idempotent.

    The function looks at the class' ``info.tags.singlepulse`` UDP (or the
    explicit ``singlepulse = True`` attribute) so callers can either lean on
    Unit 4's metadata extraction or set the marker by hand in tests.
    """
    if getattr(field_class, "pulse", None) is not None:
        return False
    if not _is_singlepulse(field_class):
        return False

    def pulse_method(self: object) -> None:
        # ``write(1)`` collapses to one bus write; hardware self-clears.
        self.write(1)

    field_class.pulse = pulse_method  # type: ignore[attr-defined]
    return True


def _is_singlepulse(node_class_or_instance: object) -> bool:
    """Return ``True`` if a class/instance is annotated as singlepulse."""
    direct = getattr(node_class_or_instance, "singlepulse", None)
    if direct:
        return True
    info = getattr(node_class_or_instance, "info", None)
    if info is not None:
        tags = getattr(info, "tags", None)
        if tags is not None and getattr(tags, "singlepulse", None):
            return True
    return False


# ---------------------------------------------------------------------------
# Reset semantics
# ---------------------------------------------------------------------------


def reset_value(reg: object) -> int:
    """Return the static reset value declared for ``reg`` (no bus traffic)."""
    info = _info_of(reg)
    val = getattr(info, "reset", None)
    if val is None:
        # Allow an explicit attribute on the wrapper for tests/stubs.
        val = getattr(reg, "_reset_value", None)
    return int(val) if val is not None else 0


def is_at_reset(reg: object) -> bool:
    """Return ``True`` when ``reg.read() == reg.reset_value`` (one bus read)."""
    expected = reset_value(reg)
    actual = int(reg.read())
    return actual == expected


def reset_all_regfile(regfile: object, *, rw_only: bool = True) -> int:
    """Restore every register under ``regfile`` to its reset value.

    Walks every child register/regfile (including arrays) and writes
    ``reg.reset_value`` to it. Read-only registers are skipped when
    ``rw_only=True`` (the default).

    Returns the number of registers actually written. The walk is
    breadth-first via :func:`_walk_registers`.
    """
    written = 0
    for reg in _walk_registers(regfile):
        if rw_only and _is_read_only(reg):
            continue
        try:
            reg.write(reset_value(reg))
        except Exception:
            # Best-effort: a child without ``write`` (e.g. a pure status reg)
            # is silently skipped; the rw_only path catches most cases.
            continue
        written += 1
    return written


def reset_all_soc(soc: object, *, rw_only: bool = True) -> int:
    """Reset every register under the whole SoC tree.

    Emits a :class:`UserWarning` if any register being restored has an
    ``rclr`` field combined with RW access (per §12.3 "ambiguous reset"
    safety check).
    """
    flagged = list(_iter_rclr_rw_conflicts(soc))
    if flagged:
        warnings.warn(
            "soc.reset_all() touching registers whose RW fields also clear on read: " + ", ".join(flagged),
            UserWarning,
            stacklevel=2,
        )
    return reset_all_regfile(soc, rw_only=rw_only)


def attach_reset_helpers(reg_class: type) -> None:
    """Attach ``reset_value`` (property) and ``is_at_reset()`` to a register class.

    Idempotent: re-attaching is a no-op when the methods already exist.
    """
    if "reset_value" not in reg_class.__dict__:

        def _reset_value_getter(self: object) -> int:
            return reset_value(self)

        reg_class.reset_value = property(_reset_value_getter)  # type: ignore[attr-defined]

    if "is_at_reset" not in reg_class.__dict__:

        def _is_at_reset_method(self: object) -> bool:
            return is_at_reset(self)

        reg_class.is_at_reset = _is_at_reset_method  # type: ignore[attr-defined]


def attach_regfile_reset_all(regfile_class: type) -> None:
    """Attach ``reset_all(rw_only=True)`` to a regfile class."""
    if "reset_all" in regfile_class.__dict__:
        return

    def _reset_all_method(self: object, *, rw_only: bool = True) -> int:
        return reset_all_regfile(self, rw_only=rw_only)

    regfile_class.reset_all = _reset_all_method  # type: ignore[attr-defined]


def attach_soc_reset_all(soc_class: type) -> None:
    """Attach a tree-walking ``reset_all()`` to a SoC top-level class."""
    if "reset_all" in soc_class.__dict__:
        return

    def _reset_all_method(self: object, *, rw_only: bool = True) -> int:
        return reset_all_soc(self, rw_only=rw_only)

    soc_class.reset_all = _reset_all_method  # type: ignore[attr-defined]


def _is_read_only(reg: object) -> bool:
    """Return ``True`` when the register is read-only per its access mode."""
    info = _info_of(reg)
    access = getattr(info, "access", None)
    if isinstance(access, str):
        return access.lower() == "r"
    return False


def _walk_registers(node: object) -> Iterable[object]:
    """Yield every register descendant of ``node``.

    A node is treated as a register when it exposes both ``read`` and
    ``write`` callables. Otherwise ``node._children()`` (or, failing that,
    iteration over ``getattr(node, "_registers", [])`` and friends) is used
    to descend further. The function tolerates a wide range of generated
    shapes -- the typical generated module exposes children as attributes
    or as a ``children`` iterable.
    """
    seen: set[int] = set()
    stack: list[object] = [node]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        if _looks_like_register(current):
            yield current
            continue

        children = _children_of(current)
        # Reverse so the natural iteration order is preserved when popping.
        stack.extend(reversed(list(children)))


def _looks_like_register(node: object) -> bool:
    """Return ``True`` if ``node`` quacks like a register (has read & write)."""
    if not callable(getattr(node, "read", None)):
        return False
    if not callable(getattr(node, "write", None)):
        return False
    # Excluding fields: a field also has read/write but is a child of a
    # register; if the node looks like a field (has ``lsb``/``width``),
    # the parent register has already been or will be seen via the walk.
    if hasattr(node, "lsb") and hasattr(node, "width"):
        return False
    return True


def _children_of(node: object) -> Iterable[object]:
    """Best-effort iteration of ``node``'s descendants.

    Looks for, in order:

    1. ``node._children()`` (callable returning an iterable).
    2. ``node.children`` (attribute that is iterable).
    3. ``node._registers``, ``node._regfiles`` (lists set by generated code).
    4. Any attribute on the instance whose value also looks register-shaped.
    """
    children_fn = getattr(node, "_children", None)
    if callable(children_fn):
        try:
            yield from children_fn()
            return
        except TypeError:
            pass

    children_attr = getattr(node, "children", None)
    if children_attr is not None and not isinstance(children_attr, type):
        try:
            yield from list(children_attr)
            return
        except TypeError:
            pass

    yielded_any = False
    for attr in ("_registers", "_regfiles", "_addrmaps"):
        coll = getattr(node, attr, None)
        if coll is None:
            continue
        try:
            for child in coll:
                yielded_any = True
                yield child
        except TypeError:
            continue
    if yielded_any:
        return

    # Final fallback: scan ``__dict__`` for register-shaped attributes. Used
    # by hand-rolled stubs and by generated top-level SoC objects whose
    # children are bound directly as public attributes.
    instance_dict = getattr(node, "__dict__", None) or {}
    for name, value in instance_dict.items():
        if name.startswith("_"):
            continue
        if value is node or value is None:
            continue
        if _looks_like_register(value) or _looks_like_container(value):
            yield value


def _looks_like_container(node: object) -> bool:
    """Return ``True`` if ``node`` looks like a regfile/addrmap container."""
    if hasattr(node, "_children") or hasattr(node, "children"):
        return True
    if hasattr(node, "_registers") or hasattr(node, "_regfiles"):
        return True
    return False


def _iter_rclr_rw_conflicts(soc: object) -> Iterable[str]:
    """Yield register paths whose RW fields are also ``rclr``."""
    for reg in _walk_registers(soc):
        info = _info_of(reg)
        access = getattr(info, "access", None)
        if isinstance(access, str) and access.lower() != "rw":
            continue
        fields_map = getattr(info, "fields", {}) or {}
        for field_info in fields_map.values():
            on_read = getattr(field_info, "on_read", None)
            f_access = getattr(field_info, "access", None)
            f_is_rw = isinstance(f_access, str) and f_access.lower() == "rw"
            if on_read == "rclr" and f_is_rw:
                yield _path_of(reg)
                break


class ResetMixin:
    """Mixin that exposes ``reset_value`` / ``is_at_reset()`` as methods.

    Generated bindings can opt in by inheriting from this mixin instead of
    using :func:`attach_reset_helpers`. Both paths produce the same surface;
    the mixin form is preferred when the generated class is authored by
    template (one less call site).
    """

    @property
    def reset_value(self) -> int:
        return reset_value(self)

    def is_at_reset(self) -> bool:
        return is_at_reset(self)


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


class LockController:
    """Wrap a lock register with §12.4 ``lock`` / ``is_locked`` /
    ``unlock_sequence`` accessors.

    Construction parameters:

    * ``host`` — the register node carrying the lock-bits.
    * ``key_field`` — name of the field that receives the LCKK key
      (typically ``"LCKK"`` per the STM32 GPIO LCKR pattern).
    * ``key_sequence`` — the sequence of values written to ``key_field``
      after the lock-bits are programmed. The canonical STM32 sequence is
      ``(1, 0, 1)`` -- write key, write 0, write key again, then read back.
    * ``unlock_sequence_fn`` — optional callable invoked by
      :meth:`unlock_sequence`. When ``None`` the method raises
      :class:`NotSupportedError` per the sketch -- many parts deliberately
      do not support unlock.
    * ``field_setter`` — optional callable ``(host, names, on) -> None``
      used to set/clear the named lock-bits. Defaults to writing each
      field via attribute-style access.

    The wrapper does not synthesise transactions on its own beyond the
    dispatch; see :meth:`lock` for the exact write order.
    """

    __slots__ = (
        "_field_setter",
        "_host",
        "_key_field",
        "_key_sequence",
        "_unlock_sequence_fn",
    )

    def __init__(
        self,
        host: object,
        *,
        key_field: str | None = None,
        key_sequence: Sequence[int] = (1,),
        unlock_sequence_fn: Callable[[object], None] | None = None,
        field_setter: Callable[[object, Sequence[str], bool], None] | None = None,
    ) -> None:
        self._host = host
        self._key_field = key_field
        self._key_sequence = tuple(key_sequence)
        self._unlock_sequence_fn = unlock_sequence_fn
        self._field_setter = field_setter

    @property
    def host(self) -> object:
        """The wrapped lock-register node."""
        return self._host

    def __repr__(self) -> str:
        key = f", key={self._key_field!r}" if self._key_field else ""
        return f"<LockController {_path_of(self._host)}{key}>"

    def lock(self, names: Iterable[str]) -> None:
        """Program the named lock-bits and run the key sequence.

        Issues two logical phases:

        1. Set the requested lock-bits to 1 (one or more bus writes,
           coalesced through ``field_setter`` when possible).
        2. Write the key field with each value from ``key_sequence``.

        Per the sketch §12.4 contract, the *minimum* observable bus traffic
        is two writes -- one for the lock-bits, one for the key.
        """
        names_list = list(names)
        if not names_list:
            raise ValueError("LockController.lock() requires at least one field name")
        self._set_locks(names_list, on=True)
        self._drive_key_sequence()

    def is_locked(self, name: str) -> bool:
        """Return whether ``name``'s lock-bit is currently set (one bus read)."""
        target = getattr(self._host, name, None)
        if target is None:
            raise AttributeError(f"{_path_of(self._host)}: no lock-field named {name!r}")
        return bool(int(target.read()))

    def unlock_sequence(self) -> None:
        """Run the vendor-specific unlock path, if any.

        Raises :class:`NotSupportedError` when no ``unlock_sequence_fn`` was
        registered. Many parts intentionally have no unlock path; the error
        makes that visible at the call site rather than silently no-op'ing.
        """
        if self._unlock_sequence_fn is None:
            raise NotSupportedError(f"{_path_of(self._host)}: no unlock_sequence is declared for this lock")
        self._unlock_sequence_fn(self._host)

    # -- internals -----------------------------------------------------------

    def _set_locks(self, names: Sequence[str], *, on: bool) -> None:
        """Set or clear the named lock-bits."""
        if self._field_setter is not None:
            self._field_setter(self._host, names, on)
            return

        modify = getattr(self._host, "modify", None)
        if callable(modify):
            modify(**{name: int(on) for name in names})
            return

        # Attribute-style fallback: write each field individually.
        for name in names:
            target = getattr(self._host, name, None)
            if target is None:
                raise AttributeError(f"{_path_of(self._host)}: no lock-field named {name!r}")
            target.write(int(on))

    def _drive_key_sequence(self) -> None:
        """Write each value in ``key_sequence`` to the key field."""
        if self._key_field is None:
            return
        modify = getattr(self._host, "modify", None)
        target = getattr(self._host, self._key_field, None)
        for value in self._key_sequence:
            if target is not None and hasattr(target, "write"):
                target.write(int(value))
            elif callable(modify):
                modify(**{self._key_field: int(value)})
            else:
                raise NotSupportedError(f"{_path_of(self._host)}: no key-field {self._key_field!r} to drive")


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def attach_counter(reg_or_field: object, **counter_kwargs: Any) -> Counter:  # noqa: ANN401
    """Build and attach a :class:`Counter` wrapper to ``reg_or_field``.

    The wrapper is also stored as ``reg_or_field.counter`` for discoverability.
    Returns the constructed wrapper so callers can use it directly.
    """
    counter = Counter(reg_or_field, **counter_kwargs)
    reg_or_field.counter = counter  # type: ignore[attr-defined]
    return counter


def attach_lock_controller(reg: object, **lock_kwargs: Any) -> LockController:  # noqa: ANN401
    """Construct a :class:`LockController` and bind it onto ``reg``.

    Adds ``reg.lock``, ``reg.is_locked``, and ``reg.unlock_sequence`` as
    bound method-style aliases of the controller's methods.
    """
    controller = LockController(reg, **lock_kwargs)
    # Methods are exposed directly on the host so user code reads
    # ``soc.gpio_a.lckr.lock([...])`` rather than ``...lckr.controller.lock(...)``.
    reg.lock = controller.lock  # type: ignore[attr-defined]
    reg.is_locked = controller.is_locked  # type: ignore[attr-defined]
    reg.unlock_sequence = controller.unlock_sequence  # type: ignore[attr-defined]
    reg._lock_controller = controller  # type: ignore[attr-defined]
    return controller


def attach_specialized(cls: type, metadata: dict[str, object]) -> None:
    """Inspect ``metadata`` for counter / singlepulse / lock UDPs and wire helpers.

    This is the registry callback invoked by Unit 1's ``register_register_enhancement``.
    ``metadata`` is the per-register dict produced by the template and may
    carry the keys:

    * ``"counter"`` — dict of counter spec kwargs (forwarded to :class:`Counter`).
    * ``"lock"``    — dict of lock spec kwargs (forwarded to :class:`LockController`).
    * ``"singlepulse_fields"`` — iterable of field names to attach ``pulse()`` to.

    Each entry is best-effort: missing metadata keys are simply skipped.
    """
    attach_reset_helpers(cls)

    counter_spec = metadata.get("counter")
    if counter_spec:
        # Counter is host-bound at construction; here we install a class-level
        # property that materialises a per-instance wrapper on first access.
        _install_counter_property(cls, counter_spec)

    lock_spec = metadata.get("lock")
    if lock_spec:
        _install_lock_property(cls, lock_spec)

    pulse_fields = metadata.get("singlepulse_fields") or ()
    for field_name in pulse_fields:
        field_cls = getattr(cls, field_name, None)
        if isinstance(field_cls, type):
            attach_pulse(field_cls)


def _install_counter_property(cls: type, spec: dict[str, object]) -> None:
    """Install a lazily-constructed ``counter`` property on ``cls``."""
    if "counter" in cls.__dict__:
        return
    cache_key = "_counter_wrapper"

    def _getter(self: object) -> Counter:
        cached = self.__dict__.get(cache_key)
        if cached is not None:
            return cached
        wrapper = Counter(self, **spec)
        self.__dict__[cache_key] = wrapper
        return wrapper

    cls.counter = property(_getter)  # type: ignore[attr-defined]


def _install_lock_property(cls: type, spec: dict[str, object]) -> None:
    """Install lock-controller methods on ``cls`` (lazy; one wrapper per instance)."""
    if "lock" in cls.__dict__:
        return
    cache_key = "_lock_controller_wrapper"

    def _ensure(self: object) -> LockController:
        cached = self.__dict__.get(cache_key)
        if cached is not None:
            return cached
        wrapper = LockController(self, **spec)
        self.__dict__[cache_key] = wrapper
        return wrapper

    def _lock(self: object, names: Iterable[str]) -> None:
        _ensure(self).lock(names)

    def _is_locked(self: object, name: str) -> bool:
        return _ensure(self).is_locked(name)

    def _unlock_sequence(self: object) -> None:
        _ensure(self).unlock_sequence()

    cls.lock = _lock  # type: ignore[attr-defined]
    cls.is_locked = _is_locked  # type: ignore[attr-defined]
    cls.unlock_sequence = _unlock_sequence  # type: ignore[attr-defined]


def attach_post_create(soc: object) -> None:
    """Wire SoC-level helpers (the §12.3 ``soc.reset_all()`` entry point).

    Idempotent. Called by Unit 1's ``register_post_create`` seam.
    """
    if not hasattr(soc, "reset_all"):
        soc.reset_all = lambda *, rw_only=True: reset_all_soc(soc, rw_only=rw_only)


# ---------------------------------------------------------------------------
# Optional registration with the runtime registry (Unit 1's seam).
# When Unit 1 is not yet merged the import fails and we silently skip;
# this keeps the file importable in isolation.
# ---------------------------------------------------------------------------


def _noop_decorator(fn: Callable[..., object]) -> Callable[..., object]:
    """No-op decorator used when the registry is unavailable."""
    return fn


try:  # pragma: no cover - import shim
    from peakrdl_pybind11.runtime._registry import (  # type: ignore[import-not-found]
        register_post_create as _registry_register_post_create,
    )
    from peakrdl_pybind11.runtime._registry import (  # type: ignore[import-not-found]
        register_register_enhancement as _registry_register_register_enhancement,
    )
except ImportError:  # pragma: no cover - sibling unit may not exist yet
    register_register_enhancement: Callable[..., object] = _noop_decorator
    register_post_create: Callable[..., object] = _noop_decorator
else:
    register_register_enhancement = _registry_register_register_enhancement
    register_post_create = _registry_register_post_create
    register_register_enhancement(attach_specialized)  # type: ignore[arg-type]
    register_post_create(attach_post_create)  # type: ignore[arg-type]
