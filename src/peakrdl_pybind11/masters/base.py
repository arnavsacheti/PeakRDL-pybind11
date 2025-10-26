from abc import ABC, abstractmethod


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
