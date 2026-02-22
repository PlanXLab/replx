from .base import Transport
from .serial import SerialTransport


def create_transport(connection_string: str, baudrate: int = 115200, timeout: float = 1.0) -> Transport:
    port = connection_string
    if connection_string.startswith("serial:"):
        port = connection_string[7:]
    
    return SerialTransport(port=port, baudrate=baudrate, timeout=timeout)


__all__ = [
    'Transport',
    'SerialTransport',
    'create_transport',
]
