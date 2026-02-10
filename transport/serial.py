from .base import Transport
from replx.utils.exceptions import TransportError
import sys

try:
    import serial
except ImportError:
    raise ImportError("pyserial is required. Install with: pip install pyserial")


class SerialTransport(Transport):
    
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        # Use shorter timeout on Unix (macOS/Linux) for faster failure on invalid ports
        if sys.platform != "win32" and timeout > 0.6:
            timeout = 0.6
        self._default_timeout = timeout
        self._serial = None
        
        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=baudrate,
                timeout=timeout,
                write_timeout=timeout,
                inter_byte_timeout=0.1
            )
            try:
                self._serial.set_buffer_size(rx_size=262144, tx_size=65536)
            except Exception:
                pass
        except serial.SerialException as e:
            raise TransportError(f"Failed to open serial port {port}: {e}") from e
    
    def write(self, data: bytes) -> int:
        try:
            return self._serial.write(data)
        except serial.SerialException as e:
            raise TransportError(f"Serial write error: {e}") from e
    
    def read(self, size: int = 1) -> bytes:
        try:
            return self._serial.read(size)
        except serial.SerialException as e:
            raise TransportError(f"Serial read error: {e}") from e
    
    def read_byte(self, timeout: float = None) -> bytes:
        try:
            if timeout is not None:
                old_timeout = self._serial.timeout
                self._serial.timeout = timeout
                try:
                    return self._serial.read(1)
                finally:
                    self._serial.timeout = old_timeout
            return self._serial.read(1)
        except serial.SerialException as e:
            raise TransportError(f"Serial read_byte error: {e}") from e
    
    def read_available(self) -> bytes:
        try:
            waiting = self._serial.in_waiting
            if waiting > 0:
                return self._serial.read(waiting)
            return b""
        except serial.SerialException as e:
            error_msg = str(e).lower()
            if "clearcommerror" in error_msg or "not exist" in error_msg or "cannot find" in error_msg:
                raise TransportError("Serial port disconnected (device removed or cable unplugged)") from e
            raise TransportError(f"Serial read_available error: {e}") from e
    
    def read_all(self) -> bytes:
        return self.read_available()
    
    def in_waiting(self) -> int:
        try:
            return self._serial.in_waiting
        except serial.SerialException as e:
            error_msg = str(e).lower()
            if "clearcommerror" in error_msg or "not exist" in error_msg or "cannot find" in error_msg or "access is denied" in error_msg:
                raise TransportError("Serial port disconnected (device removed or cable unplugged)") from e
            return 0
        except (OSError, IOError) as e:
            error_msg = str(e).lower()
            if "errno 6" in error_msg or "device not configured" in error_msg or "no such device" in error_msg:
                raise TransportError("Serial port disconnected (device removed or cable unplugged)") from e
            return 0
        except Exception:
            return 0
    
    def close(self) -> None:
        if self._serial:
            try:
                # Cancel any pending read/write operations
                if self._serial.is_open:
                    try:
                        self._serial.cancel_read()
                    except Exception:
                        pass
                    try:
                        self._serial.cancel_write()
                    except Exception:
                        pass
                    try:
                        self._serial.reset_input_buffer()
                    except Exception:
                        pass
                    try:
                        self._serial.reset_output_buffer()
                    except Exception:
                        pass
                    self._serial.close()
            except Exception:
                pass
            finally:
                self._serial = None
    
    def reset_input_buffer(self) -> None:
        if self._serial:
            self._serial.reset_input_buffer()
    
    def reset_output_buffer(self) -> None:
        if self._serial:
            self._serial.reset_output_buffer()
    
    def check_connection(self) -> bool:
        """Check if the serial port is still connected and accessible."""
        try:
            if not self._serial or not self._serial.is_open:
                return False
            # Actually test if the port is still accessible by reading in_waiting
            # This will raise an exception if the device has been disconnected
            _ = self._serial.in_waiting
            return True
        except serial.SerialException as e:
            error_msg = str(e).lower()
            if "clearcommerror" in error_msg or "not exist" in error_msg or "cannot find" in error_msg or "access is denied" in error_msg:
                return False
            return False
        except (OSError, IOError):
            return False
        except Exception:
            return False
    
    def keep_alive(self) -> None:
        """Keep the connection alive by probing the port.
        
        Raises:
            TransportError: If the serial port has been disconnected.
        """
        try:
            _ = self._serial.in_waiting
        except serial.SerialException as e:
            error_msg = str(e).lower()
            if "clearcommerror" in error_msg or "not exist" in error_msg or "cannot find" in error_msg or "access is denied" in error_msg:
                raise TransportError("Serial port disconnected (device removed or cable unplugged)") from e
        except (OSError, IOError) as e:
            error_msg = str(e).lower()
            if "errno 6" in error_msg or "device not configured" in error_msg or "no such device" in error_msg:
                raise TransportError("Serial port disconnected (device removed or cable unplugged)") from e
    
    @property
    def is_open(self) -> bool:
        return self._serial.is_open if self._serial else False
