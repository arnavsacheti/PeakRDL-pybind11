"""
Tests for RegisterInt and FieldInt classes.
"""

import pytest
from peakrdl_pybind11 import RegisterInt, FieldInt


class TestFieldInt:
    """Test FieldInt class"""

    def test_create_field_int(self):
        """Test creating a FieldInt"""
        field = FieldInt(0x5, lsb=2, width=3, offset=0x1000)
        
        # Check value
        assert int(field) == 0x5
        assert field == 5
        
        # Check metadata
        assert field.lsb == 2
        assert field.width == 3
        assert field.msb == 4
        assert field.offset == 0x1000
        assert field.mask == 0b11100  # bits 4:2
        
    def test_field_int_comparison(self):
        """Test FieldInt comparison operations"""
        field1 = FieldInt(0x5, lsb=0, width=4, offset=0x1000)
        field2 = FieldInt(0x5, lsb=0, width=4, offset=0x1000)
        field3 = FieldInt(0x3, lsb=0, width=4, offset=0x1000)
        
        # Comparison based on value
        assert field1 == field2
        assert field1 == 5
        assert field1 != field3
        assert field1 > field3
        assert field3 < field1
        
    def test_field_int_arithmetic(self):
        """Test FieldInt arithmetic operations"""
        field = FieldInt(0x5, lsb=0, width=4, offset=0x1000)
        
        # Arithmetic returns regular int, not FieldInt
        result = field + 2
        assert result == 7
        assert isinstance(result, int)
        
        result = field * 2
        assert result == 10
        
    def test_field_int_repr(self):
        """Test FieldInt string representation"""
        field = FieldInt(0xA, lsb=4, width=4, offset=0x2000)
        repr_str = repr(field)
        
        assert "FieldInt" in repr_str
        assert "0xa" in repr_str or "0xA" in repr_str
        assert "lsb=4" in repr_str
        assert "width=4" in repr_str
        

class TestRegisterInt:
    """Test RegisterInt class"""

    def test_create_register_int(self):
        """Test creating a RegisterInt"""
        reg = RegisterInt(0x12345678, offset=0x1000, width=4)
        
        # Check value
        assert int(reg) == 0x12345678
        assert reg == 0x12345678
        
        # Check metadata
        assert reg.offset == 0x1000
        assert reg.width == 4
        
    def test_register_int_with_fields(self):
        """Test RegisterInt with fields"""
        # Create a register with fields
        # Bits 7:0 = data (0x78)
        # Bits 15:8 = status (0x56)
        # Bits 31:16 = control (0x1234)
        reg = RegisterInt(
            0x12345678,
            offset=0x1000,
            width=4,
            fields={
                'data': (0, 8),
                'status': (8, 8),
                'control': (16, 16),
            }
        )
        
        # Check we can access fields
        assert hasattr(reg, '_fields')
        assert 'data' in reg._fields
        assert 'status' in reg._fields
        assert 'control' in reg._fields
        
        # Check field values are extracted correctly
        assert reg.data == 0x78
        assert reg.status == 0x56
        assert reg.control == 0x1234
        
        # Check field metadata
        assert reg.data.lsb == 0
        assert reg.data.width == 8
        assert reg.data.offset == 0x1000
        
        assert reg.status.lsb == 8
        assert reg.status.width == 8
        
        assert reg.control.lsb == 16
        assert reg.control.width == 16
        
    def test_register_int_field_access(self):
        """Test accessing fields via attribute"""
        reg = RegisterInt(
            0xFF,
            offset=0x2000,
            width=4,
            fields={
                'enable': (0, 1),
                'mode': (1, 3),
            }
        )
        
        # Access via attribute
        enable = reg.enable
        mode = reg.mode
        
        assert isinstance(enable, FieldInt)
        assert isinstance(mode, FieldInt)
        
        assert enable == 1  # bit 0 of 0xFF
        assert mode == 7    # bits 3:1 of 0xFF
        
    def test_register_int_no_fields(self):
        """Test RegisterInt without fields"""
        reg = RegisterInt(0x42, offset=0x3000, width=2)
        
        # Should not have any fields
        assert len(reg._fields) == 0
        
        # Accessing non-existent field should raise error
        with pytest.raises(AttributeError):
            _ = reg.nonexistent_field
            
    def test_register_int_comparison(self):
        """Test RegisterInt comparison"""
        reg1 = RegisterInt(0x100, offset=0x1000, width=4)
        reg2 = RegisterInt(0x100, offset=0x1000, width=4)
        reg3 = RegisterInt(0x200, offset=0x1000, width=4)
        
        assert reg1 == reg2
        assert reg1 == 0x100
        assert reg1 != reg3
        assert reg3 > reg1
        
    def test_register_int_repr(self):
        """Test RegisterInt string representation"""
        reg = RegisterInt(
            0x1234,
            offset=0x5000,
            width=2,
            fields={'enable': (0, 1), 'mode': (1, 3)}
        )
        repr_str = repr(reg)
        
        assert "RegisterInt" in repr_str
        assert "0x1234" in repr_str
        assert "0x5000" in repr_str
        assert "enable" in repr_str
        assert "mode" in repr_str


class TestIntegration:
    """Integration tests for RegisterInt and FieldInt"""

    def test_field_mask_calculation(self):
        """Test that field masks are calculated correctly"""
        # Field at bits 3:0 (width=4, lsb=0)
        field1 = FieldInt(0xF, lsb=0, width=4, offset=0)
        assert field1.mask == 0xF
        
        # Field at bits 7:4 (width=4, lsb=4)
        field2 = FieldInt(0x0, lsb=4, width=4, offset=0)
        assert field2.mask == 0xF0
        
        # Field at bits 15:8 (width=8, lsb=8)
        field3 = FieldInt(0x0, lsb=8, width=8, offset=0)
        assert field3.mask == 0xFF00
        
    def test_multibit_field_extraction(self):
        """Test extracting multi-bit field values"""
        # Register value: 0b10110101
        # Field at bits 6:4 should be 0b011 = 3
        reg = RegisterInt(
            0b10110101,
            offset=0,
            width=1,
            fields={'mid_bits': (4, 3)}
        )
        
        assert reg.mid_bits == 0b011
        assert reg.mid_bits == 3
        
    def test_field_from_different_registers(self):
        """Test that fields from different registers are independent"""
        reg1 = RegisterInt(0xFF, offset=0x1000, width=4, fields={'data': (0, 8)})
        reg2 = RegisterInt(0x00, offset=0x2000, width=4, fields={'data': (0, 8)})
        
        assert reg1.data == 0xFF
        assert reg2.data == 0x00
        assert reg1.data.offset != reg2.data.offset


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
