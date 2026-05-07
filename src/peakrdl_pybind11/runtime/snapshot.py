"""Snapshot, diff, and restore primitives.

Implements §15 of ``docs/IDEAL_API_SKETCH.md``. A snapshot is an immutable,
hashable, picklable, JSON-serializable image of every readable register or
field beneath an SoC node, captured at a single instant.

Two complementary access shapes are provided:

* ``snap[path]`` — explicit dotted-path access (``"uart.control"``). Returns
  the captured ``int`` for a leaf path, or a prefix-filtered :class:`Snapshot`
  for an interior path.
* ``snap.uart.control`` — structural attribute access. Each attribute step
  walks deeper into the tree.

The companion :class:`SnapshotDiff` lists changes between two snapshots and
provides ``assert_only_changed`` for CI-style assertions on which paths a
test was *allowed* to touch.

The module-level :func:`register_post_create` is the seam that future
generated SoC modules wire into: it binds ``snapshot()`` and ``restore()``
methods onto a soc instance.
"""

from __future__ import annotations

import fnmatch
import html
import json
import types
import warnings
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

# Unit 4 will define a typed Info; for now we type it loosely so callers can
# pass anything with ``.path`` / ``.access`` / ``.on_read``.
Info = Any
PathStr = str

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

# Sibling Unit 2 will own the canonical exception hierarchy. Until it lands,
# define ``SideEffectError`` here and try to alias the canonical version when
# available. This preserves ``except SideEffectError`` ergonomics across the
# transition.
try:  # pragma: no cover — sibling unit not present in this PR
    from peakrdl_pybind11.errors import SideEffectError as _CanonicalSideEffectError

    SideEffectError = _CanonicalSideEffectError
except ImportError:

    class SideEffectError(RuntimeError):
        """A read or write would have a side effect that the caller forbade.

        Raised when ``soc.snapshot()`` would have to issue a destructive read
        (e.g. clear-on-read) without ``allow_destructive=True``, or when
        :func:`peek` is called on a register or master that cannot peek.
        """


# ---------------------------------------------------------------------------
# Internal: peek-with-fallback
# ---------------------------------------------------------------------------

# A node is a stand-in for a future ``Reg``/``Field`` node. The minimum
# surface used here is:
#   .info: object with .path: str, .access: str, .on_read: str | None
#   .peek() -> int                 (preferred; non-side-effecting)
#   .read() -> int                 (fallback; may have side effects)
#   .write(value: int) -> None     (used by .restore())
# Mocks in tests implement exactly this surface.


_PEEK_FALLBACK_WARNED: set[str] = set()


def _is_destructive_read(info: Info) -> bool:
    """Return True if reading this node has user-visible side effects.

    The check looks at ``info.on_read`` (e.g. ``"rclr"``, ``"rset"``,
    ``"ruser"``) and falls back to inspecting the access mode for the
    common ``"rclr"`` shorthand.
    """
    on_read = getattr(info, "on_read", None)
    if isinstance(on_read, str):
        normalized = on_read.lower()
        if normalized and normalized != "none":
            return True
    access = getattr(info, "access", "") or ""
    if isinstance(access, str):
        access_l = access.lower()
        for marker in ("rclr", "rset", "ruser"):
            if marker in access_l:
                return True
    return False


def _can_write(info: Info) -> bool:
    """Return True if the node accepts writes per its access mode."""
    access = getattr(info, "access", "") or ""
    if not isinstance(access, str):
        return False
    return "w" in access.lower()


def _peek_or_read(node: Any, *, allow_destructive: bool) -> int:
    """Read ``node`` without changing hardware state when possible.

    Tries ``node.peek()`` first. If peek isn't available, falls back to
    ``node.read()`` after consulting ``info.on_read`` to confirm it is safe;
    otherwise raises :class:`SideEffectError` (unless ``allow_destructive``).
    """
    info = getattr(node, "info", None)
    path = getattr(info, "path", None) or repr(node)

    peek = getattr(node, "peek", None)
    if callable(peek):
        try:
            return int(peek())
        except SideEffectError:
            if allow_destructive:
                # The bus or node refused peek; fall through to read.
                pass
            else:
                raise
        except NotImplementedError:
            # Master can't peek — fall through to the read-based path below.
            pass

    # No peek available. If the read is destructive, gate behind the flag.
    destructive = info is not None and _is_destructive_read(info)
    if destructive and not allow_destructive:
        raise SideEffectError(
            f"reading '{path}' would have side effects (on_read="
            f"{getattr(info, 'on_read', getattr(info, 'access', None))!r}); "
            "pass allow_destructive=True to override"
        )

    # Warn once-per-path that we're using read() instead of peek(). Skip the
    # warning when the user has already acknowledged the destructive read via
    # allow_destructive=True — they know what they asked for.
    if not destructive and path not in _PEEK_FALLBACK_WARNED:
        _PEEK_FALLBACK_WARNED.add(path)
        warnings.warn(
            f"snapshot: '{path}' has no peek(); falling back to read(). "
            "This may be safe for non-volatile, non-side-effecting registers.",
            stacklevel=3,
        )
    return int(node.read())


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class Snapshot:
    """An immutable image of a subtree of register state.

    A :class:`Snapshot` carries a flat ``path -> value`` mapping and a
    parallel ``path -> info`` mapping. The flat shape is what gets serialized
    (JSON, pickle) and what compares cleanly across runs; structural access
    via ``__getattr__`` and ``__getitem__`` is provided for ergonomics.

    Snapshots are immutable, hashable on values only (metadata is ignored
    for hashing because info is descriptive, not data), and picklable.
    """

    __slots__ = ("_metadata", "_prefix", "_values")

    def __init__(
        self,
        values: dict[PathStr, int] | None = None,
        metadata: dict[PathStr, Info] | None = None,
        *,
        _prefix: str = "",
    ) -> None:
        # Defensive copies so callers can't mutate snapshot state externally.
        self._values: dict[PathStr, int] = dict(values or {})
        self._metadata: dict[PathStr, Info] = dict(metadata or {})
        self._prefix: str = _prefix

    # -- inspection --------------------------------------------------------

    def _filter(self, source: dict[PathStr, Any]) -> dict[PathStr, Any]:
        """Return ``source`` filtered by ``self._prefix`` with the prefix stripped."""
        if not self._prefix:
            return dict(source)
        prefix = self._prefix + "."
        cut = len(prefix)
        return {k[cut:]: v for k, v in source.items() if k.startswith(prefix)}

    @property
    def values(self) -> dict[PathStr, int]:
        """Return a defensive copy of ``path -> int`` mapping (filtered by prefix)."""
        return self._filter(self._values)

    @property
    def metadata(self) -> dict[PathStr, Info]:
        """Return a defensive copy of ``path -> info`` mapping (filtered by prefix)."""
        return self._filter(self._metadata)

    @property
    def paths(self) -> list[PathStr]:
        """Sorted list of dotted paths in this snapshot view."""
        return sorted(self.values.keys())

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self) -> Iterator[PathStr]:
        return iter(self.paths)

    def __contains__(self, path: object) -> bool:
        if not isinstance(path, str):
            return False
        return self._absolute(path) in self._values

    # -- access ------------------------------------------------------------

    def _absolute(self, path: str) -> str:
        return f"{self._prefix}.{path}" if self._prefix else path

    def __getattr__(self, name: str) -> Any:
        # Only handle real path-like names; let dunders / private hit the
        # default path so pickle/copy work right.
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(str(e)) from None

    def __getitem__(self, path: PathStr) -> Any:
        if not isinstance(path, str):
            raise TypeError(f"path must be a string, got {type(path).__name__}")
        absolute = self._absolute(path)
        # Exact leaf hit?
        if absolute in self._values:
            return self._values[absolute]
        # Interior subtree hit? Return a filtered Snapshot view.
        prefix_dot = absolute + "."
        if any(p.startswith(prefix_dot) for p in self._values):
            return Snapshot(
                values=self._values,
                metadata=self._metadata,
                _prefix=absolute,
            )
        raise KeyError(f"no snapshot entry at '{path}'")

    # -- diff --------------------------------------------------------------

    def diff(self, other: Snapshot) -> SnapshotDiff:
        """Return what changed between ``self`` and ``other``.

        The result is keyed from ``other -> self``: ``before`` is ``other`` and
        ``after`` is ``self``, matching the §15 example
        ``snap2.diff(snap1)``.
        """
        before = other.values
        after = self.values

        before_paths = set(before)
        after_paths = set(after)

        added = {p: after[p] for p in sorted(after_paths - before_paths)}
        removed = {p: before[p] for p in sorted(before_paths - after_paths)}
        changed: dict[PathStr, tuple[int, int]] = {}
        for p in sorted(after_paths & before_paths):
            if before[p] != after[p]:
                changed[p] = (before[p], after[p])

        return SnapshotDiff(changed=changed, added=added, removed=removed)

    # -- (de)serialization -------------------------------------------------

    def _info_to_dict(self, info: Info) -> dict[str, Any]:
        """Best-effort conversion of an info-like object to a JSON dict."""
        if info is None:
            return {}
        if isinstance(info, dict):
            return {k: v for k, v in info.items() if _is_jsonable(v)}
        out: dict[str, Any] = {}
        for attr in ("name", "path", "address", "offset", "regwidth", "access",
                     "reset", "on_read", "on_write"):
            if hasattr(info, attr):
                value = getattr(info, attr)
                if _is_jsonable(value):
                    out[attr] = value
        return out

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for serialization."""
        # Use the prefix-filtered view so subtree snapshots round-trip cleanly.
        values = self.values
        metadata = self.metadata
        return {
            "version": 1,
            "values": {k: int(v) for k, v in values.items()},
            "metadata": {k: self._info_to_dict(metadata.get(k)) for k in values},
        }

    def to_json(self, path: str | None = None, *, indent: int = 2) -> str:
        """Serialize to JSON.

        If ``path`` is given, the JSON is written there and the string is
        also returned for convenience.
        """
        text = json.dumps(self.to_dict(), indent=indent, sort_keys=True)
        if path is not None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return text

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Snapshot:
        version = data.get("version", 1)
        if version != 1:
            raise ValueError(f"unsupported snapshot version: {version!r}")
        values = {str(k): int(v) for k, v in data.get("values", {}).items()}
        # Reconstitute metadata as ``_InfoDict`` so attribute access still works.
        raw_meta = data.get("metadata", {}) or {}
        metadata = {str(k): _InfoDict(v) for k, v in raw_meta.items() if isinstance(v, dict)}
        return cls(values=values, metadata=metadata)

    @classmethod
    def from_json(cls, path: str) -> Snapshot:
        """Load a snapshot previously written by :meth:`to_json`."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    # -- pickle ------------------------------------------------------------

    def __getstate__(self) -> dict[str, Any]:
        # Convert info objects to plain dicts for portability across processes
        # that may not have the original Info type defined.
        meta_dicts = {
            k: self._info_to_dict(v) for k, v in self._metadata.items()
        }
        return {
            "values": dict(self._values),
            "metadata": meta_dicts,
            "prefix": self._prefix,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        self._values = dict(state.get("values", {}))
        self._metadata = {
            k: _InfoDict(v) if isinstance(v, dict) else v
            for k, v in state.get("metadata", {}).items()
        }
        self._prefix = state.get("prefix", "")

    # -- equality / hash ---------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Snapshot):
            return NotImplemented
        return self.values == other.values

    def __hash__(self) -> int:
        # Hash on values only — metadata is descriptive and shouldn't change
        # the identity of a snapshot used as a dict key.
        return hash(tuple(sorted(self.values.items())))

    # -- repr --------------------------------------------------------------

    def __repr__(self) -> str:
        n = len(self)
        prefix = f" prefix={self._prefix!r}" if self._prefix else ""
        return f"<Snapshot {n} entries{prefix}>"

    def __str__(self) -> str:
        lines = [self.__repr__()]
        for p in self.paths:
            lines.append(f"  {p:<40} = {self.values[p]:#x}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# SnapshotDiff
# ---------------------------------------------------------------------------


@dataclass
class SnapshotDiff:
    """Result of :meth:`Snapshot.diff` — what changed between two snapshots.

    Attributes:
        changed: ``path -> (before, after)`` for paths present in both
            snapshots whose values differ.
        added:   ``path -> value`` for paths present only in the *after*
            snapshot.
        removed: ``path -> value`` for paths present only in the *before*
            snapshot.
    """

    changed: dict[PathStr, tuple[int, int]]
    added: dict[PathStr, int]
    removed: dict[PathStr, int]

    @property
    def is_empty(self) -> bool:
        """True if no differences were found."""
        return not (self.changed or self.added or self.removed)

    def __bool__(self) -> bool:
        return not self.is_empty

    def __len__(self) -> int:
        return len(self.changed) + len(self.added) + len(self.removed)

    # -- assertions --------------------------------------------------------

    def assert_only_changed(self, *globs: str) -> None:
        """Raise ``AssertionError`` if any change falls outside ``globs``.

        Each glob is fnmatch-style (``"uart.*"``, ``"ram[*]"``).

        With no globs, asserts that the diff is entirely empty.

        Examples:
            >>> diff = before.diff(after)  # doctest: +SKIP
            >>> diff.assert_only_changed("uart.intr_state.*", "uart.data")
        """
        all_paths: list[PathStr] = sorted(
            set(self.changed) | set(self.added) | set(self.removed)
        )

        if not globs:
            if all_paths:
                raise AssertionError(
                    f"expected no changes; got {len(all_paths)} differences:\n  "
                    + "\n  ".join(all_paths)
                )
            return

        offending: list[PathStr] = []
        for path in all_paths:
            if not any(fnmatch.fnmatchcase(path, g) for g in globs):
                offending.append(path)
        if offending:
            raise AssertionError(
                f"expected only paths matching {list(globs)!r} to change; got {len(offending)} unexpected:\n  "
                + "\n  ".join(offending)
            )

    # -- repr --------------------------------------------------------------

    def __str__(self) -> str:
        n = len(self)
        if n == 0:
            return "0 differences"
        lines = [f"{n} differences"]
        for path in sorted(self.changed):
            before, after = self.changed[path]
            lines.append(f"  {path:<40} {before:#010x} -> {after:#010x}")
        for path in sorted(self.added):
            after = self.added[path]
            lines.append(f"  {path:<40} <added>     -> {after:#010x}")
        for path in sorted(self.removed):
            before = self.removed[path]
            lines.append(f"  {path:<40} {before:#010x} -> <removed>")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"SnapshotDiff(changed={len(self.changed)}, "
            f"added={len(self.added)}, removed={len(self.removed)})"
        )

    # -- jupyter -----------------------------------------------------------

    def _repr_html_(self) -> str:
        """Side-by-side HTML table for Jupyter notebooks."""
        title = (
            f"<div><strong>SnapshotDiff</strong>: {len(self.changed)} changed, "
            f"{len(self.added)} added, {len(self.removed)} removed</div>"
        )
        if self.is_empty:
            return title + "<div><em>(no differences)</em></div>"

        rows: list[str] = [
            "<tr><th>Path</th><th>Before</th><th>After</th><th>Status</th></tr>"
        ]
        for path in sorted(self.changed):
            before, after = self.changed[path]
            rows.append(
                f"<tr><td><code>{html.escape(path)}</code></td>"
                f"<td><code>{before:#010x}</code></td>"
                f"<td><code>{after:#010x}</code></td>"
                f'<td style="color:#b58900">changed</td></tr>'
            )
        for path in sorted(self.added):
            after = self.added[path]
            rows.append(
                f"<tr><td><code>{html.escape(path)}</code></td>"
                f"<td>—</td>"
                f"<td><code>{after:#010x}</code></td>"
                f'<td style="color:#859900">added</td></tr>'
            )
        for path in sorted(self.removed):
            before = self.removed[path]
            rows.append(
                f"<tr><td><code>{html.escape(path)}</code></td>"
                f"<td><code>{before:#010x}</code></td>"
                f"<td>—</td>"
                f'<td style="color:#dc322f">removed</td></tr>'
            )
        return (
            title
            + '<table style="border-collapse:collapse">'
            + "".join(rows)
            + "</table>"
        )


# ---------------------------------------------------------------------------
# soc.snapshot() / soc.restore() implementations
# ---------------------------------------------------------------------------


def _walk_readable(soc: Any) -> Iterable[Any]:
    """Yield readable nodes from a soc-like object.

    Tries (in order): ``soc.iter_readable()`` (preferred when present),
    ``soc.walk()`` filtered to nodes with a ``peek`` or ``read`` method, then
    finally ``soc.walk()`` raw.
    """
    iter_readable = getattr(soc, "iter_readable", None)
    if callable(iter_readable):
        return iter_readable()

    walk = getattr(soc, "walk", None)
    if not callable(walk):
        raise TypeError(
            f"soc {soc!r} does not expose iter_readable() or walk(); "
            "snapshot needs at least one"
        )

    def _filtered() -> Iterator[Any]:
        for node in walk():
            if callable(getattr(node, "peek", None)) or callable(
                getattr(node, "read", None)
            ):
                yield node

    return _filtered()


def _matches_where(path: str, where: str | Callable[[str], bool] | None) -> bool:
    if where is None:
        return True
    if callable(where):
        return bool(where(path))
    return fnmatch.fnmatchcase(path, where)


def take_snapshot(
    soc: Any,
    *,
    allow_destructive: bool = False,
    where: str | Callable[[str], bool] | None = None,
) -> Snapshot:
    """Capture a :class:`Snapshot` of the readable state under ``soc``.

    This is the implementation behind ``soc.snapshot(...)``. The hosted method
    binds ``soc`` automatically; use this directly only if you have a soc
    instance that hasn't been wired through :func:`register_post_create`.
    """
    values: dict[PathStr, int] = {}
    metadata: dict[PathStr, Info] = {}
    for node in _walk_readable(soc):
        info = getattr(node, "info", None)
        path = getattr(info, "path", None)
        if path is None:
            # Skip nodes that have no path — there's nothing to key on.
            continue
        if not _matches_where(path, where):
            continue
        values[path] = _peek_or_read(node, allow_destructive=allow_destructive)
        if info is not None:
            metadata[path] = info
    return Snapshot(values=values, metadata=metadata)


def restore(
    soc: Any,
    snap: Snapshot,
    *,
    dry_run: bool = False,
) -> list[tuple[PathStr, int]]:
    """Write back values from ``snap`` to the hardware behind ``soc``.

    Read-only paths are silently skipped. With ``dry_run=True``, no writes
    are issued; the function returns the list of intended ``(path, value)``
    writes the caller would have done.
    """
    intended: list[tuple[PathStr, int]] = []

    # Build a quick path -> node lookup so we can route writes.
    nodes_by_path: dict[PathStr, Any] = {}
    for node in _walk_readable(soc):
        info = getattr(node, "info", None)
        path = getattr(info, "path", None)
        if path is not None:
            nodes_by_path[path] = node

    for path, value in snap.values.items():
        node = nodes_by_path.get(path)
        if node is None:
            # Path was captured from a different tree shape — skip silently.
            continue
        info = getattr(node, "info", None)
        if info is None or not _can_write(info):
            continue
        intended.append((path, value))
        if not dry_run:
            write = getattr(node, "write", None)
            if callable(write):
                write(int(value))

    return intended


def register_post_create(soc: Any) -> Any:
    """Wire :func:`take_snapshot` and :func:`restore` onto a soc instance.

    Sibling Unit 1 will surface this seam from the generated ``create()``
    factory. Calling it directly is a supported escape hatch for tests and
    for hand-rolled soc objects.

    Returns the same ``soc`` for fluent use.
    """
    def _snapshot(
        self: Any,
        *,
        allow_destructive: bool = False,
        where: str | Callable[[str], bool] | None = None,
    ) -> Snapshot:
        return take_snapshot(self, allow_destructive=allow_destructive, where=where)

    def _restore(
        self: Any,
        snap: Snapshot,
        *,
        dry_run: bool = False,
    ) -> list[tuple[PathStr, int]]:
        return restore(self, snap, dry_run=dry_run)

    # Bind as methods so ``soc.snapshot()`` works without explicit ``self``.
    soc.snapshot = types.MethodType(_snapshot, soc)
    soc.restore = types.MethodType(_restore, soc)
    return soc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _InfoDict:
    """Lightweight attribute-accessible wrapper around a JSON metadata dict.

    Used when reconstituting a snapshot from JSON / pickle: the original
    Info object isn't available, but we still want ``info.access`` to work.
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        self._data: dict[str, Any] = dict(data)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._data:
            return self._data[name]
        # Match on common info attribute names that may legitimately be missing.
        return None

    def __getstate__(self) -> dict[str, Any]:
        return {"_data": dict(self._data)}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self._data = dict(state.get("_data", {}))

    def __repr__(self) -> str:
        return f"_InfoDict({self._data!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _InfoDict):
            return self._data == other._data
        if isinstance(other, dict):
            return self._data == other
        return NotImplemented


def _is_jsonable(value: Any) -> bool:
    """Return True if value is a primitive that ``json.dumps`` accepts."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_jsonable(x) for x in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_jsonable(v) for k, v in value.items())
    return False


