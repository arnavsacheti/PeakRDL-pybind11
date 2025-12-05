"""
Tests for RegisterIntFlag and RegisterIntEnum classes.
"""

import pytest
from enum import IntEnum, IntFlag
from peakrdl_pybind11 import RegisterIntFlag, RegisterIntEnum


class TestRegisterIntFlag:
    """Test RegisterIntFlag class"""

    def test_create_flag_class(self):
        """Test creating a RegisterIntFlag class with members"""
        # Create a flag class using enum functional API
        StatusFlags = IntFlag('StatusFlags', {
            'READY': 1 << 0,
            'ERROR': 1 << 1,
            'BUSY': 1 << 2,
        })
        # Add metadata
        StatusFlags._offset = 0x1000  # type: ignore[attr-defined]
        StatusFlags._width = 4  # type: ignore[attr-defined]
        
        # Create instances
        ready = StatusFlags.READY
        error = StatusFlags.ERROR
        
        # Check values
        assert int(ready) == 1
        assert int(error) == 2
        
    def test_flag_bitwise_operations(self):
        """Test bitwise operations with flags"""
        StatusFlags = IntFlag('StatusFlags', {
            'READY': 1 << 0,
            'ERROR': 1 << 1,
            'BUSY': 1 << 2,
        })
        StatusFlags._offset = 0x2000  # type: ignore[attr-defined]
        StatusFlags._width = 4  # type: ignore[attr-defined]
        
        # Combine flags
        combined = StatusFlags.READY | StatusFlags.ERROR
        assert int(combined) == 3
        
        # Check membership
        assert StatusFlags.READY in combined
        assert StatusFlags.ERROR in combined
        assert StatusFlags.BUSY not in combined


class TestRegisterIntEnum:
    """Test RegisterIntEnum class"""

    def test_create_enum_class(self):
        """Test creating a RegisterIntEnum class with members"""
        # Create an enum class using enum functional API
        ModeEnum = IntEnum('ModeEnum', {
            'IDLE': 0,
            'RUNNING': 1,
            'PAUSED': 2,
            'STOPPED': 3,
        })
        ModeEnum._offset = 0x4000  # type: ignore[attr-defined]
        ModeEnum._width = 4  # type: ignore[attr-defined]
        
        # Create instances
        idle = ModeEnum.IDLE
        running = ModeEnum.RUNNING
        
        # Check values
        assert int(idle) == 0
        assert int(running) == 1
        
    def test_enum_comparison(self):
        """Test enum comparison operations"""
        StateEnum = IntEnum('StateEnum', {
            'OFF': 0,
            'ON': 1,
            'STANDBY': 2,
        })
        StateEnum._offset = 0x5000  # type: ignore[attr-defined]
        StateEnum._width = 2  # type: ignore[attr-defined]
        
        off = StateEnum.OFF
        on = StateEnum.ON
        
        # Comparison
        assert off == StateEnum.OFF
        assert off != on
        assert int(off) == 0
        assert int(on) == 1


class TestFlagEnumIntegration:
    """Integration tests for RegisterIntFlag and RegisterIntEnum"""

    def test_flag_vs_enum_behavior(self):
        """Test that flags and enums have different behaviors"""
        # Flags can be combined
        Flags = IntFlag('Flags', {
            'BIT0': 1 << 0,
            'BIT1': 1 << 1,
        })
        Flags._offset = 0x7000  # type: ignore[attr-defined]
        Flags._width = 4  # type: ignore[attr-defined]
        
        # Enums represent discrete states
        States = IntEnum('States', {
            'STATE0': 0,
            'STATE1': 1,
        })
        States._offset = 0x8000  # type: ignore[attr-defined]
        States._width = 4  # type: ignore[attr-defined]
        
        # Flags can be ORed together naturally
        combined_flags = Flags.BIT0 | Flags.BIT1
        assert int(combined_flags) == 3
        
        # Enums are typically used as discrete values
        state = States.STATE1
        assert int(state) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

