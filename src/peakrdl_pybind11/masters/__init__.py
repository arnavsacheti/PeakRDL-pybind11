"""
Master interfaces for register access
"""

from abc import ABC, abstractmethod
from typing import Dict

__all__ = ['MasterBase', 'MockMaster', 'CallbackMaster', 'OpenOCDMaster', 'SSHMaster']

class MasterBase(ABC):
    """
    Base class for Master interfaces
    
    Masters provide the actual communication mechanism for reading/writing registers
    """
    
    @abstractmethod
    def read(self, address: int, width: int) -> int:
        """
        Read a value from the given address
        
        Args:
            address: Absolute address to read from
            width: Width of the register in bytes
            
        Returns:
            Value read from the address
        """
        pass
    
    @abstractmethod
    def write(self, address: int, value: int, width: int) -> None:
        """
        Write a value to the given address
        
        Args:
            address: Absolute address to write to
            value: Value to write
            width: Width of the register in bytes
        """
        pass

class MockMaster(MasterBase):
    """
    Mock Master for testing without hardware
    
    Simulates register storage in memory
    """
    
    def __init__(self):
        self.memory: Dict[int, int] = {}
    
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

class CallbackMaster(MasterBase):
    """
    Callback-based Master
    
    Allows custom read/write functions to be provided
    """
    
    def __init__(self, read_callback=None, write_callback=None):
        """
        Initialize with optional callbacks
        
        Args:
            read_callback: Function(address, width) -> value
            write_callback: Function(address, value, width) -> None
        """
        self.read_callback = read_callback
        self.write_callback = write_callback
    
    def read(self, address: int, width: int) -> int:
        """Read using callback"""
        if self.read_callback is None:
            raise RuntimeError("No read callback configured")
        return self.read_callback(address, width)
    
    def write(self, address: int, value: int, width: int) -> None:
        """Write using callback"""
        if self.write_callback is None:
            raise RuntimeError("No write callback configured")
        self.write_callback(address, value, width)

# Import optional masters
try:
    from .openocd import OpenOCDMaster
except ImportError:
    OpenOCDMaster = None

try:
    from .ssh import SSHMaster
except ImportError:
    SSHMaster = None
