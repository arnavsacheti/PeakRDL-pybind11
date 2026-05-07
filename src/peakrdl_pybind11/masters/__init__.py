"""
Master interfaces for register access
"""

from .base import AccessOp, MasterBase
from .callback import CallbackMaster
from .mock import MockMaster
from .mock_extensions import MockMasterEx
from .openocd import OpenOCDMaster
from .ssh import SSHMaster

__all__ = [
    "AccessOp",
    "CallbackMaster",
    "MasterBase",
    "MockMaster",
    "MockMasterEx",
    "OpenOCDMaster",
    "SSHMaster",
]
