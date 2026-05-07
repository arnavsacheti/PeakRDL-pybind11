"""Mock master with hooks and side-effect semantics (per IDEAL_API_SKETCH §13.7).

:class:`MockMasterEx` extends :class:`MockMaster` with three primitives a
test author actually needs:

* ``on_read(reg_or_addr, fn)``  — install a callback that synthesizes the
  value returned for a read at the given address.
* ``on_write(reg_or_addr, fn)`` — install a callback that observes (and
  optionally records) writes to the given address.
* ``preload(mem_or_addr, ndarray)`` — bulk-seed a memory region with an
  iterable / numpy array of word values.

In addition, the class understands the two RDL side-effects most tests
need to exercise:

* ``info.on_read == "rclr"``   — after a successful read, the underlying
  storage is cleared, so a second read returns 0.
* ``info.on_write == "woclr"`` — bits set in the written value clear the
  corresponding bits in the stored state (write-1-to-clear).

These semantics are inferred automatically when ``on_read`` / ``on_write``
is called with a register handle whose ``.info`` carries the metadata,
and they can also be requested explicitly via :meth:`mark_rclr` /
:meth:`mark_woclr` when the caller only has a raw integer address.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TypeAlias

from .base import AccessOp
from .mock import MockMaster

ReadHook: TypeAlias = Callable[[int], int]
WriteHook: TypeAlias = Callable[[int, int], None]
# Anything we can extract an address from: a plain int, or a register/memory
# handle that exposes ``info.address`` (or, legacy, ``.address``).
AddressLike: TypeAlias = "int | object"


def _resolve_address(reg_or_addr: AddressLike) -> int:
    """Coerce a register handle or raw int into an absolute address.

    Accepts (in order):
      * a plain :class:`int` (returned as-is);
      * an object with ``info.address`` (preferred — matches the modern
        ``reg.info.address`` surface from §11);
      * an object with ``address`` (covers older / generated handles that
        expose the attribute directly).
    """
    if isinstance(reg_or_addr, int):
        return reg_or_addr
    info = getattr(reg_or_addr, "info", None)
    if info is not None:
        addr = getattr(info, "address", None)
        if isinstance(addr, int):
            return addr
    addr = getattr(reg_or_addr, "address", None)
    if isinstance(addr, int):
        return addr
    raise TypeError(
        f"cannot derive an address from {reg_or_addr!r}; pass an int or "
        f"a register handle exposing .info.address"
    )


def _info_value(info: object, name: str) -> str | None:
    """Return the side-effect tag (``"rclr"``/``"woclr"``) on ``info`` or
    ``None`` if absent.  Tolerant of both string-valued metadata (as in the
    current sketch) and enum-valued metadata (the eventual long-term form
    from §11.2 — ``ReadEffect.RCLR`` etc.).
    """
    if info is None:
        return None
    raw = getattr(info, name, None)
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.lower()
    # enum-like: take the trailing token of the repr/name and lowercase it.
    token = getattr(raw, "name", None) or str(raw).rsplit(".", 1)[-1]
    return token.lower()


class MockMasterEx(MockMaster):
    """A :class:`MockMaster` with read/write hooks and rclr/woclr semantics.

    Existing :class:`MockMaster` semantics (``read`` / ``write`` from the
    backing dict) are preserved as the fallback path — addresses without a
    registered hook still hit the in-memory store.
    """

    def __init__(self) -> None:
        super().__init__()
        self._read_hooks: dict[int, ReadHook] = {}
        self._write_hooks: dict[int, WriteHook] = {}
        self._rclr: set[int] = set()
        self._woclr: set[int] = set()

    # -- hook registration -------------------------------------------------

    def on_read(self, reg_or_addr: AddressLike, fn: ReadHook) -> None:
        """Register a read callback.

        ``fn(address) -> int`` is invoked whenever this address is read;
        the returned value is masked to the access width and returned to
        the caller.  If ``reg_or_addr`` is a register handle and its
        ``info.on_read`` is ``"rclr"``, the address is auto-marked for
        rclr so the underlying value clears after every read.
        """
        addr = _resolve_address(reg_or_addr)
        self._read_hooks[addr] = fn
        if _info_value(getattr(reg_or_addr, "info", None), "on_read") == "rclr":
            self._rclr.add(addr)

    def on_write(self, reg_or_addr: AddressLike, fn: WriteHook) -> None:
        """Register a write callback.

        ``fn(address, value) -> None`` is invoked for every write at this
        address.  The write also propagates to the in-memory store (so a
        subsequent read returns the latest value) unless the address is
        marked woclr, in which case write-1-to-clear semantics apply.
        """
        addr = _resolve_address(reg_or_addr)
        self._write_hooks[addr] = fn
        if _info_value(getattr(reg_or_addr, "info", None), "on_write") == "woclr":
            self._woclr.add(addr)

    def mark_rclr(self, reg_or_addr: AddressLike) -> None:
        """Mark an address (or register) as read-clear-on-read."""
        self._rclr.add(_resolve_address(reg_or_addr))

    def mark_woclr(self, reg_or_addr: AddressLike) -> None:
        """Mark an address (or register) as write-1-to-clear."""
        self._woclr.add(_resolve_address(reg_or_addr))

    # -- bulk preload ------------------------------------------------------

    def preload(
        self,
        mem_or_addr: AddressLike,
        values: Iterable[int],
        *,
        word_size: int = 4,
    ) -> None:
        """Seed a memory region with an iterable / ndarray of word values.

        ``mem_or_addr`` may be a memory handle (``info.address`` is used
        as the base) or a raw int base address.  Each element of ``values``
        is stored at ``base + i * word_size``.  The default ``word_size``
        of 4 matches the typical 32-bit memory layout in generated code;
        override for byte- or 64-bit-addressed memories.
        """
        base = _resolve_address(mem_or_addr)
        # Prefer .tolist() when available (ndarray) — avoids per-element
        # numpy scalar overhead in the dict store.
        tolist = getattr(values, "tolist", None)
        seq: Sequence[int] = tolist() if callable(tolist) else list(values)
        mask = (1 << (word_size * 8)) - 1
        for i, value in enumerate(seq):
            self.memory[base + i * word_size] = int(value) & mask

    # -- core read/write paths --------------------------------------------

    def read(self, address: int, width: int) -> int:
        """Read at ``address``, dispatching to the hook if registered.

        After a successful read, if the address is marked rclr, the
        backing storage is cleared so the next non-hook read sees zero.
        """
        mask = (1 << (width * 8)) - 1
        hook = self._read_hooks.get(address)
        if hook is not None:
            value = int(hook(address)) & mask
        else:
            value = self.memory.get(address, 0) & mask
        if address in self._rclr:
            # rclr: clear storage AND drop the hook — otherwise the hook
            # would resurrect the value and the latch-clear would be
            # invisible to subsequent reads.
            self.memory[address] = 0
            self._read_hooks.pop(address, None)
        return value

    def write(self, address: int, value: int, width: int) -> None:
        """Write ``value`` at ``address``, applying woclr if marked.

        Hooks observe the caller's requested value (not the post-woclr
        storage), so test capture lists reflect intent rather than
        derived state.
        """
        mask = (1 << (width * 8)) - 1
        masked = value & mask
        hook = self._write_hooks.get(address)
        if hook is not None:
            hook(address, masked)
        if address in self._woclr:
            current = self.memory.get(address, 0) & mask
            self.memory[address] = current & ~masked & mask
        else:
            self.memory[address] = masked

    # -- batched paths -----------------------------------------------------
    # Override MockMaster's dict-direct fast paths so hooks and rclr/woclr
    # still apply when callers route through transactions / memory blocks.

    def read_many(self, ops: Sequence[AccessOp]) -> list[int]:
        return [self.read(op.address, op.width) for op in ops]

    def write_many(self, ops: Sequence[AccessOp]) -> None:
        for op in ops:
            self.write(op.address, op.value, op.width)

    # -- housekeeping ------------------------------------------------------

    def reset(self) -> None:
        """Clear stored values. Hook registrations and rclr/woclr marks
        are intentionally preserved so a fixture can be reused across
        test phases without re-arming every hook.
        """
        super().reset()
