"""Shared Protocol declarations for the duck-typed runtime seams.

Most sibling-unit modules accept register/field/master/mem-shaped objects
without depending on a concrete class hierarchy — the generated runtime
emits per-SoC types that don't share a base. These ``Protocol`` types
give pyrefly enough structural information to typecheck the seams while
keeping the duck-typed flexibility users rely on.

Each Protocol is the **minimum** surface needed by sibling code; users
passing in objects that satisfy more than the protocol is fine, the
narrow shape just lets the static checker prove the call is valid.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReadWritable(Protocol):
    """Minimum register/field shape: ``read()`` returns int-compatible, ``write(value)`` accepts an int."""

    def read(self) -> Any: ...

    def write(self, value: Any) -> Any: ...


@runtime_checkable
class HasInfo(Protocol):
    """Anything that exposes an ``info`` attribute (registers, fields, mems, etc.)."""

    info: Any  # the Info dataclass, but kept loose for sibling stubs


@runtime_checkable
class FieldLike(ReadWritable, HasInfo, Protocol):
    """A generated field — read/write plus optional peek/clear/set/pulse helpers."""


@runtime_checkable
class RegisterLike(ReadWritable, HasInfo, Protocol):
    """A generated register — read/write plus iterable fields via ``info.fields``."""


@runtime_checkable
class MasterLike(Protocol):
    """A bus master — minimum read/write surface plus optional peek/barrier."""

    def read(self, address: int, width: int) -> int: ...

    def write(self, address: int, value: int, width: int) -> None: ...


@runtime_checkable
class MemLike(Protocol):
    """A memory region — index-style read/write."""

    def __getitem__(self, index: Any) -> Any: ...

    def __setitem__(self, index: Any, value: Any) -> None: ...


@runtime_checkable
class HasFields(Protocol):
    """A node that exposes a ``fields`` mapping (register or Info)."""

    fields: Any
