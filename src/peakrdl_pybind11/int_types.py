"""
Integer subclasses for registers and fields with metadata support.

These classes extend Python's int to include position and width information,
enabling smart read-modify-write operations.
"""


class FieldInt(int):
    """
    Integer subclass representing a field value with position and width metadata.
    
    Attributes:
        lsb: Least significant bit position in the register
        width: Width of the field in bits
        msb: Most significant bit position (lsb + width - 1)
        offset: Register offset address
    """
    
    def __new__(cls, value: int, lsb: int, width: int, offset: int):
        """
        Create a new FieldInt.
        
        Args:
            value: The integer value
            lsb: Least significant bit position
            width: Width in bits
            offset: Register offset address
        """
        instance = super().__new__(cls, value)
        instance._lsb = lsb
        instance._width = width
        instance._offset = offset
        return instance
    
    @property
    def lsb(self) -> int:
        """Least significant bit position."""
        return self._lsb
    
    @property
    def width(self) -> int:
        """Field width in bits."""
        return self._width
    
    @property
    def msb(self) -> int:
        """Most significant bit position."""
        return self._lsb + self._width - 1
    
    @property
    def offset(self) -> int:
        """Register offset address."""
        return self._offset
    
    @property
    def mask(self) -> int:
        """Bit mask for this field."""
        return ((1 << self._width) - 1) << self._lsb
    
    def __repr__(self) -> str:
        """String representation showing value and metadata."""
        return f"FieldInt({int(self):#x}, lsb={self._lsb}, width={self._width}, offset={self._offset:#x})"


class RegisterInt(int):
    """
    Integer subclass representing a register value with position and width metadata.
    
    Attributes:
        offset: Register offset address
        width: Width of the register in bytes
        _fields: Dictionary mapping field names to FieldInt values
    """
    
    def __new__(cls, value: int, offset: int, width: int, fields: dict = None):
        """
        Create a new RegisterInt.
        
        Args:
            value: The integer value
            offset: Register offset address
            width: Width in bytes
            fields: Optional dictionary of field name -> (lsb, width) tuples
        """
        instance = super().__new__(cls, value)
        instance._offset = offset
        instance._width = width
        instance._fields = {}
        
        # Create FieldInt instances for each field
        if fields:
            for field_name, (lsb, field_width) in fields.items():
                # Extract field value from register value
                field_value = (value >> lsb) & ((1 << field_width) - 1)
                instance._fields[field_name] = FieldInt(field_value, lsb, field_width, offset)
        
        return instance
    
    @property
    def offset(self) -> int:
        """Register offset address."""
        return self._offset
    
    @property
    def width(self) -> int:
        """Register width in bytes."""
        return self._width
    
    def __getattr__(self, name: str):
        """
        Access fields as attributes.
        
        Args:
            name: Field name
            
        Returns:
            FieldInt: The field value
            
        Raises:
            AttributeError: If field doesn't exist
        """
        if name.startswith('_'):
            # Avoid recursion for private attributes
            raise AttributeError(f"'RegisterInt' object has no attribute '{name}'")
        
        if name in self._fields:
            return self._fields[name]
        raise AttributeError(f"Register has no field named '{name}'")
    
    def __repr__(self) -> str:
        """String representation showing value and metadata."""
        fields_str = ", ".join(self._fields.keys()) if self._fields else "no fields"
        return f"RegisterInt({int(self):#x}, offset={self._offset:#x}, width={self._width}, fields=[{fields_str}])"
