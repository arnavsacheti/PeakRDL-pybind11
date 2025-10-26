from .base import MasterBase


class MockMaster(MasterBase):
    """
    Mock Master for testing without hardware

    Simulates register storage in memory
    """

    def __init__(self) -> None:
        self.memory: dict[int, int] = {}

    def read(self, address: int, width: int) -> int:
        """Read from simulated memory"""
        return self.memory.get(address, 0)

    def write(self, address: int, value: int, width: int) -> None:
        """Write to simulated memory"""
        mask = (1 << (width * 8)) - 1
        self.memory[address] = value & mask

    def reset(self) -> None:
        """Clear all stored values"""
        self.memory.clear()
