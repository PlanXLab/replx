import os
import sys
import re
import time
import gc
from typing import Optional, Tuple

from replx.utils import (
    parse_device_banner,
    canon_port,
)
from replx.utils.constants import (
    CTRL_B,
    CTRL_C,
    REPL_PROMPT,
)


class DeviceScanner:

    @staticmethod
    def port_compare_key(port: str | None) -> str:
        normalized = canon_port(port) or ""
        if sys.platform.startswith("win"):
            return normalized.lower()
        return os.path.realpath(normalized) if normalized else ""

    @staticmethod
    def _close_scanner_serial(ser) -> None:
        if not ser:
            return

        try:
            if getattr(ser, 'is_open', False):
                try:
                    ser.cancel_read()
                except Exception:
                    pass
                try:
                    ser.cancel_write()
                except Exception:
                    pass
                try:
                    ser.reset_input_buffer()
                except Exception:
                    pass
                try:
                    ser.reset_output_buffer()
                except Exception:
                    pass
                ser.close()
        except Exception:
            pass
        finally:
            if sys.platform.startswith('win'):
                gc.collect()
                time.sleep(0.05)
    
    # USB-UART bridge vendors (WCH, Silicon Labs, FTDI, Prolific). Their REPL
    # rate is fixed by the firmware; native USB CDC ignores the baud rate.
    _UART_BRIDGE_VIDS = {0x1A86, 0x10C4, 0x0403, 0x067B}

    @staticmethod
    def _probe_port(port_info) -> Optional[Tuple[str, str, str, str]]:
        from replx.transport import repl_baud_candidates

        rates = repl_baud_candidates()
        vid = getattr(port_info, 'vid', None)
        if vid is not None and vid not in DeviceScanner._UART_BRIDGE_VIDS:
            rates = rates[:1]
        for rate in rates:
            info = DeviceScanner.get_board_info_from_banner(port_info.device, baudrate=rate)
            if info:
                return info
        return None

    @staticmethod
    def get_board_info_from_banner(port: str, timeout: float = 2.0,
                                   baudrate: int = 115200) -> Optional[Tuple[str, str, str, str]]:
        try:
            import serial
        except ImportError as e:
            raise RuntimeError("pyserial is required to scan serial ports") from e

        ser = None
        overall_start = time.monotonic()
        is_windows = sys.platform.startswith("win")
        if not is_windows and timeout > 1.5:
            timeout = 1.5
        try:
            serial_timeout = 0.5 if is_windows else 0.3
            ser = serial.Serial(port, baudrate, timeout=serial_timeout, write_timeout=serial_timeout)
            
            for _ in range(3):
                if time.monotonic() - overall_start > timeout:
                    return None
                ser.write(CTRL_C)
                time.sleep(0.05)
            
            ser.reset_input_buffer()
            
            if time.monotonic() - overall_start > timeout:
                return None
            
            ser.write(CTRL_B)
            time.sleep(0.1)
            
            response = b""
            read_timeout = 0.5 if is_windows else 0.3
            read_start = time.monotonic()
            while time.monotonic() - read_start < read_timeout:
                if time.monotonic() - overall_start > timeout:
                    break
                if ser.in_waiting:
                    response += ser.read(ser.in_waiting)
                    if REPL_PROMPT in response:
                        break
                time.sleep(0.01)
            
            response_str = response.decode(errors='ignore')
            
            return parse_device_banner(response_str)
        
        except PermissionError:
            raise
        except (OSError, serial.SerialException):
            pass
        finally:
            DeviceScanner._close_scanner_serial(ser)
        
        return None
    
    @staticmethod
    def scan_serial_ports(max_workers: int = 5, exclude_ports: list = None) -> tuple:
        from serial.tools.list_ports import comports as list_ports_comports
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")

        results = []
        permission_denied = []
        excluded = {
            DeviceScanner.port_compare_key(port)
            for port in (exclude_ports or [])
            if port
        }
        
        all_ports = list(list_ports_comports())
        valid_ports = []
        
        _plat = sys.platform
        
        for port in all_ports:
            if DeviceScanner.port_compare_key(port.device) in excluded:
                continue
            
            if DeviceScanner.is_bluetooth_port(port):
                continue
            
            if _plat == "darwin" or _plat.startswith("linux"):
                if not DeviceScanner.is_likely_micropython_port(port):
                    continue
            
            valid_ports.append(port)
        
        if not valid_ports:
            return results, permission_denied
        
        if _plat == "darwin":
            per_port_timeout = 2.5
            max_workers = min(max_workers, len(valid_ports))
        elif _plat.startswith("linux"):
            per_port_timeout = 2.5
        else:
            per_port_timeout = 3.0
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_port = {
                executor.submit(DeviceScanner._probe_port, port): port.device
                for port in valid_ports
            }
            
            for future in as_completed(future_to_port):
                port_device = future_to_port[future]
                try:
                    board_info = future.result(timeout=per_port_timeout)
                    if board_info:
                        results.append((port_device, board_info))
                except TimeoutError:
                    # Port scan timed out
                    pass
                except PermissionError:
                    # OS denied access to the port (e.g. user not in dialout/uucp group on Linux)
                    permission_denied.append(port_device)
                except (OSError, IOError):
                    # Port access error
                    pass
        
        return results, permission_denied
    
    @staticmethod
    def is_bluetooth_port(port_info) -> bool:
        bt_keywords = ('bluetooth', 'bth', 'rfcomm')
        description = str(getattr(port_info, 'description', '') or '').lower()
        device = str(getattr(port_info, 'device', '') or '').lower()
        
        if any(keyword in description or keyword in device for keyword in bt_keywords):
            return True
        
        if sys.platform == "darwin":
            if "bluetooth" in device:
                return True
            if re.search(r'/dev/(cu|tty)\.(MALS|SOC)', device, re.IGNORECASE):
                return True
        
        return False
    
    @staticmethod
    def is_likely_micropython_port(port_info) -> bool:
        device = str(getattr(port_info, 'device', '') or '').lower()
        description = str(getattr(port_info, 'description', '') or '').lower()
        vid = getattr(port_info, 'vid', None)
        
        if DeviceScanner.is_bluetooth_port(port_info):
            return False

        if vid is not None:
            return True
        
        _plat = sys.platform
        
        if _plat == "darwin":
            device_candidate = device.startswith((
                '/dev/cu.usbmodem',
                '/dev/cu.usbserial',
                '/dev/cu.wchusbserial',
                '/dev/tty.usbmodem',
                '/dev/tty.usbserial',
                '/dev/tty.wchusbserial',
            ))
            
        elif _plat.startswith("linux"):
            device_candidate = (
                device.startswith((
                    '/dev/ttyusb',
                    '/dev/ttyacm',
                    '/dev/ttyama',
                    '/dev/serial/by-id/',
                    '/dev/serial/by-path/',
                ))
            )
        else:
            return True

        description_candidate = any(
            marker in description
            for marker in ('cp210', 'ch340', 'ch341', 'ftdi', 'usb serial', 'usb to uart')
        )
        return device_candidate or description_candidate
    
    @staticmethod
    def is_valid_serial_port(port_name: str) -> bool:
        _plat = sys.platform

        if _plat.startswith("win"):
            return re.fullmatch(r"COM[1-9][0-9]*", port_name, re.IGNORECASE) is not None
        elif _plat.startswith("linux"):
            return (
                re.fullmatch(r"/dev/tty(USB|ACM|AMA)[0-9]+", port_name, re.IGNORECASE) is not None or
                port_name.startswith("/dev/serial/by-id/") or
                port_name.startswith("/dev/serial/by-path/")
            )
        elif _plat == "darwin":
            return (
                re.fullmatch(r"/dev/(tty|cu)\.(usbmodem|usbserial|wchusbserial).+", port_name, re.IGNORECASE) is not None
            )
        return False
