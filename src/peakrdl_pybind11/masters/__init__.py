"""
Master interfaces for register access
"""

from .base import AccessOp, MasterBase
from .callback import CallbackMaster
from .mock import MockMaster
from .openocd import OpenOCDMaster
from .recording_replay import (
    Event,
    RecordingMaster,
    ReplayMaster,
    ReplayMismatchError,
)
from .sim import SimMaster
from .ssh import SSHMaster

__all__ = [
    "AccessOp",
    "CallbackMaster",
    "Event",
    "MasterBase",
    "MockMaster",
    "OpenOCDMaster",
    "RecordingMaster",
    "ReplayMaster",
    "ReplayMismatchError",
    "SSHMaster",
    "SimMaster",
]
