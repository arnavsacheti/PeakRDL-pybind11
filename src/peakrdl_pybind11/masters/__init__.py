"""
Master interfaces for register access
"""

from .base import AccessOp, MasterBase
from .callback import CallbackMaster
from .mock import MockMaster
from .openocd import OpenOCDMaster
from .ssh import SSHMaster

__all__ = [
    "AccessOp",
    "CallbackMaster",
    "MasterBase",
    "MockMaster",
    "OpenOCDMaster",
    "SSHMaster",
]
