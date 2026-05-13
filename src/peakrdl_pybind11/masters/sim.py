"""Behavioural simulator master (sketch §13.7).

:class:`SimMaster` extends :class:`~peakrdl_pybind11.masters.mock.MockMaster`
with a side-effect engine that honours the five RDL behaviours the runtime
currently models:

* ``onread = rclr`` -- pre-read value returned, storage clears.
* ``onread = rset`` -- pre-read value returned, storage sets.
* ``onwrite = woclr`` / ``wclr`` -- per-bit write-1-to-clear.
* ``onwrite = wzc`` -- per-bit write-0-to-clear (inverse of ``woclr``).
* ``onwrite = woset`` / ``wset`` -- per-bit write-1-to-set.
* ``onwrite = wzs`` -- per-bit write-0-to-set.
* ``singlepulse`` -- write through then self-clear so the next read
  observes the field low.

Out of scope for this round (tracked as future work): ``sticky``,
``stickybit``, ``hwclr``, ``hwset``, ``ruser``, ``wuser``,
``onread = ruser``. These tokens are accepted without crashing but the
simulator treats them as pass-through.

Per the project's convention, ``wclr`` and ``wset`` are handled as
per-bit transformations to match the helpers in
:mod:`~peakrdl_pybind11.runtime.side_effects` and the published tests --
strict SystemRDL spells ``wclr`` as "any write clears all bits", which is
intentionally *not* what this simulator emulates. See
:mod:`_side_effect_model` for the bit-level rules.

Construct a :class:`SimMaster` standalone (it behaves identically to
:class:`MockMaster`) or pass ``soc=`` to attach the side-effect models
immediately. ``attach_soc`` can be called later to switch into the
side-effect-aware mode.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ._side_effect_model import (
    RegisterSideEffectModel,
    build_models_for_soc,
)
from .base import AccessOp
from .mock import MockMaster

__all__ = ["SimMaster"]


class SimMaster(MockMaster):
    """In-memory master with optional RDL side-effect simulation.

    Args:
        state: Mapping of ``address -> value`` to seed the in-memory
            store. Copied so the caller can safely mutate the original.
            ``None`` (the default) starts with empty state, which makes
            :class:`SimMaster` a drop-in replacement for
            :class:`MockMaster`.
        soc: When provided, walk the SoC tree at construction time and
            build the per-register side-effect models. Equivalent to
            constructing without ``soc=`` and calling :meth:`attach_soc`
            immediately afterwards.

    Without an attached SoC, every read/write goes straight through to
    the underlying dict (i.e. ``MockMaster`` semantics). With an SoC
    attached, register accesses honour the field-level RDL side effects
    described in the module docstring.
    """

    def __init__(
        self,
        state: Mapping[int, int] | None = None,
        *,
        soc: Any = None,
    ) -> None:
        super().__init__()
        if state:
            self.memory.update(state)
        self._models: dict[int, RegisterSideEffectModel] = {}
        if soc is not None:
            self.attach_soc(soc)

    # ------------------------------------------------------------------
    # Attaching the side-effect map
    # ------------------------------------------------------------------
    def attach_soc(self, soc: Any) -> None:
        """Build (or rebuild) the side-effect map from ``soc``.

        Existing memory contents are preserved. A subsequent call
        replaces the models -- handy for tests that swap in different
        SoC shapes against a single master instance.
        """
        self._models = build_models_for_soc(soc)

    # ------------------------------------------------------------------
    # Per-access overrides
    # ------------------------------------------------------------------
    def read(self, address: int, width: int) -> int:
        # Fast path: no model for this address -> behave like MockMaster.
        model = self._models.get(address)
        if model is None or not model.has_read_effects:
            return super().read(address, width)
        storage = super().read(address, width)
        returned, new_storage = model.apply_read(storage)
        if new_storage != storage:
            super().write(address, new_storage, width)
        return returned

    def write(self, address: int, value: int, width: int) -> None:
        model = self._models.get(address)
        if model is None or not model.has_write_effects:
            super().write(address, value, width)
            return
        storage = super().read(address, width)
        new_storage = model.apply_write(storage, value)
        super().write(address, new_storage, width)

    # ------------------------------------------------------------------
    # Batched variants -- defer to single-op so the transformations run
    # per access. The C++ MockMaster takes the dict-shortcut path here;
    # we explicitly *don't* because every op might tickle a different
    # side-effect model and we want bit-for-bit semantic parity with the
    # single-op path.
    # ------------------------------------------------------------------
    def read_many(self, ops: Sequence[AccessOp]) -> list[int]:
        return [self.read(op.address, op.width) for op in ops]

    def write_many(self, ops: Sequence[AccessOp]) -> None:
        for op in ops:
            self.write(op.address, op.value, op.width)
