"""
Master interfaces for register access
"""

from .base import AccessOp, MasterBase
from .callback import CallbackMaster
from .mock import MockMaster
from .mock_extensions import MockMasterEx
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
    "MockMasterEx",
    "OpenOCDMaster",
    "RecordingMaster",
    "ReplayMaster",
    "ReplayMismatchError",
    "SSHMaster",
    "SimMaster",
]
