from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


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

    Extension points (no-op defaults; sibling units of the API overhaul
    override these in ``runtime/bus_policies.py`` and friends):

    * :meth:`peek` — non-snooping read (defaults to :meth:`read`).
    * :meth:`barrier` — flush in-flight ops (default: no-op).
    * :meth:`on_disconnect` / :attr:`_on_disconnect_callbacks` — callbacks
      fired when transport drops.
    * :meth:`set_retry_policy` / :attr:`_retry_policy` — retry config storage.
    * :meth:`cache_get` / :meth:`cache_set` — read-cache hooks (default:
      pass-through, no caching).
    """

    # State buckets are populated lazily so subclass ``__init__`` doesn't
    # need to call super().__init__(); the existing concrete masters
    # (MockMaster etc.) keep working unchanged.
    _on_disconnect_callbacks: list[Callable[[], None]]
    _retry_policy: dict[str, Any]

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

    # -----------------------------------------------------------------
    # Extension points
    # -----------------------------------------------------------------

    def peek(self, address: int, width: int) -> int:
        """Non-snooping read.

        Default implementation forwards to :meth:`read`. Sibling unit
        ``runtime/bus_policies.py`` overrides this on caching masters to
        avoid promoting cache lines.
        """
        return self.read(address, width)

    def barrier(self) -> None:
        """Flush any in-flight operations.

        Default: no-op. Sibling unit ``runtime/bus_policies.py`` provides
        per-master implementations (e.g. waiting for outstanding write
        completions on JTAG masters). The ``soc.barrier(scope="all")``
        SoC-wide form fans this out across every attached master.
        """
        return None

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        """Register ``callback`` to fire when the transport drops.

        Default: store on a per-instance list. Concrete masters that
        actually have a transport (SSH, OpenOCD, JTAG) override this to
        wire the callback into their disconnect detection.

        Sibling extension point: ``runtime/bus_policies.py``.
        """
        if not hasattr(self, "_on_disconnect_callbacks"):
            self._on_disconnect_callbacks = []
        self._on_disconnect_callbacks.append(callback)

    def set_retry_policy(self, **kwargs: Any) -> None:
        """Configure transient-error retry behaviour.

        Default: stash kwargs on a per-instance dict. Sibling unit
        ``runtime/bus_policies.py`` consumes the dict in its
        retry/backoff wrapper.
        """
        if not hasattr(self, "_retry_policy"):
            self._retry_policy = {}
        self._retry_policy.update(kwargs)

    def cache_get(self, address: int) -> int | None:
        """Look up ``address`` in the master's read cache.

        Default: ``None`` (no cache). Sibling unit
        ``runtime/bus_policies.py`` overrides on caching masters.
        """
        return None

    def cache_set(self, address: int, value: int) -> None:
        """Insert ``(address, value)`` into the master's read cache.

        Default: pass-through (no cache).
        """
        return None
