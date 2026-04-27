from rich.box import ROUNDED, HORIZONTALS

PANEL_BOX_STYLE = "rounded"
CONSOLE_WIDTH = 100


def get_panel_box():
    return HORIZONTALS if PANEL_BOX_STYLE == "horizontals" else ROUNDED


import threading
_core = ""
_device = ""
_version = "?"
_device_root_fs = "/"
_device_path = ""
_global_context_lock = threading.Lock()


def set_global_context(core: str, device: str, version: str, device_root_fs: str, device_path: str):
    global _core, _device, _version, _device_root_fs, _device_path
    with _global_context_lock:
        _core = core
        _device = device
        _version = version
        _device_root_fs = device_root_fs
        _device_path = device_path


def get_global_context():
    with _global_context_lock:
        return _core, _device, _version, _device_root_fs, _device_path


from .output import OutputHelper
from replx.utils import (
    SUPPORT_CORE_DEVICE_TYPES, CORE_ROOT_FS, DEFAULT_ROOT_FS,
    get_root_fs_for_core, parse_device_banner,
)

__all__ = [
    'PANEL_BOX_STYLE', 'CONSOLE_WIDTH', 'get_panel_box',
    'set_global_context', 'get_global_context',
    'OutputHelper',
    'DeviceScanner',
    'parse_device_banner', 'get_root_fs_for_core',
    'SUPPORT_CORE_DEVICE_TYPES', 'CORE_ROOT_FS', 'DEFAULT_ROOT_FS',
    'EnvironmentManager',
    'StoreManager',
    'CompilerHelper',
    'InstallHelper', 'SearchHelper', 'RegistryHelper',
    'UpdateChecker',
]


_LAZY_ATTRS = {
    'DeviceScanner': ('.scanner', 'DeviceScanner'),
    'EnvironmentManager': ('.environment', 'EnvironmentManager'),
    'StoreManager': ('.store', 'StoreManager'),
    'CompilerHelper': ('.compiler', 'CompilerHelper'),
    'InstallHelper': ('.registry', 'InstallHelper'),
    'SearchHelper': ('.registry', 'SearchHelper'),
    'RegistryHelper': ('.registry', 'RegistryHelper'),
    'UpdateChecker': ('.updater', 'UpdateChecker'),
}


def __getattr__(name):
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value
    return value
