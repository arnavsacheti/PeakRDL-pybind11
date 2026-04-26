from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class AccessOp:
    """One register-access operation used by batched ``read_many`` /
    ``write_many``.

    For reads, ``value`` is ignored and conventionally zero; for writes it
    carries the value to write. Mirrors the C++ ``AccessOp`` struct exposed
    by every generated module.
    """

    address: int
    value: int = 0
    width: int = 4


class MasterBase(ABC):
    """
    Base class for Master interfaces

    Masters provide the actual communication mechanism for reading/writing registers.

    .. note::
       For in-memory test/mock fixtures, prefer the C++ ``MockMaster`` and
       ``CallbackMaster`` classes shipped *inside* every generated module
       (e.g. ``my_soc.MockMaster()``). They live entirely in C++, skip the
       pybind11 trampoline, and are noticeably faster on a tight register
       loop than wrapping a Python subclass of ``MasterBase`` via
       ``wrap_master``. Subclass ``MasterBase`` only when the master truly
       has to be implemented in Python (sockets, REST APIs, exotic hardware
       glue) — at which point per-access overhead is dominated by I/O
       anyway.
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

    def read_many(self, ops: Sequence[AccessOp]) -> list[int]:
        """Batched read. Default impl loops single-op :meth:`read`.

        Subclasses can override with a fast path that performs one
        transport round-trip for N ops (e.g. one socket exchange instead
        of N).
        """
        return [self.read(op.address, op.width) for op in ops]

    def write_many(self, ops: Sequence[AccessOp]) -> None:
        """Batched write. Default impl loops single-op :meth:`write`."""
        for op in ops:
            self.write(op.address, op.value, op.width)
