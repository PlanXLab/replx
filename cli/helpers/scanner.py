import sys
import re
import time
from typing import Optional, Tuple

from replx.utils import (
    SUPPORT_CORE_DEVICE_TYPES,
    parse_device_banner,
)
from replx.utils.constants import (
    CTRL_B,
    CTRL_C,
    REPL_PROMPT,
)


class DeviceScanner:
    
    @staticmethod
    def get_board_info_from_banner(port: str, timeout: float = 2.0) -> Optional[Tuple[str, str, str, str]]:
        ser = None
        overall_start = time.time()
        if sys.platform != "win32" and timeout > 1.5:
            timeout = 1.5
        try:
            import serial
            serial_timeout = 0.3 if sys.platform != "win32" else 0.5
            ser = serial.Serial(port, 115200, timeout=serial_timeout, write_timeout=serial_timeout)
            
            for _ in range(3):
                if time.time() - overall_start > timeout:
                    return None
                ser.write(CTRL_C)
                time.sleep(0.05)
            
            ser.reset_input_buffer()
            
            if time.time() - overall_start > timeout:
                return None
            
            ser.write(CTRL_B)
            time.sleep(0.1)
            
            response = b""
            read_timeout = 0.3 if sys.platform != "win32" else 0.5
            read_start = time.time()
            while time.time() - read_start < read_timeout:
                if time.time() - overall_start > timeout:
                    break
                if ser.in_waiting:
                    response += ser.read(ser.in_waiting)
                    if REPL_PROMPT in response:
                        break
                time.sleep(0.01)
            
            response_str = response.decode(errors='ignore')
            
            return parse_device_banner(response_str)
        
        except (OSError, IOError, serial.SerialException):
            pass
        except ImportError:
            pass
        finally:
            if ser:
                try:
                    ser.close()
                except (OSError, IOError):
                    pass
        
        return None
    
    @staticmethod
    def scan_serial_ports(max_workers: int = 5, exclude_ports: list = None) -> list:
        from serial.tools.list_ports import comports as list_ports_comports
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        results = []
        
        excluded = set()
        if exclude_ports:
            for p in exclude_ports:
                if p:
                    excluded.add(p)

        excluded_norm = None
        if sys.platform == "win32":
            excluded_norm = {str(p).lower() for p in excluded if p}
        
        all_ports = list(list_ports_comports())
        valid_ports = []
        
        _plat = sys.platform
        
        for port in all_ports:
            if excluded:
                if port.device in excluded:
                    continue
                if excluded_norm is not None and str(port.device).lower() in excluded_norm:
                    continue
            
            if DeviceScanner.is_bluetooth_port(port):
                continue
            
            if _plat == "darwin" or _plat.startswith("linux"):
                if not DeviceScanner.is_likely_micropython_port(port):
                    continue
            
            valid_ports.append(port)
        
        if not valid_ports:
            return results
        
        if _plat == "darwin":
            per_port_timeout = 2.5
            max_workers = min(max_workers, len(valid_ports))
        elif _plat.startswith("linux"):
            per_port_timeout = 2.5
        else:
            per_port_timeout = 3.0
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_port = {
                executor.submit(DeviceScanner.get_board_info_from_banner, port.device): port.device
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
                except (OSError, IOError):
                    # Port access error
                    pass
        
        return results
    
    @staticmethod
    def is_bluetooth_port(port_info) -> bool:
        bt_keywords = ['bluetooth', 'bth', 'devb', 'rfcomm', 'blue', 'bt']
        description = port_info.description.lower()
        device = port_info.device.lower()
        
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
        device = port_info.device.lower()
        description = port_info.description.lower() if port_info.description else ""
        vid = port_info.vid  # USB Vendor ID
        
        if DeviceScanner.is_bluetooth_port(port_info):
            return False
        
        _plat = sys.platform
        
        if _plat == "darwin":
            if not (device.startswith('/dev/cu.usbmodem') or 
                    device.startswith('/dev/cu.usbserial') or
                    device.startswith('/dev/cu.wchusbserial') or
                    device.startswith('/dev/tty.usbmodem') or
                    device.startswith('/dev/tty.usbserial') or
                    device.startswith('/dev/tty.wchusbserial')):
                if not any(chip in description for chip in ['cp210', 'ch340', 'ch341', 'ftdi', 'usb serial', 'usb to uart']):
                    return False
            
        elif _plat.startswith("linux"):
            if not (device.startswith('/dev/ttyusb') or 
                    device.startswith('/dev/ttyacm') or
                    device.startswith('/dev/ttyama') or
                    device.startswith('/dev/serial/by-id/')):
                return False
        
        micropython_vids = {
            0x2E8A,  # Raspberry Pi (Pico, Pico W)
            0x239A,  # Adafruit
            0x303A,  # Espressif (ESP32)
            0x1A86,  # QinHeng Electronics (CH340/CH341)
            0x10C4,  # Silicon Labs (CP210x)
            0x0403,  # FTDI
            0x1366,  # SEGGER (J-Link, some debug probes)
            0x0D28,  # ARM/DAPLink
        }
        
        if vid is not None and vid in micropython_vids:
            return True
        
        if vid is not None:
            return True
        
        return True
    
    @staticmethod
    def is_valid_serial_port(port_name: str) -> bool:
        _plat = sys.platform

        if _plat.startswith("win"):
            return re.fullmatch(r"COM[1-9][0-9]*", port_name, re.IGNORECASE) is not None
        elif _plat.startswith("linux"):
            return (
                re.fullmatch(r"/dev/tty(USB|ACM|AMA)[0-9]+", port_name, re.IGNORECASE) is not None or
                port_name.startswith("/dev/serial/by-id/")
            )
        elif _plat == "darwin":
            return (
                re.fullmatch(r"/dev/(tty|cu)\.(usbmodem|usbserial|wchusbserial).+", port_name, re.IGNORECASE) is not None
            )
        return False
