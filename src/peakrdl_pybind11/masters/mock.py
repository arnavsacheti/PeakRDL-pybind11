from collections.abc import Sequence

from .base import AccessOp, MasterBase


class MockMaster(MasterBase):
    """
    Mock Master for testing without hardware

    Simulates register storage in memory
    """

    def __init__(self) -> None:
        self.memory: dict[int, int] = {}

    def read(self, address: int, width: int) -> int:
        """Read from simulated memory, masked to ``width`` bytes."""
        mask = (1 << (width * 8)) - 1
        return self.memory.get(address, 0) & mask

    def write(self, address: int, value: int, width: int) -> None:
        """Write to simulated memory"""
        mask = (1 << (width * 8)) - 1
        self.memory[address] = value & mask

    def reset(self) -> None:
        """Clear all stored values"""
        self.memory.clear()

    def read_many(self, ops: Sequence[AccessOp]) -> list[int]:
        """Batched read; touches the dict directly without per-op dispatch."""
        out: list[int] = []
        mem = self.memory
        for op in ops:
            mask = (1 << (op.width * 8)) - 1
            out.append(mem.get(op.address, 0) & mask)
        return out

    def write_many(self, ops: Sequence[AccessOp]) -> None:
        """Batched write; touches the dict directly without per-op dispatch."""
        mem = self.memory
        for op in ops:
            mask = (1 << (op.width * 8)) - 1
            mem[op.address] = op.value & mask
