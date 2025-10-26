from collections.abc import Callable

from .base import MasterBase


class CallbackMaster(MasterBase):
    """
    Callback-based Master

    Allows custom read/write functions to be provided
    """

    def __init__(
        self,
        read_callback: Callable[[int, int], int] | None = None,
        write_callback: Callable[[int, int, int], None] | None = None,
    ) -> None:
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
