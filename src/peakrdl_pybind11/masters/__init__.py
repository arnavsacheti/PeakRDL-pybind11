"""
Master interfaces for register access
"""

from .base import MasterBase
from .callback import CallbackMaster
from .mock import MockMaster
from .openocd import OpenOCDMaster
from .ssh import SSHMaster

__all__ = ["CallbackMaster", "MasterBase", "MockMaster", "OpenOCDMaster", "SSHMaster"]
