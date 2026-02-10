"""
Helpers Package

This package contains domain-separated helper utilities for the CLI.
"""
import sys
from rich.box import ROUNDED, HORIZONTALS

# Panel box style: "rounded" (4-side box) or "horizontals" (top/bottom only)
PANEL_BOX_STYLE = "rounded"  # Options: "rounded", "horizontals"
CONSOLE_WIDTH = 100  # Global console/panel width


def get_panel_box():
    """Get panel box style based on PANEL_BOX_STYLE setting."""
    return HORIZONTALS if PANEL_BOX_STYLE == "horizontals" else ROUNDED


# Global state variables (set by CLI)
import threading
_core = ""
_device = ""
_version = "?"
_device_root_fs = "/"
_device_path = ""
_global_context_lock = threading.Lock()


def set_global_context(core: str, device: str, version: str, device_root_fs: str, device_path: str):
    """Set global context variables used by helper classes."""
    global _core, _device, _version, _device_root_fs, _device_path
    with _global_context_lock:
        _core = core
        _device = device
        _version = version
        _device_root_fs = device_root_fs
        _device_path = device_path


def get_global_context():
    """Get global context variables."""
    with _global_context_lock:
        return _core, _device, _version, _device_root_fs, _device_path


# Import all classes for backward compatibility
from .output import OutputHelper
from .scanner import DeviceScanner, DeviceValidator
from replx.utils import (
    SUPPORT_CORE_DEVICE_TYPES, CORE_ROOT_FS, DEFAULT_ROOT_FS,
    get_root_fs_for_core, parse_device_banner,
)
from .environment import EnvironmentManager
from .store import StoreManager
from .compiler import CompilerHelper
from .registry import InstallHelper, SearchHelper, RegistryHelper
from .updater import UpdateChecker

__all__ = [
    # Constants and utilities
    'PANEL_BOX_STYLE', 'CONSOLE_WIDTH', 'get_panel_box',
    'set_global_context', 'get_global_context',
    # Classes
    'OutputHelper',
    'DeviceScanner', 'DeviceValidator',
    'parse_device_banner', 'get_root_fs_for_core',
    'SUPPORT_CORE_DEVICE_TYPES', 'CORE_ROOT_FS', 'DEFAULT_ROOT_FS',
    'EnvironmentManager',
    'StoreManager',
    'CompilerHelper',
    'InstallHelper', 'SearchHelper', 'RegistryHelper',
    'UpdateChecker',
]
