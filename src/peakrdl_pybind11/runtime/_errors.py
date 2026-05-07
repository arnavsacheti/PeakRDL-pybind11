"""Error types used by the bus policy machinery.

This module is a sibling to Unit 2's ``runtime.errors``. It defines the
exact subset Unit 17 needs so the bus policies can be imported, exercised,
and reviewed independently. The umbrella PR is responsible for unifying
this shim with Unit 2's broader error hierarchy; the public names exported
here (``BusError``, ``NotSupportedError``, ``TransportError``,
``TimeoutError``, ``NackError``, ``DisconnectError``) are stable.

Sketch reference: §13.5 (bus error recovery) and §19 (error model).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..masters.base import MasterBase


class NotSupportedError(Exception):
    """Operation is not supported by the selected master or register.

    Raised, for example, when ``cache_for(...)`` is attached to a register
    whose exporter metadata flags ``info.is_volatile`` or ``info.on_read``
    (see sketch §13.4): caching a side-effecting read would silently lie
    to the caller.
    """


class TransportError(Exception):
    """Base class for bus-level transport failures.

    Carries an optional ``kind`` tag — one of ``"timeout"``, ``"nack"``,
    ``"disconnect"`` — so retry policies can match on the failure mode
    without depending on the concrete exception class produced by a
    particular master backend.
    """

    def __init__(self, message: str = "", *, kind: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind


class TimeoutError(TransportError):
    """Bus transaction timed out before completing.

    Note: shadows the builtin ``TimeoutError``. The sketch's `on=("timeout", ...)`
    vocabulary is the source of truth for this naming.
    """

    def __init__(self, message: str = "bus timeout") -> None:
        super().__init__(message, kind="timeout")


class NackError(TransportError):
    """Bus target signalled a negative acknowledgement (NACK)."""

    def __init__(self, message: str = "bus nack") -> None:
        super().__init__(message, kind="nack")


class DisconnectError(TransportError):
    """Master lost its transport-level connection to the target."""

    def __init__(self, message: str = "transport disconnected") -> None:
        super().__init__(message, kind="disconnect")


class BusError(Exception):
    """Transaction failed after the configured retry policy gave up.

    Carries enough context to triage a CI failure without re-instrumenting:

    * ``addr`` — absolute address of the failing transaction.
    * ``op`` — ``"read"`` or ``"write"``.
    * ``master`` — the master object that raised.
    * ``retries`` — number of retries actually performed before giving up.
    * ``underlying`` — the last exception observed from the master.

    Sketch §13.5: "BusError carries the failed transaction, the retry
    count, and the underlying exception".
    """

    def __init__(
        self,
        addr: int,
        op: str,
        master: MasterBase,
        retries: int,
        underlying: BaseException | None,
    ) -> None:
        msg = f"bus {op} @0x{addr:08x} failed after {retries} retr{'y' if retries == 1 else 'ies'}"
        if underlying is not None:
            msg += f": {type(underlying).__name__}: {underlying}"
        super().__init__(msg)
        self.addr = addr
        self.op = op
        self.master = master
        self.retries = retries
        self.underlying = underlying
