"""
Helper classes for replx operations.
"""
import os
import sys
import re
import time
import json
import base64
import shutil
import stat
import urllib.request
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime
from urllib.parse import urlparse

import mpy_cross
import serial
import typer
from rich.console import Console
from rich.panel import Panel


# Device support matrix
SUPPORT_CORE_DEVICE_TYPES = {  # core, platform, device
    'EFR32MG': {'zigbee': 'xnode'},
    'ESP32': {'lopy4': 'smartfarm1'},
    'ESP32S3': {},
    'ESP32C6': {},
    'RP2350': {
        'ticle': 'ticle',
        'xconvey': 'xconvey',
        'xhome': 'xhome',
        'autocon': 'autocon'
    },
}

# Global state variables (set by CLI in replx.py)
# These will be imported and set by replx.py main module
_core = ""
_device = ""
_version = 0.0
_device_root_fs = "/"
_device_path = ""
_file_system = None


def set_global_context(core: str, device: str, version: float, device_root_fs: str, device_path: str, file_system):
    """
    Set global context variables used by helper classes.
    Called by replx.py CLI initialization.
    """
    global _core, _device, _version, _device_root_fs, _device_path, _file_system
    _core = core
    _device = device
    _version = version
    _device_root_fs = device_root_fs
    _device_path = device_path
    _file_system = file_system


class DebugHelper:
    """Debug utilities for replx."""
    
    @staticmethod
    def enabled() -> bool:
        """Check if debug mode is enabled."""
        return os.environ.get("REPLXDBG") == "1"
    
    @staticmethod
    def log(msg: str) -> None:
        """Log a debug message."""
        if not DebugHelper.enabled():
            return
        try:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            sys.stderr.write(f"[REPLXDBG {ts}] {msg}\n")
            sys.stderr.flush()
        except Exception:
            pass


class OutputHelper:
    """Output formatting and display utilities."""
    
    _console = Console()
    PANEL_WIDTH = 80  # Consistent width for all progress/result panels
    
    @staticmethod
    def print_panel(content: str, title: str = "", border_style: str = "blue"):
        """Print content in a rich panel box."""
        OutputHelper._console.print(Panel(content, title=title, title_align="left", border_style=border_style, expand=True, width=OutputHelper.PANEL_WIDTH))
    
    @staticmethod
    def create_progress_panel(current: int, total: int, title: str = "Progress", message: str = ""):
        """Create a progress panel for live updates with consistent width."""
        pct = 0 if total == 0 else min(1.0, current / total)
        bar_length = 40
        block = min(bar_length, int(round(bar_length * pct)))
        bar = "█" * block + "░" * (bar_length - block)
        percent = int(pct * 100)
        
        content_lines = []
        if message:
            content_lines.append(message)
        content_lines.append(f"[{bar}] {percent}% ({current}/{total})")
        
        return Panel("\n".join(content_lines), title=title, border_style="green", expand=True, width=OutputHelper.PANEL_WIDTH)
    
    @staticmethod
    def create_spinner_panel(message: str, title: str = "Processing", spinner_frames: list = None, frame_idx: int = 0):
        """Create a spinner panel for indeterminate progress."""
        if spinner_frames is None:
            spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        
        spinner = spinner_frames[frame_idx % len(spinner_frames)]
        content = f"{spinner}  {message}"
        return Panel(content, title=title, border_style="yellow", expand=True, width=OutputHelper.PANEL_WIDTH)
    
    @staticmethod
    def print_progress_bar(current: int, total: int, bar_length: int = 40):
        """Print a progress bar to stdout."""
        pct = 0 if total == 0 else min(1.0, current / total)
        block = min(bar_length, int(round(bar_length * pct)))
        bar = "#" * block + "-" * (bar_length - block)
        percent = int(pct * 100)
        print(f"\r[{bar}] {percent}% ({current}/{total})", end="", flush=True)
    
    @staticmethod
    def format_error_output(out, local_file):
        """Process the error output from the device and print it in a readable format."""
        _error_header = b"Traceback (most recent call last):"
        
        OutputHelper._console.print(f"\r[dim]{'-'*40}Traceback{'-'*40}[/dim]")
        for l in out[1:-2]:
            if "<stdin>" in l:
                full_path = os.path.abspath(os.path.join(os.getcwd(), local_file))
                l = l.replace("<stdin>", full_path, 1)
            print(l.strip())
            
        try:
            err_line_raw = out[-2].strip()
            
            if "<stdin>" in err_line_raw:
                full_path = os.path.abspath(os.path.join(os.getcwd(), local_file))
                err_line = err_line_raw.replace("<stdin>", full_path, 1)
            else:
                match = re.search(r'File "([^"]+)"', err_line_raw)
                if match:
                    device_src_path = os.path.join(_device_path, "src")
                    full_path = os.path.join(device_src_path, match.group(1))
                    escaped_filename = re.sub(r"([\\\\])", r"\\\1", full_path)
                    err_line = re.sub(r'File "([^"]+)"', rf'File "{escaped_filename}"', err_line_raw)
                else:
                    full_path = os.path.abspath(os.path.join(os.getcwd(), local_file))
                    err_line = err_line_raw
                    
            print(f" {err_line}")
            
            err_content = out[-1].strip()

            match = re.search(r"line (\d+)", err_line)
            if match:
                line = int(match.group(1))
                try:
                    with open(full_path, "r") as f:
                        lines = f.readlines()
                        print(f"  {lines[line - 1].rstrip()}")
                except:
                    pass

        except IndexError:
            err_content = out[-1].strip()
        
        OutputHelper._console.print(f"[bright_magenta]{err_content}[/bright_magenta]")


class DeviceScanner:
    """Device detection and scanning utilities."""
    
    @staticmethod
    def get_board_info(port: str, is_long: bool = False) -> Optional[Tuple[str, str, str, str]]:
        """Get the firmware version, build date, core name, and device name of the connected device."""
        try:
            with serial.Serial(port, 115200, timeout=1) as ser:
                ser.write(b'\r\x03')
                time.sleep(0.1)
                ser.reset_input_buffer()

                ser.write(b'\r\x02')
                time.sleep(0.1)

                response = ser.read_all().decode(errors='ignore').strip()
                if response:
                    m = re.search(r"(?:MicroPython|Pycom MicroPython)\s+(.*)", response)
                    if m:
                        response = m.group(1)

                    if is_long:
                        return response

                    rx = re.compile(
                        r"(?P<full_version>[^\s\[,]+),?"
                        r"(?:\s*\[[^\]]+\])?"
                        r"\s+on\s+(?P<date>\d{4}-\d{2}-\d{2});\s+"
                        r"(?P<manufacturer>.+?)\s+with\s+(?P<core>\S+)",
                        re.I,
                    )
                    m = rx.search(response)
                    if not m:
                        return None

                    full_version = m.group("full_version").lstrip("v").rstrip(",")
                    date = m.group("date")
                    manufacturer = m.group("manufacturer").strip().lower().split()[-1]
                    core = m.group("core").strip().upper()
                    
                    if len(manufacturer.split()) > 1:
                        manufacturer = manufacturer.split()[-1]
                        
                    if manufacturer.startswith('pico2'):
                        manufacturer = manufacturer[:-1]
                    
                    num_match = re.match(r"(\d+\.\d+)", full_version)
                    pico_match = re.match(r"pico2_w_(\d{4})_(\d{2})_(\d{2})", full_version, re.I)

                    if num_match:
                        version = num_match.group(1)
                    elif pico_match:
                        y, mth, d = pico_match.groups()

                        if int(y) >= 2025:
                            version = 1.25
                        else:
                            version = 1.24
                    else:
                        version = 0.0
                        
                    device_list = SUPPORT_CORE_DEVICE_TYPES.get(core, None)
                    if device_list:
                        device = device_list.get(manufacturer, core)
                    else:
                        device = core

                    return version, date, core, device
        except (OSError, serial.SerialException):
            pass
        
        return None
    
    @staticmethod
    def is_bluetooth_port(port_info) -> bool:
        """Check if the given port_info is a Bluetooth port."""
        bt_keywords = ['bluetooth', 'bth', 'devb', 'rfcomm', 'blue', 'bt']
        description = port_info.description.lower()
        device = port_info.device.lower()
        return any(keyword in description or keyword in device for keyword in bt_keywords)
    
    @staticmethod
    def is_valid_serial_port(port_name: str) -> bool:
        """Check if the port_name is valid on the current platform."""
        _plat = sys.platform

        if _plat.startswith("win"):
            return re.fullmatch(r"COM[1-9][0-9]*", port_name, re.IGNORECASE) is not None
        elif _plat.startswith("linux"):
            return (
                re.fullmatch(r"/dev/tty(USB|ACM|AMA)[0-9]+", port_name) is not None or
                port_name.startswith("/dev/serial/by-id/")
            )
        elif _plat == "darwin":
            return re.fullmatch(r"/dev/(tty|cu)\..+", port_name) is not None
        return False


class DeviceValidator:
    """Device and core validation utilities."""
    
    @staticmethod
    def find_core_by_device(device_name: str) -> Optional[str]:
        """Find core type by device name."""
        for core, mapping in SUPPORT_CORE_DEVICE_TYPES.items():
            if device_name in mapping.values():
                return core
        return None
    
    @staticmethod
    def is_supported_core(core: str) -> bool:
        """Check if core type is supported."""
        return core in SUPPORT_CORE_DEVICE_TYPES.keys()
    
    @staticmethod
    def is_supported_device(device: str) -> bool:
        """Check if device is supported."""
        leaf_values = [dname for sub in SUPPORT_CORE_DEVICE_TYPES.values() for dname in sub.values()]
        return device in leaf_values


class EnvironmentManager:
    """Environment setup and file management utilities."""
    
    @staticmethod
    def load_env_from_rep():
        """Load environment variables from the .env file in the .vscode directory."""
        current_path = os.getcwd()

        while True:
            min_path = os.path.join(current_path, ".vscode", ".env")
            if os.path.isfile(min_path):
                with open(min_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, value = line.split("=", 1)
                            os.environ[key.strip()] = value.strip()
                return
            
            parent_path = os.path.dirname(current_path)
            if parent_path == current_path:
                return
            current_path = parent_path
    
    @staticmethod
    def copy_tree_or_file(src: str, dst: str) -> None:
        """Copy a file or directory tree."""
        src = os.path.abspath(src)
        dst = os.path.abspath(dst)

        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst, onerror=EnvironmentManager.force_remove_readonly)
            shutil.copytree(src, dst)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    
    @staticmethod
    def link_typehints_into_vscode(src_dir: str, vscode_dir: str) -> int:
        """Link typehints directory into .vscode."""
        if not os.path.isdir(src_dir):
            return 0

        n = 0
        for name in os.listdir(src_dir):
            s = os.path.join(src_dir, name)
            d = os.path.join(vscode_dir, name)
            EnvironmentManager.copy_tree_or_file(s, d)
            n += 1
        return n
    
    @staticmethod
    def force_remove_readonly(func, path, exc_info):
        """Force remove a read-only file or directory."""
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception as e:
            print(f"Deletion failed: {path}, error: {e}")


class StoreManager:
    """Local and remote store management utilities."""
    
    HOME_STORE = Path.home() / ".replx"
    HOME_STAGING = HOME_STORE / ".staging"
    META_NAME = "replx_registry.json"
    
    @staticmethod
    def ensure_home_store():
        """Ensure home store directories exist."""
        StoreManager.HOME_STORE.mkdir(parents=True, exist_ok=True)
        (StoreManager.HOME_STORE / "core").mkdir(exist_ok=True)
        (StoreManager.HOME_STORE / "device").mkdir(exist_ok=True)
        StoreManager.HOME_STAGING.mkdir(parents=True, exist_ok=True)
    
    @staticmethod
    def pkg_root() -> str:
        """Get the package root directory."""
        StoreManager.ensure_home_store()
        return str(StoreManager.HOME_STORE)
    
    @staticmethod
    def local_meta_path() -> str:
        """Get the local metadata file path."""
        return os.path.join(StoreManager.pkg_root(), StoreManager.META_NAME)
    
    @staticmethod
    def gh_headers() -> dict:
        """Get GitHub API headers."""
        hdrs = {"User-Agent": "replx"}
        tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if tok:
            hdrs["Authorization"] = f"Bearer {tok}"
        return hdrs
    
    @staticmethod
    def load_local_meta() -> dict:
        """Load local metadata."""
        p = StoreManager.local_meta_path()
        if not os.path.exists(p):
            return {"targets": {}, "items": {}}
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"targets": {}, "items": {}}
    
    @staticmethod
    def save_local_meta(meta: dict):
        """Save local metadata."""
        p = StoreManager.local_meta_path()
        tmp = p + ".tmp"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    
    @staticmethod
    def load_remote_meta(owner: str, repo: str, ref_: str) -> dict:
        """Load remote metadata from GitHub."""
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{StoreManager.META_NAME}?ref={ref_}"
        req = urllib.request.Request(url, headers=StoreManager.gh_headers())
        with urllib.request.urlopen(req) as r:
            data = json.load(r)
        b64 = (data.get("content") or "").replace("\n", "")
        if not b64:
            raise typer.BadParameter("Remote meta has no content.")
        txt = base64.b64decode(b64.encode("utf-8")).decode("utf-8")
        return json.loads(txt)
    
    @staticmethod
    def refresh_meta_if_online(owner: str, repo: str, ref_: str) -> bool:
        """Refresh metadata from online if available."""
        try:
            remote = StoreManager.load_remote_meta(owner, repo, ref_)
            StoreManager.save_local_meta(remote)
            return True
        except Exception:
            return False


class CompilerHelper:
    """MPY compilation utilities."""
    
    @staticmethod
    def mpy_arch_tag() -> str:
        """Get the MPY architecture tag."""
        return _core or "unknown"
    
    @staticmethod
    def staging_out_for(abs_py: str, base: str, arch_tag: str) -> str:
        """Get the staging output path for a compiled file."""
        rel = os.path.relpath(abs_py, base).replace("\\", "/")
        rel_mpy = os.path.splitext(rel)[0] + ".mpy"
        out_path = StoreManager.HOME_STAGING / arch_tag / rel_mpy
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return str(out_path)
    
    @staticmethod
    def compile_to_staging(abs_py: str, base: str) -> str:
        """Compile a Python file to MPY and stage it."""
        # Verify source file exists
        if not os.path.exists(abs_py):
            raise FileNotFoundError(f"Source file not found: {abs_py}")
        
        args = ['_filepath_', '-o', '_outpath_', '-msmall-int-bits=31']
        if _core == "EFR32MG":
            if _version < 1.19:
                args.append('-mno-unicode')
        elif _core == "ESP32":
            args.append('-march=xtensa')
        elif _core == "ESP32S3":
            args.append('-march=xtensawin')
        elif _core == "RP2350":
            args.append('-march=armv7emsp')
        else:
            raise typer.BadParameter(f"The {_core} is not supported")

        out_mpy = CompilerHelper.staging_out_for(abs_py, base, CompilerHelper.mpy_arch_tag())
        
        # Ensure output directory exists
        out_dir = os.path.dirname(out_mpy)
        os.makedirs(out_dir, exist_ok=True)
        
        args[0] = abs_py
        args[2] = out_mpy
        
        try:
            mpy_cross.run(*args)
        except Exception as e:
            raise RuntimeError(f"MPY compilation failed for {abs_py}: {e}")
        
        # Ensure file is fully written and wait a bit for filesystem
        import time
        for _ in range(10):  # Try for 1 second
            if os.path.exists(out_mpy) and os.path.getsize(out_mpy) > 0:
                return out_mpy
            time.sleep(0.1)
        
        raise FileNotFoundError(f"Compilation failed: {out_mpy} not found or empty")
        
        return out_mpy


class InstallHelper:
    """Installation utilities."""
    
    @staticmethod
    def is_url(s: str) -> bool:
        """Check if string is a valid URL."""
        try:
            u = urlparse(s)
            return u.scheme in ("http", "https") and bool(u.netloc) and bool(u.path)
        except Exception:
            return False
    
    @staticmethod
    def resolve_spec(spec: str) -> Tuple[str, str]:
        """Resolve a spec string to scope and rest."""
        if spec == "core":
            return "core", ""
        if spec == "device":
            return "device", ""
        if spec.startswith("core/"):
            return "core", spec[len("core/"):]
        if spec.startswith("device/"):
            return "device", spec[len("device/"):]
        raise typer.BadParameter(
            f"Invalid spec: {spec} (expect 'core[/...]' or 'device[/...]')"
        )
    
    @staticmethod
    def download_raw_file(owner: str, repo: str, ref_: str, path: str, out_path: str) -> str:
        """Download a raw file from GitHub."""
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref_}/{path}"
        req = urllib.request.Request(url, headers=StoreManager.gh_headers())
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with urllib.request.urlopen(req) as r, open(out_path, "wb") as f:
            f.write(r.read())
        return out_path
    
    @staticmethod
    def ensure_remote_dir(remote_dir: str):
        """Ensure remote directory exists on device."""
        if not remote_dir:
            return
        parts = [p for p in remote_dir.replace("\\", "/").strip("/").split("/") if p]
        path = _device_root_fs
        for p in parts:
            path = path + p + "/"
            _file_system.mkdir(path)
    
    @staticmethod
    def remote_dir_for(scope: str, rel_dir: str) -> str:
        """Get the remote directory for a given scope."""
        if scope == "core":
            return "lib/" + (rel_dir + "/" if rel_dir else "")
        else:
            return f"lib/{_device}/" + (rel_dir + "/" if rel_dir else "")
    
    @staticmethod
    def list_local_py_targets(scope: str, rest: str) -> Tuple[str, list]:
        """List local Python targets for installation."""
        if scope == "core":
            base = os.path.join(StoreManager.pkg_root(), "core", _core, "src")
        else:
            base = os.path.join(StoreManager.pkg_root(), "device", _device, "src")

        target_path = os.path.join(base, rest)
        if os.path.isfile(target_path) and target_path.endswith(".py"):
            rel = os.path.relpath(target_path, base).replace("\\", "/")
            return base, [(target_path, rel)]

        if os.path.isdir(target_path):
            out = []
            for dp, _, fns in os.walk(target_path):
                for fn in fns:
                    if not fn.endswith(".py"):
                        continue
                    ap = os.path.join(dp, fn)
                    rel = os.path.relpath(ap, base).replace("\\", "/")
                    out.append((ap, rel))
            return base, out
        return base, []
    
    @staticmethod
    def local_store_ready_for_full_install(core: str, device: str) -> Tuple[bool, str]:
        """Check if local store is ready for full installation."""
        StoreManager.ensure_home_store()
        meta_path = StoreManager.local_meta_path()
        if not os.path.isfile(meta_path):
            return False, "meta-missing"

        try:
            _ = StoreManager.load_local_meta()
        except Exception:
            return False, "meta-broken"

        req_dirs = [
            os.path.join(StoreManager.pkg_root(), "core", core, "src"),
            os.path.join(StoreManager.pkg_root(), "core", core, "typehints"),
            os.path.join(StoreManager.pkg_root(), "device", device, "src"),
            os.path.join(StoreManager.pkg_root(), "device", device, "typehints"),
        ]
        missing = [p for p in req_dirs if not os.path.isdir(p)]
        if missing:
            return False, "dirs-missing"

        return True, "ok"


class SearchHelper:
    """Registry search utilities."""
    
    @staticmethod
    def fmt_ver_with_star(remote_ver: float, local_ver: float, missing_local: bool) -> str:
        """Format version with star if update available."""
        star = "*" if missing_local or (remote_ver > (local_ver or 0.0)) else ""
        return f"{remote_ver:.1f}{star}"
    
    @staticmethod
    def key_ci(d: dict, name: str) -> Optional[str]:
        """Case-insensitive key lookup."""
        if not isinstance(d, dict) or not name:
            return None
        if name in d:
            return name
        n = name.lower()
        for k in d.keys():
            if isinstance(k, str) and k.lower() == n:
                return k
        return None


class UpdateChecker:
    """Update checking utilities."""
    
    UPDATE_TIMESTAMP_FILE = StoreManager.HOME_STORE / "update_check"
    UPDATE_INTERVAL = int(os.environ.get("REPLX_UPDATE_INTERVAL_SEC", str(60 * 60 * 24)))
    ENV_NO_UPDATE = "REPLX_NO_UPDATE_CHECK"
    
    @staticmethod
    def is_interactive_tty() -> bool:
        """Check if running in interactive TTY."""
        try:
            return sys.stdin.isatty() and sys.stdout.isatty()
        except Exception:
            return False
    
    @staticmethod
    def check_for_updates(current_version: str, *, force: bool = False):
        """Check PyPI for a newer version of replx and prompt the user to upgrade."""
        if os.environ.get(UpdateChecker.ENV_NO_UPDATE, "").strip():
            return
        if not force and not UpdateChecker.is_interactive_tty():
            return
        
        StoreManager.ensure_home_store()
        p = UpdateChecker.UPDATE_TIMESTAMP_FILE
        try:
            should_check = (not p.exists()) or (time.time() - p.stat().st_mtime) >= UpdateChecker.UPDATE_INTERVAL
        except Exception:
            should_check = True
        
        if not should_check:
            return
        
        def _vt(v: str) -> tuple:
            parts = re.findall(r"\d+", str(v))
            return tuple(int(p) for p in parts[:3]) or (0,)

        def _is_newer(latest: str, current: str) -> bool:
            try:
                from packaging.version import Version
                return Version(str(latest)) > Version(str(current))
            except Exception:
                return _vt(latest) > _vt(current)

        try:
            with urllib.request.urlopen("https://pypi.org/pypi/replx/json", timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            latest_version = data["info"]["version"]

            if _is_newer(latest_version, current_version):
                if UpdateChecker.is_interactive_tty():
                    print(f"\n[bright_yellow]New version available: {latest_version}[/bright_yellow]")
                    print(f"Run: [bright_blue]pip install --upgrade replx[/bright_blue]\n")
        except Exception:
            pass
        finally:
            try:
                StoreManager.ensure_home_store()
                UpdateChecker.UPDATE_TIMESTAMP_FILE.touch()
            except Exception:
                pass


class RegistryHelper:
    """Helper class for managing registry metadata operations."""
    
    @staticmethod
    def root_sections(reg: dict):
        """Return (cores_dict, devices_dict) from either new or old layouts."""
        items = reg.get("items") or {}
        cores = items.get("core") or reg.get("cores") or {}
        devices = items.get("device") or reg.get("devices") or {}
        return cores, devices

    @staticmethod
    def get_node(reg: dict, scope: str, target: str) -> dict:
        cores, devices = RegistryHelper.root_sections(reg)
        if scope == "core":
            return cores.get(target, {}) or {}
        else:
            return devices.get(target, {}) or {}

    @staticmethod
    def find_entry(node: dict, name: str):
        """
        Find entry named `name` in node["files"] for list or dict layouts.
        Returns the entry object (dict or empty dict for plain file), or None.
        """
        files = node.get("files")
        if files is None:
            return None
        # Older dict layout
        if isinstance(files, dict):
            return files.get(name)
        # New list layout
        for e in files:
            if isinstance(e, str):
                if e == name:
                    return {}  # plain file (no per-file meta)
            elif isinstance(e, dict) and e.get("name") == name:
                return e
        return None

    @staticmethod
    def walk_files(node: dict, prefix: str = ""):
        """
        Yield (relpath, leaf_meta_dict) for every file under this node.
        Supports list-based and dict-based 'files'.
        """
        files = node.get("files")
        if not files:
            return
        # dict layout (old)
        if isinstance(files, dict):
            for name, meta in files.items():
                if isinstance(meta, dict) and "files" in meta:  # folder
                    yield from RegistryHelper.walk_files(meta, f"{prefix}{name}/")
                else:  # file
                    yield (f"{prefix}{name}", meta if isinstance(meta, dict) else {})
            return
        # list layout (new)
        for entry in files:
            if isinstance(entry, str):  # file
                yield (f"{prefix}{entry}", {})
            elif isinstance(entry, dict):
                nm = entry.get("name")
                if not nm:
                    continue
                if "files" in entry:     # folder
                    yield from RegistryHelper.walk_files(entry, f"{prefix}{nm}/")
                else:                    # file object with optional ver
                    yield (f"{prefix}{nm}", {"ver": entry.get("ver")})

    @staticmethod
    def get_version(d: dict, key_primary="ver", key_fallback="version", default=0.0):
        """Extract version from dict, trying primary then fallback key."""
        v = d.get(key_primary, d.get(key_fallback, default)) if isinstance(d, dict) else default
        try:
            return float(v)
        except Exception:
            return default

    @staticmethod
    def effective_version(reg: dict, scope: str, target: str, part: str, relpath: str) -> float:
        """
        Version resolution (file > nearest folder > part(ver) > node(ver) > 0.0).
        Uses 'ver' if present (falls back to 'version' just in case).
        """
        node = RegistryHelper.get_node(reg, scope, target)
        node_ver = RegistryHelper.get_version(node, default=0.0)

        part_node = (node.get(part) or {})
        part_ver = RegistryHelper.get_version(part_node, default=node_ver)

        nearest = part_ver
        if relpath:
            segs = relpath.split("/")
            dirs, leaf = segs[:-1], segs[-1]

            cur = part_node
            for d in dirs:
                ent = RegistryHelper.find_entry(cur, d)
                if not isinstance(ent, dict):
                    break
                v = RegistryHelper.get_version(ent, default=None)
                if v is not None:
                    nearest = v
                cur = ent

            # file-level override
            leaf_ent = RegistryHelper.find_entry(cur, leaf)
            if isinstance(leaf_ent, dict):
                v = RegistryHelper.get_version(leaf_ent, default=None)
                if v is not None:
                    nearest = v

        return nearest
