from collections.abc import Callable, Sequence

from .base import AccessOp, MasterBase


class CallbackMaster(MasterBase):
    """
    Callback-based Master

    Allows custom read/write functions to be provided. Optionally accepts
    batched callbacks (``read_many_callback`` / ``write_many_callback``)
    that receive the full list of :class:`AccessOp` in one call,
    amortizing the Python-side dispatch across N ops. If unset,
    :meth:`read_many` / :meth:`write_many` fall back to looping the
    single-op callbacks.
    """

    def __init__(
        self,
        read_callback: Callable[[int, int], int] | None = None,
        write_callback: Callable[[int, int, int], None] | None = None,
        read_many_callback: Callable[[Sequence[AccessOp]], list[int]] | None = None,
        write_many_callback: Callable[[Sequence[AccessOp]], None] | None = None,
    ) -> None:
        """
        Initialize with optional callbacks

        Args:
            read_callback: Function(address, width) -> value
            write_callback: Function(address, value, width) -> None
            read_many_callback: Function(ops) -> list[int] for batched reads.
            write_many_callback: Function(ops) -> None for batched writes.
        """
        self.read_callback = read_callback
        self.write_callback = write_callback
        self.read_many_callback = read_many_callback
        self.write_many_callback = write_many_callback

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

    def read_many(self, ops: Sequence[AccessOp]) -> list[int]:
        """Batched read. Uses ``read_many_callback`` if set, else loops."""
        if self.read_many_callback is not None:
            return list(self.read_many_callback(ops))
        return super().read_many(ops)

    def write_many(self, ops: Sequence[AccessOp]) -> None:
        """Batched write. Uses ``write_many_callback`` if set, else loops."""
        if self.write_many_callback is not None:
            self.write_many_callback(ops)
            return
        super().write_many(ops)
