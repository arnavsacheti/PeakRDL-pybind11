"""Tests for the reified transaction API (Unit 18, sketch §13.2).

Pure-Python tests: no cmake, no generated module. The :class:`Read`,
:class:`Write`, :class:`Burst` dataclasses are exercised directly, and
``execute`` / ``batch`` are tested against ``peakrdl_pybind11.masters.mock``
plus a hand-rolled fake SoC that implements just enough of the real
``soc.transaction()`` contract to verify the batching behaviour.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from peakrdl_pybind11.masters.base import AccessOp
from peakrdl_pybind11.masters.mock import MockMaster
from peakrdl_pybind11.runtime import _registry
from peakrdl_pybind11.runtime.transactions import Burst, Read, Write, batch, execute

# ---------------------------------------------------------------------------
# Dataclass surface
# ---------------------------------------------------------------------------


class TestDataclasses:
    """Frozen, slotted dataclasses with sensible defaults."""

    def test_read_construct(self) -> None:
        r = Read(0x1000)
        assert r.addr == 0x1000
        assert r.width == 4

    def test_read_custom_width(self) -> None:
        r = Read(0x1000, width=2)
        assert r.width == 2

    def test_read_is_frozen(self) -> None:
        r = Read(0x1000)
        with pytest.raises(FrozenInstanceError):
            r.addr = 0x2000  # type: ignore[misc]

    def test_write_construct(self) -> None:
        w = Write(0x1004, 0x42)
        assert w.addr == 0x1004
        assert w.value == 0x42
        assert w.width == 4

    def test_write_is_frozen(self) -> None:
        w = Write(0x1004, 0x42)
        with pytest.raises(FrozenInstanceError):
            w.value = 0  # type: ignore[misc]

    def test_burst_read_construct(self) -> None:
        b = Burst(0x2000, count=4, op="read")
        assert b.addr == 0x2000
        assert b.count == 4
        assert b.op == "read"
        assert b.values is None
        assert b.width == 4

    def test_burst_write_construct(self) -> None:
        b = Burst(0x2000, count=4, op="write", values=[1, 2, 3, 4])
        assert b.values == [1, 2, 3, 4]

    def test_burst_is_frozen(self) -> None:
        b = Burst(0x2000, count=4, op="read")
        with pytest.raises(FrozenInstanceError):
            b.count = 8  # type: ignore[misc]

    def test_burst_invalid_op(self) -> None:
        with pytest.raises(ValueError, match="op must be"):
            Burst(0x2000, count=2, op="bogus")  # type: ignore[arg-type]

    def test_burst_read_rejects_values(self) -> None:
        with pytest.raises(ValueError, match="must not specify values"):
            Burst(0x2000, count=2, op="read", values=[1, 2])

    def test_burst_write_requires_values(self) -> None:
        with pytest.raises(ValueError, match="requires values"):
            Burst(0x2000, count=2, op="write")

    def test_burst_write_values_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="does not match count"):
            Burst(0x2000, count=2, op="write", values=[1, 2, 3])

    def test_top_level_imports(self) -> None:
        """``from peakrdl_pybind11 import Read, Write, Burst`` works (sketch §13.2)."""
        import peakrdl_pybind11

        assert peakrdl_pybind11.Read is Read
        assert peakrdl_pybind11.Write is Write
        assert peakrdl_pybind11.Burst is Burst


# ---------------------------------------------------------------------------
# execute()
# ---------------------------------------------------------------------------


class _CountingMaster(MockMaster):
    """MockMaster that counts read_many/write_many invocations.

    Lets the tests assert that bursts collapse into a single batched call
    instead of N single-op calls.
    """

    def __init__(self) -> None:
        super().__init__()
        self.read_many_calls = 0
        self.write_many_calls = 0
        self.read_calls = 0
        self.write_calls = 0

    def read(self, address: int, width: int) -> int:
        self.read_calls += 1
        return super().read(address, width)

    def write(self, address: int, value: int, width: int) -> None:
        self.write_calls += 1
        super().write(address, value, width)

    def read_many(self, ops: Sequence[AccessOp]) -> list[int]:
        self.read_many_calls += 1
        return super().read_many(ops)

    def write_many(self, ops: Sequence[AccessOp]) -> None:
        self.write_many_calls += 1
        super().write_many(ops)


class TestExecute:
    def test_single_read(self) -> None:
        m = _CountingMaster()
        m.memory[0x1000] = 0xDEADBEEF
        result = execute(m, [Read(0x1000)])
        assert result == [0xDEADBEEF]
        assert m.read_calls == 1
        assert m.read_many_calls == 0

    def test_single_write(self) -> None:
        m = _CountingMaster()
        result = execute(m, [Write(0x1000, 0x42)])
        assert result == []  # writes do not contribute to result
        assert m.memory[0x1000] == 0x42
        assert m.write_calls == 1

    def test_write_then_read(self) -> None:
        m = _CountingMaster()
        result = execute(m, [Write(0x1000, 0x42), Read(0x1000)])
        assert result == [0x42]
        assert m.memory[0x1000] == 0x42

    def test_burst_read_uses_read_many(self) -> None:
        m = _CountingMaster()
        for i in range(4):
            m.memory[0x2000 + i * 4] = 0x10 + i
        result = execute(m, [Burst(0x2000, count=4, op="read")])
        assert result == [0x10, 0x11, 0x12, 0x13]
        assert m.read_many_calls == 1
        assert m.read_calls == 0

    def test_burst_write_uses_write_many(self) -> None:
        m = _CountingMaster()
        execute(m, [Burst(0x2000, count=4, op="write", values=[1, 2, 3, 4])])
        for i, v in enumerate([1, 2, 3, 4]):
            assert m.memory[0x2000 + i * 4] == v
        assert m.write_many_calls == 1
        assert m.write_calls == 0

    def test_burst_read_falls_back_when_master_lacks_read_many(self) -> None:
        """Masters without read_many still work — execute loops single ops."""

        class NoBatchMaster:
            def __init__(self) -> None:
                self.read_calls = 0

            def read(self, addr: int, width: int) -> int:
                self.read_calls += 1
                return addr  # echo the address

            def write(self, addr: int, value: int, width: int) -> None:
                pass

        m = NoBatchMaster()
        result = execute(m, [Burst(0x2000, count=3, op="read")])
        assert result == [0x2000, 0x2004, 0x2008]
        assert m.read_calls == 3

    def test_burst_write_falls_back_when_master_lacks_write_many(self) -> None:
        class NoBatchMaster:
            def __init__(self) -> None:
                self.writes: list[tuple[int, int, int]] = []

            def read(self, addr: int, width: int) -> int:
                return 0

            def write(self, addr: int, value: int, width: int) -> None:
                self.writes.append((addr, value, width))

        m = NoBatchMaster()
        execute(m, [Burst(0x2000, count=3, op="write", values=[10, 20, 30])])
        assert m.writes == [
            (0x2000, 10, 4),
            (0x2004, 20, 4),
            (0x2008, 30, 4),
        ]

    def test_burst_zero_count_is_a_noop(self) -> None:
        m = _CountingMaster()
        result = execute(m, [Burst(0x2000, count=0, op="read")])
        assert result == []
        # read_many is still called once with an empty op list; that's fine.

    def test_unsupported_transaction_type(self) -> None:
        m = _CountingMaster()
        with pytest.raises(TypeError, match="unsupported transaction type"):
            execute(m, ["not a transaction"])  # type: ignore[list-item]

    def test_mixed_sequence_preserves_order(self) -> None:
        """Reads, writes, and bursts interleave in submission order."""
        m = _CountingMaster()
        result = execute(
            m,
            [
                Write(0x1000, 0xAA),
                Read(0x1000),
                Burst(0x2000, count=2, op="write", values=[1, 2]),
                Burst(0x2000, count=2, op="read"),
                Write(0x3000, 0xBB),
                Read(0x3000),
            ],
        )
        assert result == [0xAA, 1, 2, 0xBB]

    def test_custom_widths(self) -> None:
        m = _CountingMaster()
        result = execute(m, [Write(0x100, 0xABCD, width=2), Read(0x100, width=2)])
        assert result == [0xABCD]


# ---------------------------------------------------------------------------
# Master extension — execute() attached as a bound method
# ---------------------------------------------------------------------------


class TestMasterExtension:
    def test_extension_attaches_execute_method(self) -> None:
        m = MockMaster()
        _registry.fire_master_extensions(m)
        assert callable(m.execute)
        m.memory[0x1000] = 0x55
        assert m.execute([Read(0x1000)]) == [0x55]

    def test_extension_is_idempotent(self) -> None:
        """Firing the extension twice doesn't double-wrap."""
        m = MockMaster()
        _registry.fire_master_extensions(m)
        first = m.execute
        _registry.fire_master_extensions(m)
        assert m.execute is first  # pre-existing callable is preserved

    def test_extension_silently_skips_immutable_master(self) -> None:
        """A master whose attribute set raises is left untouched (not crashed)."""

        class FrozenMaster:
            __slots__ = ("memory",)

            def __init__(self) -> None:
                self.memory: dict[int, int] = {}

            def read(self, addr: int, width: int) -> int:
                return self.memory.get(addr, 0)

            def write(self, addr: int, value: int, width: int) -> None:
                self.memory[addr] = value

        m = FrozenMaster()
        # Must not raise; just no convenience method gets attached.
        _registry.fire_master_extensions(m)
        assert not hasattr(m, "execute")
        # Module-level execute() still works for the same master.
        m.memory[0x100] = 0x77
        assert execute(m, [Read(0x100)]) == [0x77]


# ---------------------------------------------------------------------------
# batch() context manager
# ---------------------------------------------------------------------------


class _FakeReg:
    """Minimal register stand-in: writes route through the parent SoC."""

    def __init__(self, soc: _FakeSoC, addr: int) -> None:
        self._soc = soc
        self.addr = addr

    def write(self, value: int) -> None:
        self._soc._dispatch_write(self.addr, value)


class _FakeUart:
    def __init__(self, soc: _FakeSoC) -> None:
        self.control = _FakeReg(soc, 0x10)
        self.data = _FakeReg(soc, 0x14)


class _FakeSoC:
    """Hand-rolled SoC fake with a ``transaction()`` cm and a uart subtree.

    Mirrors the real generated SoC's transaction semantics: writes inside
    the cm queue into a buffer and flush in one ``master.write_many`` on
    clean exit; on exception the buffer is discarded.
    """

    def __init__(self) -> None:
        self.master = _CountingMaster()
        self._tx_ops: list[AccessOp] | None = None
        self.uart = _FakeUart(self)

    def _dispatch_write(self, addr: int, value: int, width: int = 4) -> None:
        if self._tx_ops is not None:
            self._tx_ops.append(AccessOp(address=addr, value=value, width=width))
        else:
            self.master.write(addr, value, width)

    @contextmanager
    def transaction(self) -> Any:
        self._tx_ops = []
        try:
            yield self
            ops, self._tx_ops = self._tx_ops, None
            if ops:
                self.master.write_many(ops)
        except BaseException:
            self._tx_ops = None
            raise


class TestBatch:
    def test_batch_flushes_in_one_write_many(self) -> None:
        soc = _FakeSoC()
        with batch(soc) as b:
            b.uart.control.write(1)
            b.uart.data.write(0x55)
            # Nothing has reached the master yet.
            assert soc.master.write_calls == 0
            assert soc.master.write_many_calls == 0
        # Exactly one batched flush for both writes.
        assert soc.master.write_many_calls == 1
        assert soc.master.write_calls == 0
        assert soc.master.memory[0x10] == 1
        assert soc.master.memory[0x14] == 0x55

    def test_batch_yields_soc_itself(self) -> None:
        soc = _FakeSoC()
        with batch(soc) as b:
            assert b is soc

    def test_batch_discards_on_exception(self) -> None:
        soc = _FakeSoC()
        with pytest.raises(RuntimeError):
            with batch(soc) as b:
                b.uart.control.write(0xAA)
                raise RuntimeError("boom")
        # No flush, master untouched.
        assert soc.master.write_many_calls == 0
        assert soc.master.write_calls == 0
        assert 0x10 not in soc.master.memory

    def test_batch_requires_transaction_method(self) -> None:
        class NoTransactionSoC:
            pass

        with pytest.raises(AttributeError, match="requires soc.transaction"):
            with batch(NoTransactionSoC()):
                pass

    def test_post_create_attaches_batch_method(self) -> None:
        soc = _FakeSoC()
        _registry.fire_post_create_hooks(soc)
        assert callable(soc.batch)
        with soc.batch() as b:
            b.uart.control.write(0x77)
        assert soc.master.write_many_calls == 1
        assert soc.master.memory[0x10] == 0x77


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
