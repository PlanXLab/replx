import re
from typing import Optional, Tuple

from replx.utils.constants import (
    DEVICE_CHUNK_SIZE_DEFAULT, DEVICE_CHUNK_SIZE_EFR32MG,
    PUT_BATCH_BYTES_DEFAULT, PUT_BATCH_BYTES_EFR32MG,
    RAW_MODE_DELAY_DEFAULT, RAW_MODE_DELAY_EFR32MG,
)


_DEFAULT_PROFILE = {
    'std': True,
    'devices': set(),
    'root_fs': '/',
    'chunk_size': DEVICE_CHUNK_SIZE_DEFAULT,
    'put_batch_bytes': PUT_BATCH_BYTES_DEFAULT,
    'raw_mode_delay': RAW_MODE_DELAY_DEFAULT,
}

CORE_PROFILES = {
    'EFR32MG': {
        'std': False,
        'devices': {'xnode'},
        'root_fs': '/flash',
        'chunk_size': DEVICE_CHUNK_SIZE_EFR32MG,
        'put_batch_bytes': PUT_BATCH_BYTES_EFR32MG,
        'raw_mode_delay': RAW_MODE_DELAY_EFR32MG,
    },
    'RP2350': {
        'devices': {'ticle', 'ticle-lite', 'ticle-sensor', 'ticle-auto'},
    },
    'MIMXRT1062DVJ6A': {
        'devices': {'teensy'},
        'root_fs': '/flash',
    },
    'ESP32C5': {
        'devices': {'ESP32C5'},
    },
    'ESP32S3': {
        'devices': {'ESP32S3'},
    },
    'ESP32P4': {
        'devices': {'ESP32P4'},
    },
}

SUPPORT_CORE_DEVICE_TYPES = {
    k: {'std': v.get('std', _DEFAULT_PROFILE['std']), 'devices': v.get('devices', set())}
    for k, v in CORE_PROFILES.items()
}

CORE_ROOT_FS = {
    k: v.get('root_fs', _DEFAULT_PROFILE['root_fs'])
    for k, v in CORE_PROFILES.items()
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


def get_core_profile(core: str) -> dict:
    normalized = normalize_core(core)
    profile = CORE_PROFILES.get(normalized, {})
    return {**_DEFAULT_PROFILE, **profile}


def get_root_fs_for_core(core: str) -> str:
    return get_core_profile(core)['root_fs']


def is_std_micropython(core: str) -> bool:
    return get_core_profile(core)['std']


def get_devices_for_core(core: str) -> set:
    return get_core_profile(core)['devices']


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