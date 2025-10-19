"""Internal intermediate representation used by the PyBind backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional


def _sanitize(path: str) -> str:
    result = path.replace(".", "_").replace("[", "_").replace("]", "")
    return result.replace(" ", "_")


class AccessMode(str, Enum):
    """Software access policy extracted from SystemRDL."""

    RW = "rw"
    RO = "ro"
    WO = "wo"
    W1C = "w1c"
    W0C = "w0c"
    RC = "rc"
    R = "r"
    W = "w"

    @classmethod
    def from_string(cls, value: str) -> "AccessMode":
        value = (value or "rw").lower()
        try:
            return cls(value)
        except ValueError:  # pragma: no cover - defensive fallback
            # Treat unknown policies as read/write.
            return cls.RW

    def to_cpp(self) -> str:
        return {
            AccessMode.RW: "AccessMode::kRW",
            AccessMode.RO: "AccessMode::kRO",
            AccessMode.WO: "AccessMode::kWO",
            AccessMode.W1C: "AccessMode::kW1C",
            AccessMode.W0C: "AccessMode::kW0C",
            AccessMode.RC: "AccessMode::kRC",
            AccessMode.R: "AccessMode::kRO",
            AccessMode.W: "AccessMode::kWO",
        }[self]


@dataclass
class FieldIR:
    name: str
    lsb: int
    msb: int
    access: AccessMode
    reset: Optional[int] = None
    description: Optional[str] = None

    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1


@dataclass
class RegisterIR:
    name: str
    path: str
    address: int
    offset: int
    width: int
    reset: Optional[int]
    access: AccessMode
    is_volatile: bool = False
    description: Optional[str] = None
    fields: List[FieldIR] = field(default_factory=list)

    def to_cpp_name(self) -> str:
        return _sanitize(self.path)


@dataclass
class ArrayIR:
    name: str
    element: "BlockIR"
    count: int
    stride: int


@dataclass
class BlockIR:
    name: str
    path: str
    base_address: int
    registers: List[RegisterIR] = field(default_factory=list)
    blocks: List["BlockIR"] = field(default_factory=list)
    arrays: List[ArrayIR] = field(default_factory=list)
    description: Optional[str] = None

    def to_cpp_name(self) -> str:
        sanitized = _sanitize(self.path)
        return sanitized if sanitized else "top"

    def iter_all_registers(self) -> Iterable[RegisterIR]:
        for reg in self.registers:
            yield reg
        for block in self.blocks:
            yield from block.iter_all_registers()
        for array in self.arrays:
            yield from array.element.iter_all_registers()


@dataclass
class SoCIR:
    module_name: str
    namespace: str
    word_bytes: int
    little_endian: bool
    top: BlockIR
    generate_pyi: bool = False
    include_examples: bool = False
    options: Dict[str, object] = field(default_factory=dict)

    def iter_all_registers(self) -> Iterable[RegisterIR]:
        yield from self.top.iter_all_registers()
