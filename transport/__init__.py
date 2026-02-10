from .base import Transport
from .serial import SerialTransport


def create_transport(connection_string: str, baudrate: int = 115200, timeout: float = 1.0) -> Transport:
    """Create a serial transport for the given connection string.
    
    Args:
        connection_string: Serial port name (e.g., 'COM3', '/dev/ttyUSB0', 'serial:COM3')
        baudrate: Serial baud rate (default: 115200)
        timeout: Read timeout in seconds (default: 1.0)
    
    Returns:
        SerialTransport instance
    """
    # Strip "serial:" prefix if present
    port = connection_string
    if connection_string.startswith("serial:"):
        port = connection_string[7:]
    
    return SerialTransport(port=port, baudrate=baudrate, timeout=timeout)


__all__ = [
    'Transport',
    'SerialTransport',
    'create_transport',
]
