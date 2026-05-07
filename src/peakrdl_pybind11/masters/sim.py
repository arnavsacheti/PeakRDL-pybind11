"""Sim master — alpha-quality stub that mimics hardware as a dict.

.. warning::
   Status: **alpha**. The intent of :class:`SimMaster` is to evolve
   into a behavioural simulator that honours RDL side-effects (rclr,
   W1C, single-pulse, …). Today it's a :class:`MockMaster` with one
   convenience: seeding state at construction time. Code that relies
   on side-effect simulation should continue to use the
   mock-with-callbacks pattern from sketch §13.7 until this class is
   upgraded.
"""

from __future__ import annotations

from collections.abc import Mapping

from .mock import MockMaster

__all__ = ["SimMaster"]


class SimMaster(MockMaster):
    """In-memory master pre-seeded with hardware-like state.

    Args:
        state: Mapping of ``address -> value``. Copied into the
            internal store so the caller can safely mutate the
            original. ``None`` (the default) starts with empty state,
            which makes :class:`SimMaster` a drop-in replacement for
            :class:`MockMaster`.
    """

    def __init__(self, state: Mapping[int, int] | None = None) -> None:
        super().__init__()
        if state:
            self.memory.update(state)
