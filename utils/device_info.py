import re
from typing import Optional, Tuple


SUPPORT_CORE_DEVICE_TYPES = {
    'EFR32MG': {'std': False, 'devices': {'xnode'}},  # XBee3 Zigbee (non-standard MicroPython)
    'RP2350': {'std': True, 'devices': {'ticle', 'ticle-lite', 'ticle-sensor', 'ticle-auto'}},  # Pico 2W
    'MIMXRT1062DVJ6A': {'std': True, 'devices': {'teensy'}},  # Teensy 4.0
    # ESP32 family (standard MicroPython)
    'ESP32C5': {'std': True, 'devices': {'ESP32C5'}},
    'ESP32S3': {'std': True, 'devices': {'ESP32S3'}},
    'ESP32P4': {'std': True, 'devices': {'ESP32P4'}},
    'ESP32P4C5': {'std': True, 'devices': {'ESP32P4'}},
    'ESP32P4C6': {'std': True, 'devices': {'ESP32P4'}},
}

CORE_ROOT_FS = {
    'RP2350': '/',
    'EFR32MG': '/flash',
    'MIMXRT1062DVJ6A': '/flash',
    'ESP32C5': '/',
    'ESP32S3': '/',
    'ESP32P4': '/',
    'ESP32P4C5': '/',
    'ESP32P4C6': '/',
}

DEFAULT_ROOT_FS = '/' 

def normalize_core(core: str) -> str:
    if core and "/" in core:
        core = core.split("/", 1)[0]

    if core in ("ESP32P4C5", "ESP32P4C6"):
        return "ESP32P4"

    if core and len(core) > 1 and core[-1].isalpha() and core[-2].isdigit():
        return core[:-1]
    return core


def get_root_fs_for_core(core: str) -> str:
    normalized = normalize_core(core)
    return CORE_ROOT_FS.get(normalized, DEFAULT_ROOT_FS)


def is_std_micropython(core: str) -> bool:
    normalized = normalize_core(core)
    core_info = SUPPORT_CORE_DEVICE_TYPES.get(normalized, {})
    return core_info.get('std', True)


def get_devices_for_core(core: str) -> set:
    normalized = normalize_core(core)
    core_info = SUPPORT_CORE_DEVICE_TYPES.get(normalized, {})
    return core_info.get('devices', set())


def parse_device_banner(banner_text: str) -> Optional[Tuple[str, str, str, str]]:
    version_match = re.search(r'v(\d+\.\d+(?:\.\d+)?)(?:-[\w.]+)?', banner_text)
    version = version_match.group(1) if version_match else '?'
    
    multi_with_match = re.search(r';\s*(.+?)\s+with\s+(.+?)\s+module\s+of\s+external\s+(\w+)\s+with\s+(\w+)', banner_text)
    if multi_with_match:
        prefix = multi_with_match.group(1).strip()
        wifi_desc = multi_with_match.group(2).strip()
        core2 = multi_with_match.group(3).strip().upper()
        core1 = multi_with_match.group(4).strip().upper()

        if core1 == "ESP32P4" and core2 in ("ESP32C5", "ESP32C6"):
            core = f"ESP32P4{core2[-2:]}"
        else:
            core = core1
        
        if prefix.endswith(" module"):
            prefix = prefix[:-7].strip()
        
        manufacturer = f"{prefix} with {wifi_desc} ({core2})".strip()

        device = core
        return version, core, device, manufacturer
    
    match = re.search(r';\s*(.+?)\s+with\s+(\S+)', banner_text)
    if not match:
        return None
    
    prefix = match.group(1).strip()
    core_raw = match.group(2).strip().upper()
    core = normalize_core(core_raw)
    
    device_set = get_devices_for_core(core)
    
    device = None
    manufacturer = None
    
    for known_device in sorted(device_set, key=len, reverse=True):
        prefix_lower = prefix.lower()
        if prefix_lower.endswith(known_device):
            device = known_device
            idx = prefix_lower.rfind(known_device)
            manufacturer = prefix[:idx].strip()
            break
    
    if device is None:
        if len(device_set) == 1:
            device = next(iter(device_set))
            manufacturer = prefix.split()[0] if prefix else "Unknown"
        else:
            device = core
            manufacturer = prefix
    
    if manufacturer:
        if manufacturer.startswith("Raspberry Pi"):
            manufacturer = "Raspberry Pi"
        elif manufacturer.endswith(" module"):
            manufacturer = manufacturer[:-7].strip()
    
    if not manufacturer:
        manufacturer = "Unknown"
    
    return version, core, device, manufacturer