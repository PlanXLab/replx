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
        """Get board information by reading the MicroPython banner.
        
        Args:
            port: Serial port name (e.g., 'COM3', '/dev/ttyUSB0')
            timeout: Maximum time to wait for response (default: 2.0 seconds)
            
        Returns:
            Tuple of (version, core, device, manufacturer) or None if detection fails
        """
        ser = None
        overall_start = time.time()
        # Use shorter timeout on Unix for faster failure with invalid ports
        if sys.platform != "win32" and timeout > 1.5:
            timeout = 1.5
        try:
            import serial
            # Use shorter serial timeout on Unix (macOS takes long to fail on invalid ports)
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
            # Use shorter read timeout on Unix for faster failure
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
    def scan_serial_ports(max_workers: int = 5, exclude_port: str = None, exclude_ports: list = None) -> list:
        """Scan serial ports for MicroPython devices.
        
        Uses platform-specific optimizations:
        - Windows: Standard parallel scan
        - macOS: Pre-filter USB modem/serial ports, skip Bluetooth and virtual ports
        - Linux: Pre-filter ttyUSB/ttyACM devices
        
        Args:
            max_workers: Maximum number of parallel workers (default: 5)
            exclude_port: Single port to exclude from scan
            exclude_ports: List of ports to exclude from scan
            
        Returns:
            List of (port_device, (version, core, device, manufacturer)) tuples
        """
        from serial.tools.list_ports import comports as list_ports_comports
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        results = []
        
        excluded = set()
        if exclude_port:
            excluded.add(exclude_port)
        if exclude_ports:
            for p in exclude_ports:
                if p:
                    excluded.add(p)

        excluded_norm = None
        if sys.platform == "win32":
            excluded_norm = {str(p).lower() for p in excluded if p}
        
        # Collect all ports and apply platform-specific filtering
        all_ports = list(list_ports_comports())
        valid_ports = []
        
        _plat = sys.platform
        
        for port in all_ports:
            # Skip excluded ports
            if excluded:
                if port.device in excluded:
                    continue
                if excluded_norm is not None and str(port.device).lower() in excluded_norm:
                    continue
            
            # Skip Bluetooth ports (all platforms)
            if DeviceScanner.is_bluetooth_port(port):
                continue
            
            # Platform-specific pre-filtering for faster scan
            if _plat == "darwin" or _plat.startswith("linux"):
                # Use faster pre-filter on macOS and Linux
                if not DeviceScanner.is_likely_micropython_port(port):
                    continue
            
            valid_ports.append(port)
        
        if not valid_ports:
            return results
        
        # Adjust timeout based on platform
        # macOS and Linux may need slightly longer timeout for USB enumeration
        if _plat == "darwin":
            per_port_timeout = 2.5
            max_workers = min(max_workers, len(valid_ports))  # Don't over-parallelize on macOS
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
        """Check if port is a Bluetooth port (should be skipped during scan)."""
        bt_keywords = ['bluetooth', 'bth', 'devb', 'rfcomm', 'blue', 'bt']
        description = port_info.description.lower()
        device = port_info.device.lower()
        
        # Basic Bluetooth keyword check
        if any(keyword in description or keyword in device for keyword in bt_keywords):
            return True
        
        # macOS-specific Bluetooth detection
        # Bluetooth serial ports on macOS have patterns like:
        # /dev/cu.Bluetooth-Incoming-Port, /dev/tty.Bluetooth-*, etc.
        if sys.platform == "darwin":
            if "bluetooth" in device:
                return True
            # Skip MALS (Mobile Apple Local Serial) and SOC (System on Chip) pseudo ports
            if re.search(r'/dev/(cu|tty)\.(MALS|SOC)', device, re.IGNORECASE):
                return True
        
        return False
    
    @staticmethod
    def is_likely_micropython_port(port_info) -> bool:
        """Check if port is likely a MicroPython device (fast filter before slow banner check).
        
        This helps reduce scan time by filtering out unlikely ports early.
        """
        device = port_info.device.lower()
        description = port_info.description.lower() if port_info.description else ""
        vid = port_info.vid  # USB Vendor ID
        
        # Skip if it's a Bluetooth port
        if DeviceScanner.is_bluetooth_port(port_info):
            return False
        
        # Platform-specific filters for faster scanning
        _plat = sys.platform
        
        if _plat == "darwin":
            # macOS: Focus on USB serial devices
            # MicroPython boards typically use cu.usbmodem* or cu.usbserial*
            if not (device.startswith('/dev/cu.usbmodem') or 
                    device.startswith('/dev/cu.usbserial') or
                    device.startswith('/dev/cu.wchusbserial') or  # CH340/CH341 chips
                    device.startswith('/dev/tty.usbmodem') or
                    device.startswith('/dev/tty.usbserial') or
                    device.startswith('/dev/tty.wchusbserial')):
                # Also check for common USB serial chip descriptions
                if not any(chip in description for chip in ['cp210', 'ch340', 'ch341', 'ftdi', 'usb serial', 'usb to uart']):
                    return False
            
        elif _plat.startswith("linux"):
            # Linux: Focus on ttyUSB and ttyACM devices
            if not (device.startswith('/dev/ttyusb') or 
                    device.startswith('/dev/ttyacm') or
                    device.startswith('/dev/ttyama') or
                    device.startswith('/dev/serial/by-id/')):
                return False
        
        # Common MicroPython board USB VIDs (if available)
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
        
        # If VID is available and matches known vendors, prioritize
        if vid is not None and vid in micropython_vids:
            return True
        
        # If VID is available but not in known list, still allow (might be new device)
        # But skip if VID indicates definitely not a serial device
        if vid is not None:
            return True
        
        # No VID available - use heuristics
        return True
    
    @staticmethod
    def is_valid_serial_port(port_name: str) -> bool:
        """Check if port name matches valid serial port patterns."""
        _plat = sys.platform

        if _plat.startswith("win"):
            return re.fullmatch(r"COM[1-9][0-9]*", port_name, re.IGNORECASE) is not None
        elif _plat.startswith("linux"):
            return (
                re.fullmatch(r"/dev/tty(USB|ACM|AMA)[0-9]+", port_name, re.IGNORECASE) is not None or
                port_name.startswith("/dev/serial/by-id/")
            )
        elif _plat == "darwin":
            # macOS: Match USB modem and serial ports, exclude Bluetooth
            return (
                re.fullmatch(r"/dev/(tty|cu)\.(usbmodem|usbserial|wchusbserial).+", port_name, re.IGNORECASE) is not None
            )
        return False


class DeviceValidator:
    
    @staticmethod
    def find_core_by_device(device_name: str) -> Optional[str]:
        for core, devices in SUPPORT_CORE_DEVICE_TYPES.items():
            if device_name in devices:
                return core
        return None
    
    @staticmethod
    def is_supported_core(core: str) -> bool:
        return core in SUPPORT_CORE_DEVICE_TYPES
    
    @staticmethod
    def is_supported_device(device: str) -> bool:
        all_devices = set()
        for devices in SUPPORT_CORE_DEVICE_TYPES.values():
            all_devices.update(devices)
        return device in all_devices
