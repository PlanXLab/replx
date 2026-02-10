"""
Pack and unpack binary data structures.

Example
-------
```python
    >>> import struct
    >>> 
    >>> # Pack values
    >>> data = struct.pack('<HH', 0x1234, 0x5678)
    >>> 
    >>> # Unpack values
    >>> x, y = struct.unpack('<HH', data)
```
"""

from typing import Any, Union


def pack(fmt: str, *values: Any) -> bytes:
    """
    Pack values according to format string.

    Format characters:
    - b/B: signed/unsigned byte
    - h/H: signed/unsigned short (2 bytes)
    - i/I: signed/unsigned int (4 bytes)
    - l/L: signed/unsigned long (4 bytes)
    - q/Q: signed/unsigned long long (8 bytes)
    - f: float (4 bytes)
    - d: double (8 bytes)
    - s: bytes (preceded by length)

    Byte order:
    - < : little-endian
    - > : big-endian
    - @ : native

    :param fmt: Format string
    :param values: Values to pack

    :returns: Packed bytes

    Example
    -------
    ```python
        >>> import struct
        >>> 
        >>> # Little-endian uint16 + uint16
        >>> data = struct.pack('<HH', 0x1234, 0x5678)
        >>> 
        >>> # Float value
        >>> data = struct.pack('<f', 3.14159)
        >>> 
        >>> # String with length prefix
        >>> data = struct.pack('<H5s', 5, b'Hello')
    ```
    """
    ...


def unpack(fmt: str, buffer: Union[bytes, bytearray]) -> tuple:
    """
    Unpack values from buffer according to format.

    :param fmt: Format string
    :param buffer: Buffer to unpack from

    :returns: Tuple of unpacked values

    Example
    -------
    ```python
        >>> import struct
        >>> 
        >>> data = bytes([0x34, 0x12, 0x78, 0x56])
        >>> x, y = struct.unpack('<HH', data)
        >>> print(hex(x), hex(y))  # 0x1234 0x5678
    ```
    """
    ...


def unpack_from(fmt: str, buffer: Union[bytes, bytearray], offset: int = 0) -> tuple:
    """
    Unpack values from buffer at offset.

    :param fmt: Format string
    :param buffer: Buffer to unpack from
    :param offset: Byte offset to start at

    :returns: Tuple of unpacked values

    Example
    -------
    ```python
        >>> import struct
        >>> 
        >>> data = bytes([0x00, 0x00, 0x34, 0x12])
        >>> value, = struct.unpack_from('<H', data, 2)
        >>> print(hex(value))  # 0x1234
    ```
    """
    ...


def pack_into(fmt: str, buffer: bytearray, offset: int, *values: Any) -> None:
    """
    Pack values into buffer at offset.

    :param fmt: Format string
    :param buffer: Target buffer (must be bytearray)
    :param offset: Byte offset to write at
    :param values: Values to pack

    Example
    -------
    ```python
        >>> import struct
        >>> 
        >>> buf = bytearray(10)
        >>> struct.pack_into('<HH', buf, 2, 0x1234, 0x5678)
    ```
    """
    ...


def calcsize(fmt: str) -> int:
    """
    Calculate size of packed data.

    :param fmt: Format string

    :returns: Size in bytes

    Example
    -------
    ```python
        >>> import struct
        >>> 
        >>> size = struct.calcsize('<HHI')
        >>> print(size)  # 8 (2+2+4)
    ```
    """
    ...
