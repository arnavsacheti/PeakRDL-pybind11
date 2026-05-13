"""Composable region-based master routing.

Implements the bus-master routing layer described in
``docs/IDEAL_API_SKETCH.md`` §13.1. A :class:`Router` accepts one or
more masters, each tied to a *region* of the SoC's address space, and
dispatches each ``read``/``write`` to the master whose region contains
the target address.

Regions can be specified four ways via ``where=``:

* ``where=None`` — catch-all default; the master serves the entire tree.
* ``where="peripherals.uart.*"`` — fnmatch-style glob on node paths.
* ``where=lambda node: node.info.is_external`` — callable predicate.
* ``where=(0x4000_0000, 0x5000_0000)`` — half-open ``[start, end)`` range.

When several rules match the same address, the *most-specific* one wins:

1. An explicit address-range tuple beats any glob, predicate, or catch-all
   (smaller ranges break ties between two range tuples).
2. A glob beats any predicate or catch-all. Globs with more literal
   path segments beat globs with fewer (``peripherals.uart.*`` (2 literal
   segments) beats ``peripherals.*`` (1) beats ``*`` (0)).
3. A predicate beats only the catch-all.
4. Any explicit ``where=`` beats ``where=None``.
5. On any remaining tie, the first-registered rule wins (stable).

Sibling-unit notes
------------------
The ideal API in §13.1 wires the router via a ``register_post_create``
seam exposed by Unit 1. That seam doesn't yet exist on
``exp/api_overhaul``; this module ships :class:`Router` and the free
function :func:`attach_master` standalone, so generated SoC objects (or
hand-rolled fakes) can plug it in directly. When Unit 2 lands its
project-wide :class:`RoutingError` in a shared ``runtime/errors.py``,
the definition here will be replaced by a re-export.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from ..masters.base import AccessOp


@runtime_checkable
class _NodeInfo(Protocol):
    """Subset of ``node.info`` (§4.2) used by the router.

    Real generated nodes expose far more; tests only need these four.
    """

    path: str
    address: int
    size: int
    is_external: bool


@runtime_checkable
class NodeLike(Protocol):
    """Subset of an SoC node the router traverses.

    ``walk()`` must yield ``self`` followed by every descendant.
    Generated nodes will satisfy this once Unit 4 lands; tests stub it
    with plain dataclasses.
    """

    @property
    def info(self) -> _NodeInfo: ...

    def walk(self) -> Iterable[NodeLike]: ...


@runtime_checkable
class MasterLike(Protocol):
    """Subset of :class:`~peakrdl_pybind11.masters.base.MasterBase` used here."""

    def read(self, address: int, width: int) -> int: ...

    def write(self, address: int, value: int, width: int) -> None: ...


WhereGlob = str
WherePredicate = Callable[[NodeLike], bool]
WhereRange = tuple[int, int]
Where = WhereGlob | WherePredicate | WhereRange | None


class _Tier(IntEnum):
    """Specificity tier for a rule. Higher beats lower."""

    CATCH_ALL = 0
    PREDICATE = 1
    GLOB = 2
    RANGE = 3


class RoutingError(LookupError):
    """No attached master serves the requested address.

    Raised by :meth:`Router.read` and :meth:`Router.write` when no rule
    matches *and* no catch-all is attached. The error carries the
    offending address so callers can render whatever context they want.

    Unit 2 will eventually own a project-wide :class:`RoutingError`;
    until then this class lives here and is re-exported from
    :mod:`peakrdl_pybind11.runtime`.
    """

    def __init__(self, address: int, message: str | None = None) -> None:
        self.address = int(address)
        if message is None:
            message = f"no master attached for 0x{self.address:08x}"
        else:
            message = f"{message} (addr=0x{self.address:08x})"
        super().__init__(message)


@dataclass(frozen=True)
class _Range:
    """Half-open ``[start, end)`` address range."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(
                f"range end (0x{self.end:x}) must be strictly greater than start (0x{self.start:x})"
            )

    def contains(self, addr: int) -> bool:
        return self.start <= addr < self.end

    @property
    def size(self) -> int:
        return self.end - self.start


def _glob_specificity(pattern: str) -> int:
    """Number of literal (non-wildcard) dot-separated segments in ``pattern``.

    Wildcard metacharacters: ``*``, ``?``, ``[``. Used to break ties
    between overlapping globs — more literal segments beats fewer.
    """

    return sum(1 for seg in pattern.split(".") if seg and not any(c in seg for c in "*?["))


@dataclass
class RoutingRule:
    """One ``(predicate-or-region, master)`` mapping inside a :class:`Router`.

    Created by :meth:`Router.attach_master`; users normally don't
    construct this directly. Stored fields:

    - ``master`` — the bus master to dispatch to.
    - ``where`` — the original ``where=`` argument (also drives dispatch).
    - ``ranges`` — pre-resolved address ranges (empty for catch-all).
    - ``order`` — insertion order, used for stable tie-breaking.
    """

    master: MasterLike
    where: Where = None
    ranges: tuple[_Range, ...] = field(default_factory=tuple)
    order: int = 0

    @property
    def is_catch_all(self) -> bool:
        return self.where is None

    def matches_address(self, addr: int) -> bool:
        if self.is_catch_all:
            return True
        return any(r.contains(addr) for r in self.ranges)

    def specificity(self) -> tuple[int, int]:
        """Specificity score as ``(tier, sub-rank)``; higher beats lower.

        ``sub-rank`` only matters between two rules in the same tier:

        - ``RANGE``: ``-size`` of the range, so the smallest range wins.
        - ``GLOB``: literal-segment count from :func:`_glob_specificity`.
        - ``PREDICATE`` / ``CATCH_ALL``: always 0; the surrounding tie-
          breaker (``order``) decides.
        """

        where = self.where
        if where is None:
            return (_Tier.CATCH_ALL, 0)
        if isinstance(where, tuple):
            size = self.ranges[0].size if self.ranges else 0
            return (_Tier.RANGE, -size)
        if isinstance(where, str):
            return (_Tier.GLOB, _glob_specificity(where))
        # callable predicate
        return (_Tier.PREDICATE, 0)


class Router:
    """Region-based dispatch over multiple bus masters.

    A :class:`Router` is itself a :class:`MasterLike`-shaped object: it
    exposes :meth:`read`, :meth:`write`, :meth:`read_many`, and
    :meth:`write_many`, and forwards each call to the master whose
    attached region contains the target address.

    Construct it with an optional catch-all default
    (``Router(default)``), then add region rules with
    :meth:`attach_master`.

    Example::

        router = Router()
        router.attach_master(jtag, where="peripherals.uart.*", soc=soc)
        router.attach_master(mem_master, where=(0x4000_0000, 0x4001_0000))
        router.attach_master(MockMaster(),
                             where=lambda n: n.info.is_external,
                             soc=soc)
        router.read(0x4000_1000, width=4)

    Glob and predicate rules walk ``soc`` once at attach time and cache
    the matching ``(start, end)`` ranges, so the per-access path never
    needs an address-to-node reverse lookup. Range tuples and
    ``where=None`` don't need a tree.
    """

    def __init__(self, default_master: MasterLike | None = None) -> None:
        self._rules: list[RoutingRule] = []
        self._next_order = 0
        if default_master is not None:
            self._attach_rule(default_master, where=None)

    def __repr__(self) -> str:
        rules = ", ".join(f"{r.where!r}->{type(r.master).__name__}" for r in self._rules)
        return f"Router({rules})"

    # ------------------------------------------------------------------
    # Public API: attaching masters
    # ------------------------------------------------------------------
    def attach_master(
        self,
        master: MasterLike,
        where: Where = None,
        *,
        soc: NodeLike | None = None,
    ) -> RoutingRule:
        """Attach ``master`` to serve all addresses matching ``where``.

        See the module docstring for the four shapes ``where`` can take.
        Glob and predicate forms walk ``soc`` at attach time to resolve
        the matching set of address ranges; pass ``soc=`` for those
        forms. Range tuples and ``where=None`` don't require a tree.
        """

        return self._attach_rule(master, where=where, soc=soc)

    def _attach_rule(
        self,
        master: MasterLike,
        where: Where,
        soc: NodeLike | None = None,
    ) -> RoutingRule:
        ranges = self._resolve_ranges(where, soc)
        rule = RoutingRule(
            master=master,
            where=where,
            ranges=ranges,
            order=self._next_order,
        )
        self._next_order += 1
        self._rules.append(rule)
        return rule

    @staticmethod
    def _resolve_ranges(where: Where, soc: NodeLike | None) -> tuple[_Range, ...]:
        if where is None:
            return ()
        if isinstance(where, tuple):
            start, end = where
            return (_Range(int(start), int(end)),)
        if isinstance(where, str):
            if soc is None:
                raise ValueError(
                    "glob 'where=' requires soc=; pass the SoC root so the "
                    "router can resolve matching addresses at attach time"
                )
            return _resolve_glob(soc, where)
        if callable(where):
            if soc is None:
                raise ValueError(
                    "predicate 'where=' requires soc=; pass the SoC root so "
                    "the router can resolve matching addresses at attach time"
                )
            return _resolve_predicate(soc, where)
        raise TypeError(
            f"where= must be None, a glob string, a (start, end) tuple, "
            f"or a callable predicate; got {type(where).__name__}"
        )

    # ------------------------------------------------------------------
    # Public API: bus operations
    # ------------------------------------------------------------------
    def read(self, address: int, width: int = 4) -> int:
        return self._lookup(address).master.read(address, width)

    def write(self, address: int, value: int, width: int = 4) -> None:
        self._lookup(address).master.write(address, value, width)

    def read_many(self, ops: Sequence[AccessOp]) -> list[int]:
        return [self._lookup(op.address).master.read(op.address, op.width) for op in ops]

    def write_many(self, ops: Sequence[AccessOp]) -> None:
        for op in ops:
            self._lookup(op.address).master.write(op.address, op.value, op.width)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def rules(self) -> tuple[RoutingRule, ...]:
        return tuple(self._rules)

    def master_for(self, address: int) -> MasterLike:
        """Master that would service ``address``; raises :class:`RoutingError`."""

        return self._lookup(address).master

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _lookup(self, address: int) -> RoutingRule:
        addr = int(address)
        best: RoutingRule | None = None
        best_score: tuple[int, int, int] | None = None
        for rule in self._rules:
            if not rule.matches_address(addr):
                continue
            tier, sub = rule.specificity()
            # Negate ``order`` so first-registered (lowest order) wins
            # under tuple comparison.
            score = (tier, sub, -rule.order)
            if best_score is None or score > best_score:
                best = rule
                best_score = score
        if best is None:
            raise RoutingError(addr)
        return best


# ----------------------------------------------------------------------
# Tree walking helpers
# ----------------------------------------------------------------------

# Attribute names that signal a child is a "container" (regfile / addrmap /
# mem) worth descending into when ``vars(node)`` is enumerated. Duck typing
# only — we avoid importing the generated C++ classes from this module so
# the runtime stays decoupled from the per-SoC bindings.
_CHILD_ATTR_HINTS = ("read", "write", "bits", "lsb", "offset", "info")


def _kind_for(node: Any) -> str:
    """Coarse classification of ``node``. Returns one of:

    ``"Signal"`` — :class:`~peakrdl_pybind11.runtime.signals.Signal`
                   instance (metadata-only, no bus access).
    ``"Field"`` — has ``bits`` or ``lsb`` (and no ``read``/``write``).
    ``"Reg"``   — has ``read`` and ``write`` callables (and is not a field).
    ``"Mem"``   — type name contains ``"mem"`` (case-insensitive substring).
    ``"RegFile"``/``"AddrMap"`` — type name contains those tokens.
    ``""`` otherwise.
    """

    if node is None:
        return ""
    # Signals must be tested before the bits/lsb branch: ``Signal.lsb``
    # is part of its dataclass schema, so the duck-typed Field check
    # below would otherwise grab it. Lazy import to avoid the circular
    # import — ``signals`` imports ``_registry`` which is part of the
    # same package and loads after ``routing`` alphabetically.
    from .signals import Signal as _Signal

    if isinstance(node, _Signal):
        return "Signal"
    cls_name = type(node).__name__.lower()
    # Fields: bits/lsb take precedence over read/write because some
    # generated field types also expose a ``read()`` for typed readback.
    has_bits = hasattr(node, "bits") or hasattr(node, "lsb")
    has_rw = callable(getattr(node, "read", None)) and callable(getattr(node, "write", None))
    if has_bits and not has_rw:
        return "Field"
    if "field" in cls_name:
        return "Field"
    if has_rw and not has_bits:
        return "Reg"
    if "reg" in cls_name and "regfile" not in cls_name:
        return "Reg"
    if "mem" in cls_name:
        return "Mem"
    if "regfile" in cls_name:
        return "RegFile"
    if "addrmap" in cls_name or "soc" in cls_name:
        return "AddrMap"
    return ""


def _looks_like_child(value: Any) -> bool:
    """Duck-typed check: is ``value`` a node we should descend into?

    Two recognition paths:

    1. *Leaf* nodes (registers/fields) expose at least one of the hint
       attributes from :data:`_CHILD_ATTR_HINTS` (``read``/``write`` for
       registers, ``bits``/``lsb`` for fields, etc.).
    2. *Container* nodes (regfiles/addrmaps/mems) have no hint attribute
       themselves but their ``__dict__`` contains at least one
       node-like child. We detect them by inspecting ``vars(value)`` and
       recursing one level — bounded recursion, since a container has
       to bottom out at leaves to be useful.
    """

    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return False
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return False
    if callable(value) and not hasattr(value, "__dict__"):
        return False
    # Path 1: leaf-style node with a recognised hint attribute.
    if any(hasattr(value, attr) for attr in _CHILD_ATTR_HINTS):
        return True
    # Path 2: container-style node — has its own ``__dict__`` and at
    # least one nested attribute that itself exposes a hint attribute.
    try:
        nested = vars(value)
    except TypeError:
        return False
    for child_name, child in nested.items():
        if child_name.startswith("_"):
            continue
        if child is value:
            continue
        if any(hasattr(child, attr) for attr in _CHILD_ATTR_HINTS):
            return True
    return False


def _iter_children(node: Any) -> Iterator[Any]:
    """Yield duck-typed child nodes of ``node`` in deterministic order.

    Iterates ``vars(node)`` (the instance ``__dict__``) and filters out
    private members, the parent back-pointer, the bus master, and any
    non-node-like values. Order matches insertion order in the dict —
    on CPython 3.7+ that's deterministic per construction.
    """

    try:
        items = vars(node)
    except TypeError:
        return
    for name, value in items.items():
        if name.startswith("_"):
            continue
        if name in ("parent", "master", "info"):
            continue
        if value is node:
            continue
        if _looks_like_child(value):
            yield value


def _walk(node: Any, *, kind: str | None = None) -> Iterator[Any]:
    """Pre-order traversal of the SoC tree rooted at ``node``.

    Two modes:

    * If ``node.walk()`` exists, use it. This preserves the existing
      :class:`NodeLike` Protocol contract (FakeNodes in router tests
      implement ``.walk()`` directly) and lets generated trees pick
      whichever shape they prefer.
    * Otherwise fall back to a duck-typed pre-order traversal over
      ``vars(node)``. This is what the discovery API uses against real
      pybind11 SoCs which don't ship a ``.walk()`` method.

    The optional ``kind`` filter accepts a string token (case-insensitive
    substring match against either :func:`_kind_for` or the node's class
    name, e.g. ``"Reg"`` matches a generated ``uart_control_t``). Nodes
    that don't match the filter are skipped but their children are still
    traversed — a containing ``AddrMap`` shouldn't hide its registers.
    """

    walk_fn = getattr(node, "walk", None)
    # Skip the bound ``walk`` we install ourselves (post-create hook):
    # calling it would recurse straight back into _walk on the same node.
    if (
        walk_fn is not None
        and callable(walk_fn)
        and not getattr(walk_fn, "__peakrdl_discovery_walk__", False)
    ):
        try:
            from collections.abc import Iterable as _Iterable

            seq: list[Any] = list(cast("_Iterable[Any]", walk_fn()))
        except (TypeError, AttributeError):
            seq = []
        if not seq or seq[0] is not node:
            seq.insert(0, node)
        seen: set[int] = set()
        for n in seq:
            key = id(n)
            if key in seen:
                continue
            seen.add(key)
            if _matches_kind(n, kind):
                yield n
        return

    # Duck-typed fallback: pre-order DFS over vars() with a visited set
    # so cycles (back-pointers we somehow let through the filter) don't
    # loop forever.
    visited: set[int] = set()
    stack: list[Any] = [node]
    while stack:
        cur = stack.pop()
        key = id(cur)
        if key in visited:
            continue
        visited.add(key)
        if _matches_kind(cur, kind):
            yield cur
        # Push children in reverse so pre-order DFS keeps the natural
        # left-to-right traversal order users see when reading vars().
        children = list(_iter_children(cur))
        for child in reversed(children):
            stack.append(child)


def _matches_kind(node: Any, kind: str | None) -> bool:
    """``True`` if ``node`` matches the ``kind`` filter token.

    Empty / None filter matches everything. Otherwise compares the token
    (case-insensitive) against both the duck-typed classification from
    :func:`_kind_for` and the node's class name — so ``kind="reg"``
    catches a duck-typed ``"Reg"`` and a generated ``uart_control_reg_t``.
    """

    if not kind:
        return True
    token = kind.lower()
    duck = _kind_for(node).lower()
    if duck and token in duck:
        return True
    cls_name = type(node).__name__.lower()
    return token in cls_name


def _node_range(node: NodeLike) -> _Range | None:
    """``[address, address+size)`` for ``node``, or ``None`` if missing/invalid."""

    info = getattr(node, "info", None)
    if info is None:
        return None
    addr = getattr(info, "address", None)
    size = getattr(info, "size", None)
    if addr is None or size is None or size <= 0:
        return None
    return _Range(int(addr), int(addr) + int(size))


def _coalesce(ranges: Iterable[_Range]) -> tuple[_Range, ...]:
    """Merge overlapping/adjacent ``_Range`` objects."""

    sorted_ranges = sorted(ranges, key=lambda r: (r.start, r.end))
    merged: list[_Range] = []
    for r in sorted_ranges:
        if merged and r.start <= merged[-1].end:
            last = merged[-1]
            merged[-1] = _Range(last.start, max(last.end, r.end))
        else:
            merged.append(r)
    return tuple(merged)


def _resolve_glob(root: NodeLike, pattern: str) -> tuple[_Range, ...]:
    """Merged address ranges of nodes whose ``info.path`` matches ``pattern``."""

    out: list[_Range] = []
    for node in _walk(root):
        info = getattr(node, "info", None)
        path = getattr(info, "path", None) if info is not None else None
        if not isinstance(path, str):
            continue
        if fnmatch.fnmatchcase(path, pattern):
            rng = _node_range(node)
            if rng is not None:
                out.append(rng)
    return _coalesce(out)


def _resolve_predicate(
    root: NodeLike,
    predicate: WherePredicate,
) -> tuple[_Range, ...]:
    """Merged address ranges of nodes for which ``predicate`` is truthy."""

    out: list[_Range] = []
    for node in _walk(root):
        try:
            ok = bool(predicate(node))
        except Exception:
            # User-supplied predicates may raise on unrelated nodes
            # (e.g. ``n.info.is_external`` on a node without that
            # attribute). Skip rather than abort the whole attach.
            ok = False
        if not ok:
            continue
        rng = _node_range(node)
        if rng is not None:
            out.append(rng)
    return _coalesce(out)


# ----------------------------------------------------------------------
# Free-function entry point — the API sketch §13.1 spells
# ``soc.attach_master(...)``, but Unit 1's post-create seam isn't here
# yet. This helper installs a Router on any SoC that exposes a
# writable ``master`` attribute, so callers can use the sketch's API
# verbatim today.
# ----------------------------------------------------------------------
def attach_master(
    soc: NodeLike,
    master: MasterLike,
    where: Where = None,
) -> Router:
    """Attach ``master`` to ``soc`` via a :class:`Router`.

    If ``soc.master`` is already a :class:`Router`, the new rule is
    added to it. Otherwise this creates a fresh router seeded with the
    existing ``soc.master`` (if any) as the catch-all default, replaces
    ``soc.master`` with the router, and adds the new rule.
    """

    existing = getattr(soc, "master", None)
    if isinstance(existing, Router):
        router = existing
    else:
        router = Router(default_master=existing)
        try:
            soc.master = router  # type: ignore[attr-defined]
        except AttributeError as exc:
            raise TypeError(
                f"cannot install Router on {type(soc).__name__}: 'master' attribute is not assignable"
            ) from exc

    router.attach_master(master, where=where, soc=soc)
    return router


# ----------------------------------------------------------------------
# Post-create hook — wires ``soc.attach_master(master, where=...)`` into
# every generated SoC. The C++ binding's ``attach_master`` signature is
# ``(Master*) -> None`` and rejects unknown kwargs, so we wrap it: with
# ``where=None`` the call goes straight through; with any ``where=`` the
# wrapper installs a :class:`Router` (creating it on first use), adds
# the rule, and re-attaches the router as the C++ master.
# ----------------------------------------------------------------------
def attach_router(soc: Any) -> None:
    """Wrap ``soc.attach_master`` to accept ``where=`` routing kwargs.

    Captures the original C++ ``attach_master(master)`` and replaces it
    with a Python wrapper that:

    * calls the original directly when ``where`` is ``None`` (or when
      no kwargs were passed), preserving the existing fast path; and
    * for any non-``None`` ``where``, lazily creates a :class:`Router`,
      registers ``(master, where)`` as a rule, and re-installs the
      router as the SoC's C++ master via the captured original.

    The router itself satisfies ``MasterLike`` so the C++ side keeps
    seeing one master object even though Python may have layered
    several rules on top of it.

    Idempotent: a second call is a no-op so post-create hooks can fire
    repeatedly (e.g. across reloads) without double-wrapping.
    """

    existing = getattr(soc, "attach_master", None)
    if existing is None:
        return
    if getattr(existing, "__peakrdl_router_wrapper__", False):
        return

    orig_attach: Callable[[MasterLike], None] = existing
    # Closure state — one Router per wrapped SoC. We only construct it
    # the first time a ``where=`` rule arrives so SoCs that never use
    # routing pay nothing.
    router_box: list[Router | None] = [None]

    def attach_master_wrapper(master: MasterLike, where: Where = None) -> None:
        """Wrapper around the C++ ``attach_master`` that honours ``where=``."""
        if where is None:
            orig_attach(master)
            return

        router = router_box[0]
        if router is None:
            router = Router()
            router_box[0] = router
        router.attach_master(master, where=where, soc=soc)
        # Re-install the router each time so the C++ side propagates the
        # current router to every child node. Cheap — ``attach_master``
        # on the SoC just calls ``set_master`` + ``propagate_master``.
        orig_attach(router)

    attach_master_wrapper.__peakrdl_router_wrapper__ = True  # type: ignore[attr-defined]
    soc.attach_master = attach_master_wrapper  # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# Discovery API (sketch §4.2): soc.find(addr), soc.find_by_name(name),
# soc.walk(kind=...).
#
# All three are exposed as bound methods on the SoC via the post-create
# registry seam (mirroring ``transactions._attach_batch_to_soc``). The
# free functions below are the real implementations; the seam just
# closes a Python closure over the SoC and attaches it. Stays decoupled
# from the C++ classes — we duck-type the tree.
# ----------------------------------------------------------------------


def _find_by_addr(soc: Any, addr: int) -> Any | None:
    """Return the first register in ``soc`` whose ``.offset == addr``.

    Walks the tree pre-order and returns the *first* node that both
    duck-types as a register and exposes a matching ``.offset``. Returns
    ``None`` if no match is found. Address matching is exact; callers
    that need range-based lookup should use the :class:`Router`
    machinery instead.
    """

    target = int(addr)
    for node in _walk(soc):
        if _kind_for(node) != "Reg":
            continue
        offset = getattr(node, "offset", None)
        if offset is None:
            continue
        try:
            if int(offset) == target:
                return node
        except (TypeError, ValueError):
            continue
    return None


def _find_by_name(soc: Any, name: str) -> list:
    """Return every node in ``soc`` whose ``.name`` matches ``name``.

    Case-insensitive substring match against ``node.name`` (falls back
    to ``type(node).__name__`` if the instance has no ``name`` attr).
    Returns a list (possibly empty) — never raises, never returns
    ``None``. Order matches the pre-order traversal so callers can
    treat ``result[0]`` as the first match when they expect a unique
    hit.
    """

    needle = (name or "").lower()
    if not needle:
        return []
    matches: list[Any] = []
    for node in _walk(soc):
        node_name = getattr(node, "name", None)
        if not isinstance(node_name, str):
            continue
        if needle in node_name.lower():
            matches.append(node)
    return matches


def _bound_walk(soc: Any) -> Callable[..., Iterator[Any]]:
    """Build a bound ``walk(kind=None)`` callable closed over ``soc``."""

    def walk(kind: str | None = None) -> Iterator[Any]:
        return _walk(soc, kind=kind)

    # Marker so ``_walk`` doesn't recurse into its own bound wrapper
    # when it discovers ``soc.walk`` via getattr().
    walk.__peakrdl_discovery_walk__ = True  # type: ignore[attr-defined]
    return walk


def _try_setattr(obj: Any, name: str, value: Any) -> None:
    """``setattr`` that swallows the rejection from pybind11 classes.

    pybind11 classes without ``py::dynamic_attr()`` raise
    :class:`AttributeError` (or :class:`TypeError`) on ``setattr``; we
    treat that as "nothing to attach here" so the registry hook stays a
    no-op for raw C++ SoC objects.
    """

    try:
        setattr(obj, name, value)
    except (AttributeError, TypeError):
        pass


def attach_discovery(soc: Any) -> None:
    """Attach ``find`` / ``find_by_name`` / ``walk`` bound methods to ``soc``.

    Idempotent: if the SoC already exposes any of the three callables
    (set previously, or natively provided by the generated class), the
    existing implementation is left alone. Best-effort: failures to
    ``setattr`` on a slotted/pybind11 SoC are swallowed silently.
    """

    if not hasattr(soc, "find") or not callable(getattr(soc, "find", None)):

        def _bound_find(addr: int) -> Any | None:
            return _find_by_addr(soc, addr)

        _try_setattr(soc, "find", _bound_find)

    if not hasattr(soc, "find_by_name") or not callable(getattr(soc, "find_by_name", None)):

        def _bound_find_by_name(name: str) -> list:
            return _find_by_name(soc, name)

        _try_setattr(soc, "find_by_name", _bound_find_by_name)

    # ``walk`` is a little trickier — Router's NodeLike protocol declares
    # ``walk()`` (no kwargs), and FakeNode in our existing tests already
    # implements it. We only attach the discovery flavor if no such
    # method is already present. If the SoC already has ``walk``, we
    # respect it.
    if not hasattr(soc, "walk") or not callable(getattr(soc, "walk", None)):
        _try_setattr(soc, "walk", _bound_walk(soc))


# ----------------------------------------------------------------------
# Registry wiring (sibling-dep: Unit 1's runtime/_registry).
#
# When the registry seam is present we register :func:`attach_router`
# as a post-create hook so every ``MySoc.create()`` automatically gains
# the ``soc.attach_master(master, where=...)`` overload. When it isn't
# (this unit can land before Unit 1), the import quietly fails and
# callers can still use :func:`attach_router` or the free
# :func:`attach_master` helper above explicitly.
# ----------------------------------------------------------------------

try:  # pragma: no cover - depends on Unit 1 landing order
    from . import _registry  # type: ignore[attr-defined]
except ImportError:
    _registry = None  # type: ignore[assignment]

if _registry is not None and hasattr(_registry, "register_post_create"):
    _registry.register_post_create(attach_router)
    _registry.register_post_create(attach_discovery)
