import os
import glob
import time
import threading
import urllib.request
import urllib.error
from urllib.parse import urlparse
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import typer
from rich.console import Console
from rich.panel import Panel
from rich.live import Live

from ..helpers import (
    OutputHelper, StoreManager, InstallHelper, SearchHelper, RegistryHelper,
    CompilerHelper,
    get_panel_box, CONSOLE_WIDTH
)
from replx.utils import device_name_to_path
from ..config import STATE
from ..connection import (
    _ensure_connected, _create_agent_client
)
from ..app import app


def _format_size(b: int) -> str:
    if b < 1024:
        return f"{b}B"
    elif b < 1024 * 1024:
        return f"{b/1024:.1f}KB"
    else:
        return f"{b/(1024*1024):.1f}MB"


def _upload_file_with_progress(client, local_path: str, remote_path: str, progress_callback):
    return client.send_command_streaming(
        'put_from_local_streaming',
        progress_callback=progress_callback,
        local_path=local_path,
        remote_path=remote_path
    )


def _find_local_pkg_version(local_meta: dict, *, scope: str, target: str, source_path: str, pkg_name: str) -> tuple[float, bool]:
    """Resolve local registry version for a package.

    Local registry keys are not always a simple `${scope}:${target}:${name}`:
    - device-specific packages may be stored as `${scope}:${target}:${name}@_variant`
    - some historical entries may have a different `${name}` but the same `source`

    This helper prefers direct-key lookup, then variant-key lookup, then falls back
    to matching by `source` within the target scope.
    """
    if not local_meta or not isinstance(local_meta, dict):
        return 0.0, True

    local_packages = local_meta.get("packages", {})
    if not local_packages or not isinstance(local_packages, dict):
        return 0.0, True

    if not scope or not target or not source_path or not pkg_name:
        return 0.0, True

    base_key = f"{scope}:{target}:{pkg_name}"

    # 1) Exact match
    if base_key in local_packages:
        return RegistryHelper.get_version(local_packages[base_key]), False

    # 2) Variant match (device only)
    if scope == "device":
        best = None
        for key, meta in local_packages.items():
            if not isinstance(key, str):
                continue
            if key.startswith(base_key + "@"):
                v = RegistryHelper.get_version(meta)
                best = v if best is None else max(best, v)
        if best is not None:
            return best, False

    # 3) Fallback: match by source path within the same target scope
    # (handles cases like key name mismatch but source is stable)
    prefix_lower = f"{scope}:{target}:".lower()
    for key, meta in local_packages.items():
        if not isinstance(key, str) or not isinstance(meta, dict):
            continue
        if key.lower().startswith(prefix_lower) and meta.get("source") == source_path:
            return RegistryHelper.get_version(meta), False

    return 0.0, True


@app.command(name="pkg", rich_help_panel="Package Management")
def pkg(
    args: list[str] = typer.Argument(None, help="Package command and arguments"),
    owner: str = typer.Option("PlanXLab", help="GitHub repo owner (for search/download)"),
    repo: str = typer.Option("replx_libs", help="GitHub repo name (for search/download)"),
    ref: str = typer.Option("main", help="Git reference (for search/download)"),
    target: Optional[str] = typer.Option(None, "--target", "-t", help="Board target path for update (e.g., lib/ticle)"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Package management for MicroPython devices.

[bold cyan]Usage:[/bold cyan] replx pkg [yellow]SUBCOMMAND[/yellow] [args]

[bold cyan]Subcommands:[/bold cyan]
  [yellow]search[/yellow] [dim][QUERY][/dim]          Search available libraries. [dim]QUERY[/dim] filters results by module name
  [yellow]download[/yellow]                Download to local store
  [yellow]update[/yellow] [green]TARGET[/green] [dim][OPTION][/dim]  Install to device (.py → .mpy)
  [yellow]clean[/yellow]                   Remove current core/device from local store

[bold cyan]Update Targets:[/bold cyan]
  [green]core/[/green]                   Install core libraries for connected board
  [green]device/[/green]                 Install device libraries for connected board
  [green]file.py[/green]                 Single .py file → /lib/file.mpy
  [green]file.bin[/green]                Other files → /lib/file.bin (as-is)
  [green]*.py[/green] | [green]a.py b.py[/green]        Multiple files → /lib/
  [green]folder[/green]                  Folder → /lib/folder/ (with structure)
  [green]https://...[/green]             Download from URL and install

[bold cyan]Update Options:[/bold cyan]
  [green]--target[/green] [dim]PATH[/dim]           Specify board target path (e.g., lib/ticle, /lib/ticle)
                          [dim]Applies to file/folder/URL targets[/dim]

[bold cyan]Examples:[/bold cyan]
  replx pkg search                               [dim]List all libraries[/dim]
  replx pkg search audio                         [dim]Search by name[/dim]
  replx pkg download                             [dim]Download for connected board[/dim]
  replx pkg update core/                         [dim]Install core libs[/dim]
  replx pkg update slip.py                       [dim]→ /lib/slip.mpy[/dim]
  replx pkg update *.py                          [dim]All .py files → /lib/[/dim]
  replx pkg update upaho                         [dim]→ /lib/upaho/[/dim]
  replx pkg update ws2812 --target lib/ticle     [dim]→ /lib/ticle/ws2812/[/dim]
  replx pkg update https://... --target lib/ext  [dim]→ /lib/ext/[/dim]
  replx pkg clean                                [dim]Remove current core/device[/dim]

[bold cyan]Workflow:[/bold cyan] search → download → update core/

[bold cyan]Note:[/bold cyan]
  • Device connection required (--port or default)
  • Core/device auto-detected from connected board
  • Options --owner/--repo/--ref are for search/download only
  • URL format for GitHub: [green]https://GITHUB/OWNER/REPO/BRANCH/path/file.py[/green]
    [dim]• GITHUB: raw.githubusercontent.com [/dim]
    [dim]• e.g. OWNER: micropython | REPO: micropython-lib | BRANCH: master[/dim]
    [dim]• e.g. path: micropython/umqtt.simple/umqtt | file.py: simple.py[/dim]"""
        OutputHelper.print_panel(help_text, border_style="dim")
        console.print()
        raise typer.Exit()
    
    if not args:
        OutputHelper.print_panel(
            "Specify a command: [bright_blue]search[/bright_blue], [bright_blue]download[/bright_blue], [bright_blue]update[/bright_blue], or [bright_blue]clean[/bright_blue]\n\n"
            "Usage:\n"
            "  [bright_green]replx pkg search[/bright_green]      [dim]# List available libraries[/dim]\n"
            "  [bright_green]replx pkg download[/bright_green]    [dim]# Download to local store[/dim]\n"
            "  [bright_green]replx pkg update[/bright_green]      [dim]# Install to device[/dim]\n"
            "  [bright_green]replx pkg clean[/bright_green]       [dim]# Remove current core/device[/dim]\n\n"
            "Use [bright_blue]replx pkg --help[/bright_blue] for details.",
            title="Package Management",
            border_style="yellow"
        )
        raise typer.Exit(1)
    
    cmd = args[0].lower()
    cmd_args = args[1:] if len(args) > 1 else []
    
    target_path = target.lstrip("/").rstrip("/") if target else None
    
    if cmd == "search":
        _pkg_search(cmd_args, owner, repo, ref)
    elif cmd == "download":
        _pkg_download(cmd_args, owner, repo, ref)
    elif cmd == "update":
        _pkg_update(cmd_args, target_path)
    elif cmd == "clean":
        _pkg_clean(cmd_args)
    else:
        OutputHelper.print_panel(
            f"Unknown command: [red]{cmd}[/red]\n\n"
            "Available commands: [bright_blue]search[/bright_blue], [bright_blue]download[/bright_blue], [bright_blue]update[/bright_blue], [bright_blue]clean[/bright_blue]",
            title="Package Error",
            border_style="red"
        )
        raise typer.Exit(1)


def _pkg_search(args: list[str], owner: str, repo: str, ref: str):
    status = _ensure_connected()
    
    lib_name = args[0] if args else None
    
    try:
        remote = StoreManager.load_remote_meta(owner, repo, ref)
    except Exception as e:
        raise typer.BadParameter(f"Failed to load remote registry: {e}")

    try:
        local = StoreManager.load_local_meta()
    except Exception:
        local = {}

    cores, devices = RegistryHelper.root_sections(remote)

    # Nerd Font icons for status column (display only)
    STAT_ICON_NEW = ""
    STAT_ICON_UPD = ""

    def status_label(remote_ver: float, local_ver_: float, missing_local: bool) -> str:
        if missing_local:
            return "NEW"
        if remote_ver > (local_ver_ or 0.0):
            return "UPD"
        return ""

    def local_ver(source_path: str, target: str, scope: str) -> tuple[float, bool]:
        """Check if package exists locally for specific target (core/device)."""
        if not source_path or not target:
            return 0.0, True

        # Extract package name from source path
        # e.g., "core/_std/src/i2c.py" -> "i2c"
        # e.g., "core/_std/src/upaho/__init__.py" -> "upaho"
        filename = source_path.split("/")[-1] if "/" in source_path else source_path
        name = filename.replace(".py", "").replace(".pyi", "").replace("__init__", "")
        if not name and "/" in source_path:
            parts = source_path.split("/")
            name = parts[-2] if len(parts) >= 2 else ""

        return _find_local_pkg_version(local, scope=scope, target=target, source_path=source_path, pkg_name=name)

    def add_core_rows(core_name: str, rows: list):
        for relpath, pkg_meta in RegistryHelper.walk_files_for_core(remote, core_name, "src"):
            if not relpath.endswith(".py"):
                continue
            rver = RegistryHelper.get_version(pkg_meta)
            source = pkg_meta.get("source", "")
            lver, missing = local_ver(source, core_name, "core")
            stat = status_label(rver, lver, missing)
            # Extract package name from source (e.g., "slip" from "core/_std/src/slip.py")
            pkg_name = pkg_meta.get("source", "").split("/")[-1].replace(".py", "")
            # Display folder name for __init__.py files
            display_path = f"src/{relpath}"
            if relpath.endswith("/__init__.py"):
                display_path = display_path.replace("/__init__.py", "/")
            rows.append(("core", core_name, stat, f"{rver:.1f}", display_path, pkg_name))

    def add_device_rows(dev_name: str, rows: list):
        for relpath, pkg_meta in RegistryHelper.walk_files_for_device(remote, dev_name, "src"):
            if not relpath.endswith(".py"):
                continue
            rver = RegistryHelper.get_version(pkg_meta)
            source = pkg_meta.get("source", "")
            lver, missing = local_ver(source, dev_name, "device")
            stat = status_label(rver, lver, missing)
            # Extract package name from source
            pkg_name = pkg_meta.get("source", "").split("/")[-1].replace(".py", "")
            # Display folder name for __init__.py files
            display_path = f"src/{relpath}"
            if relpath.endswith("/__init__.py"):
                display_path = display_path.replace("/__init__.py", "/")
            rows.append(("device", dev_name, stat, f"{rver:.1f}", display_path, pkg_name))

    def resolve_current_dev_core() -> tuple[Optional[str], Optional[str]]:
        """Get current device and core from status returned by _ensure_connected()."""
        # Use status values from _ensure_connected() to ensure correct device/core
        # for the specified port (e.g., --port com1)
        if not status or not status.get('connected'):
            return None, None
        
        cur_core = status.get('core', '').strip()
        cur_dev = status.get('device', '').strip()
        
        if not cur_core:
            return None, None
        
        # Convert device name to filesystem path format for matching GitHub metadata
        dk = SearchHelper.key_ci(devices, device_name_to_path(cur_dev)) if cur_dev else None
        ck = SearchHelper.key_ci(cores, cur_core) if cur_core else None
        
        if ck:
            return dk, ck

        return None, None

    rows: list[tuple[str, str, str, str, str, str]] = []
    cur_dev_key, cur_core_key = resolve_current_dev_core()

    if lib_name:
        # Try both original name and converted name for device matching
        dkey = SearchHelper.key_ci(devices, device_name_to_path(lib_name))
        ckey = SearchHelper.key_ci(cores, lib_name)

        if dkey and cur_dev_key and dkey == cur_dev_key:
            # Device found, add device packages
            add_device_rows(dkey, rows)
        elif ckey and cur_core_key and ckey == cur_core_key:
            # Core found, add core packages
            add_core_rows(ckey, rows)
        else:
            # Search by name within current core/device packages
            q = lib_name.lower()
            temp_rows = []
            
            # Get all packages for current core/device
            if cur_core_key:
                add_core_rows(cur_core_key, temp_rows)
                if cur_dev_key and cur_dev_key != cur_core_key:
                    add_device_rows(cur_dev_key, temp_rows)
            
            # Filter by search query
            for scope, target, stat, ver_str, shown_path, pkg_name in temp_rows:
                # Check if query matches package name or file path
                if (q in pkg_name.lower()) or (q in shown_path.lower()):
                    rows.append((scope, target, stat, ver_str, shown_path, pkg_name))
    else:
        # No search query - list only for current core/device
        if cur_core_key:
            add_core_rows(cur_core_key, rows)
            if cur_dev_key and cur_dev_key != cur_core_key:
                add_device_rows(cur_dev_key, rows)

    if not rows:
        OutputHelper.print_panel(
            "No results found.",
            title=f"Search Results [{owner}/{repo}@{ref}]",
            border_style="yellow"
        )
        return

    def row_key(r):
        scope_order = 0 if r[0] == "core" else 1
        return (scope_order, r[1].lower(), r[4].lower())

    rows.sort(key=row_key)

    w1 = max(5, max(len(r[0]) for r in rows))
    w2 = max(6, max(len(r[1]) for r in rows))

    def _stat_display(stat: str) -> str:
        if stat == "NEW":
            return STAT_ICON_NEW
        if stat == "UPD":
            return STAT_ICON_UPD
        return ""

    w3 = max(4, max(len(_stat_display(r[2])) for r in rows))  # STAT
    w4 = max(3, max(len(r[3]) for r in rows))  # VER

    lines = []
    lines.append(f"{'SCOPE'.ljust(w1)}   {'TARGET'.ljust(w2)}   {'STAT'.ljust(w3)}   {'VER'.ljust(w4)}  FILE")
    lines.append("─" * (80 - 4))

    def _color_target(scope: str, padded: str) -> str:
        if scope == "core":
            return f"[bright_green]{padded}[/bright_green]"
        if scope == "device":
            return f"[bright_yellow]{padded}[/bright_yellow]"
        return padded

    def _color_stat(padded: str, stat: str) -> str:
        if stat == "NEW":
            return f"[bright_yellow]{padded}[/bright_yellow]"
        if stat == "UPD":
            return f"[cyan]{padded}[/cyan]"
        return padded

    def _color_file(padded: str, stat: str) -> str:
        # De-emphasize unchanged entries; keep NEW/UPD entries readable.
        if stat in ("NEW", "UPD"):
            return padded
        return f"[dim]{padded}[/dim]"

    for scope, target, stat, ver_str, shown_path, _pkg_name in rows:
        scope_cell = scope.ljust(w1)
        target_cell = _color_target(scope, target.ljust(w2))

        stat_shown = _stat_display(stat)
        stat_cell_raw = stat_shown.ljust(w3)
        stat_cell = _color_stat(stat_cell_raw, stat)

        ver_cell = ver_str.ljust(w4)

        file_plain = shown_path[4:]
        file_cell = _color_file(file_plain, stat)

        lines.append(f"{scope_cell}   {target_cell}   {stat_cell}   {ver_cell}  {file_cell}")

    lines.append("")
    lines.append(f"[dim]{STAT_ICON_NEW} new   {STAT_ICON_UPD} update[/dim]")

    
    OutputHelper.print_panel(
        "\n".join(lines),
        title=f"Search Results [{owner}/{repo}@{ref}]",
        border_style="magenta",
    )


def _pkg_download(args: list[str], owner: str, repo: str, ref: str):
    _ensure_connected()
    
    StoreManager.ensure_home_store()

    client = _create_agent_client()
    status = client.send_command('status')
    if not status.get('connected'):
        OutputHelper.print_panel(
            "Device not connected.",
            title="Download Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    dev = status.get('device', '').strip()
    core = status.get('core', '').strip()
    
    if not core:
        OutputHelper.print_panel(
            "Failed to get device info from connected board.",
            title="Download Error",
            border_style="red"
        )
        raise typer.Exit(1)

    try:
        remote = StoreManager.load_remote_meta(owner, repo, ref)
    except Exception as e:
        raise typer.BadParameter(f"Failed to load remote meta: {e}")

    cores, devices = RegistryHelper.root_sections(remote)
    
    core_exists_remote = core in cores if core else False
    device_exists_remote = (device_name_to_path(dev) in devices if dev else False) and (dev != core)
    
    if not core_exists_remote:
        OutputHelper.print_panel(
            f"Core [yellow]{core}[/yellow] not found in remote registry.",
            title="Notice",
            border_style="yellow"
        )
        raise typer.Exit(0)

    try:
        local = StoreManager.load_local_meta()
        if not isinstance(local, dict):
            local = {}
    except Exception:
        local = {}

    # Initialize local packages structure
    local_packages = local.setdefault("packages", {})
    
    # Copy only the platform_cores and device_configs for the current core/device
    if "platform_cores" in remote and core:
        remote_platform_cores = remote.get("platform_cores", {})
        if core in remote_platform_cores:
            if "platform_cores" not in local:
                local["platform_cores"] = {}
            local["platform_cores"][core] = remote_platform_cores[core]
    
    if "device_configs" in remote and dev and dev != core:
        remote_device_configs = remote.get("device_configs", {})
        device_key = device_name_to_path(dev)
        if device_key in remote_device_configs:
            if "device_configs" not in local:
                local["device_configs"] = {}
            local["device_configs"][device_key] = remote_device_configs[device_key]
    
    if "schema_version" in remote:
        local["schema_version"] = remote.get("schema_version")

    def _local_touch_package(pkg_name: str, pkg_meta: dict) -> None:
        """Add or update a package in local registry."""
        local_packages[pkg_name] = pkg_meta.copy()

    exts = (".py", ".pyi", ".json")

    def _plan_for_core(core_name: str, part: str) -> list[tuple[str, dict, str, str, dict, str]]:
        """Returns list of (relpath, file_pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type)."""
        todo = []
        remote_packages = remote.get("packages", {})
        
        for relpath, pkg_meta in RegistryHelper.walk_files_for_core(remote, core_name, part):
            if not relpath.endswith(exts):
                continue
            
            source = pkg_meta.get("source", "")
            rver = RegistryHelper.get_version(pkg_meta)
            
            # Find original package by source (walk_files may return variant metadata)
            # For core packages, the original package has this source in its base metadata or variants
            orig_pkg_name = None
            orig_pkg_meta = None
            
            for name, meta in remote_packages.items():
                # Check base package source
                if meta.get("source") == source:
                    orig_pkg_name = name
                    orig_pkg_meta = meta
                    break
                # Check variants
                variants = meta.get("variants", {})
                for var_name, var_meta in variants.items():
                    if var_meta.get("source") == source:
                        # Found in variant - use base package name but preserve variant's source
                        orig_pkg_name = name
                        # Copy base metadata and override source with variant's source
                        orig_pkg_meta = meta.copy()
                        orig_pkg_meta["source"] = source
                        break
                if orig_pkg_name:
                    break
            
            if not orig_pkg_name or not orig_pkg_meta:
                # Fallback: extract from source path
                pkg_name = source.split("/")[-1].replace(".py", "").replace(".pyi", "").replace("__init__", "")
                if not pkg_name or pkg_name == "":
                    # For __init__.py, use parent folder name
                    pkg_name = source.split("/")[-2] if len(source.split("/")) >= 2 else "unknown"
                orig_pkg_name = pkg_name
                orig_pkg_meta = pkg_meta
            
            # Check local version using the same resolver as `pkg search`
            # (handles variant keys and source-based matching)
            source_for_match = (
                source
                or orig_pkg_meta.get("source", "")
                or orig_pkg_meta.get("typehint", "")
                or ""
            )
            lver, missing = _find_local_pkg_version(
                local,
                scope="core",
                target=core_name,
                source_path=source_for_match,
                pkg_name=orig_pkg_name,
            )

            change_type = "NEW" if missing else ("UPD" if float(lver) < float(rver) else "")
            
            if float(lver) < float(rver):
                # Extract filename for display
                display_name = source.split("/")[-1].replace(".py", "").replace(".pyi", "")
                todo.append((relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type))
        
        return todo

    def _plan_for_device(device_name: str, part: str) -> list[tuple[str, dict, str, str, dict, str]]:
        """Returns list of (relpath, file_pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type)."""
        todo = []
        remote_packages = remote.get("packages", {})
        
        for relpath, pkg_meta in RegistryHelper.walk_files_for_device(remote, device_name, part):
            if not relpath.endswith(exts):
                continue
            
            source = pkg_meta.get("source", "")
            rver = RegistryHelper.get_version(pkg_meta)
            
            # Find original package by source
            orig_pkg_name = None
            orig_pkg_meta = None
            
            for name, meta in remote_packages.items():
                # Check base package source
                if meta.get("source") == source:
                    orig_pkg_name = name
                    orig_pkg_meta = meta
                    break
                # Check variants
                variants = meta.get("variants", {})
                for var_name, var_meta in variants.items():
                    if var_meta.get("source") == source:
                        # Found in variant - use base package name but preserve variant's source
                        orig_pkg_name = name
                        # Copy base metadata and override source with variant's source
                        orig_pkg_meta = meta.copy()
                        orig_pkg_meta["source"] = source
                        break
                if orig_pkg_name:
                    break
            
            if not orig_pkg_name or not orig_pkg_meta:
                # Fallback: extract from source path
                pkg_name = source.split("/")[-1].replace(".py", "").replace(".pyi", "").replace("__init__", "")
                if not pkg_name or pkg_name == "":
                    # For __init__.py, use parent folder name
                    pkg_name = source.split("/")[-2] if len(source.split("/")) >= 2 else "unknown"
                orig_pkg_name = pkg_name
                orig_pkg_meta = pkg_meta
            
            # Check local version using the same resolver as `pkg search`
            source_for_match = (
                source
                or orig_pkg_meta.get("source", "")
                or orig_pkg_meta.get("typehint", "")
                or ""
            )
            lver, missing = _find_local_pkg_version(
                local,
                scope="device",
                target=device_name,
                source_path=source_for_match,
                pkg_name=orig_pkg_name,
            )

            change_type = "NEW" if missing else ("UPD" if float(lver) < float(rver) else "")
            
            if float(lver) < float(rver):
                # Extract display name
                display_name = source.split("/")[-1].replace(".py", "").replace(".pyi", "")
                todo.append((relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type))
        
        return todo

    plan = []
    new_count = 0
    upd_count = 0
    download_targets = []
    
    # Check core for updates (always check, even if already downloaded)
    if core_exists_remote:
        core_src_files = _plan_for_core(core, "src")
        core_hints_files = _plan_for_core(core, "typehints")
        
        for relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type in core_src_files:
            plan.append(("core", core, "src", relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type))
            if change_type == "NEW":
                new_count += 1
            elif change_type == "UPD":
                upd_count += 1
        
        for relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type in core_hints_files:
            plan.append(("core", core, "typehints", relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type))
            if change_type == "NEW":
                new_count += 1
            elif change_type == "UPD":
                upd_count += 1
        
        if core_src_files or core_hints_files:
            download_targets.append(f"core/{core}")
    
    # Check device for updates (always check, even if already downloaded)
    if device_exists_remote:
        device_src_files = _plan_for_device(device_name_to_path(dev), "src")
        device_hints_files = _plan_for_device(device_name_to_path(dev), "typehints")
        
        for relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type in device_src_files:
            plan.append(("device", device_name_to_path(dev), "src", relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type))
            if change_type == "NEW":
                new_count += 1
            elif change_type == "UPD":
                upd_count += 1
        
        for relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type in device_hints_files:
            plan.append(("device", device_name_to_path(dev), "typehints", relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, change_type))
            if change_type == "NEW":
                new_count += 1
            elif change_type == "UPD":
                upd_count += 1
        
        if device_src_files or device_hints_files:
            download_targets.append(f"device/{dev}")
    
    download_target = " + ".join(download_targets) if download_targets else ""
    
    total = len(plan)

    if total > 0:
        done = 0
        done_lock = threading.Lock()
        errors = []
        
        def download_file(task):
            scope, target, part, relpath, pkg_meta, display_name, orig_pkg_name, orig_pkg_meta, _change_type = task
            
            # Get correct source path based on part (src or typehints)
            if part == "typehints":
                source = pkg_meta.get("typehint", "")
                if not source:
                    return (False, relpath, "No typehint path in metadata")
            else:
                source = pkg_meta.get("source", "")
            
            # Build local path based on deploy_path: scope/target/part/relpath
            # e.g., core/RP2350/src/upaho/__init__.py
            # For device typehints, add device name as package folder: device/ticle_lite/typehints/ticle_lite/button.pyi
            if scope == "device" and part == "typehints":
                out_path = os.path.join(StoreManager.pkg_root(), scope, target, part, target, relpath.replace("/", os.sep))
            else:
                out_path = os.path.join(StoreManager.pkg_root(), scope, target, part, relpath.replace("/", os.sep))
            
            try:
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                InstallHelper.download_raw_file(owner, repo, ref, source, out_path)
                
                # Download submodules if present
                # Use submodules_typehints for typehints part, otherwise use submodules
                if part == "typehints":
                    submodules = pkg_meta.get("submodules_typehints", [])
                else:
                    submodules = pkg_meta.get("submodules", [])
                    
                for submodule_path in submodules:
                    # Build submodule path relative to main module's directory
                    # e.g., relpath="ws2812/__init__.py", submodule="device/_ticle/src/display/ws2812/effect.py"
                    # -> sub_relpath="ws2812/effect.py"
                    relpath_dir = os.path.dirname(relpath) if "/" in relpath or "\\" in relpath else ""
                    submodule_filename = os.path.basename(submodule_path)
                    sub_relpath = os.path.join(relpath_dir, submodule_filename) if relpath_dir else submodule_filename
                    
                    # For device typehints, add device name as package folder
                    if scope == "device" and part == "typehints":
                        sub_out_path = os.path.join(StoreManager.pkg_root(), scope, target, part, target, sub_relpath.replace("/", os.sep))
                    else:
                        sub_out_path = os.path.join(StoreManager.pkg_root(), scope, target, part, sub_relpath.replace("/", os.sep))
                    
                    os.makedirs(os.path.dirname(sub_out_path), exist_ok=True)
                    InstallHelper.download_raw_file(owner, repo, ref, submodule_path, sub_out_path)
                
                # Save original package metadata (not variant metadata) to local registry
                # Use scope:target:name as unique key to avoid conflicts between core/device packages
                unique_pkg_name = f"{scope}:{target}:{orig_pkg_name}"
                _local_touch_package(unique_pkg_name, orig_pkg_meta)
                return (True, relpath, None)
            except urllib.error.HTTPError:
                url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{source}"
                return (False, relpath, f"404 Not Found - URL: {url}")
            except OSError as e:
                return (False, relpath, f"Local: {e}")
            except Exception as e:
                return (False, relpath, str(e))
        
        default_workers = 4
        max_workers = min(
            int(os.environ.get("REPLX_DOWNLOAD_THREADS", str(default_workers))),
            total,
            8
        )
        
        with Live(OutputHelper.create_progress_panel(done, total, title=f"Downloading {download_target}", message=f"Downloading {total} file(s)..."), console=OutputHelper._console, refresh_per_second=10) as live:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_task = {executor.submit(download_file, task): task for task in plan}
                
                for future in as_completed(future_to_task):
                    success, relpath, error = future.result()
                    
                    with done_lock:
                        done += 1
                        
                        if not success:
                            errors.append(f"{relpath}: {error}")
                        
                        live.update(OutputHelper.create_progress_panel(
                            done, total, 
                            title=f"Downloading {download_target}", 
                            message=f"Downloading... {relpath} ({done}/{total})"
                        ))
        
        if errors:
            OutputHelper._console.print("\n[red]Download errors:[/red]")
            for err in errors[:5]:
                OutputHelper._console.print(f"  [yellow]•[/yellow] {err}")
            if len(errors) > 5:
                OutputHelper._console.print(f"  [dim]... and {len(errors) - 5} more errors[/dim]")
            raise typer.BadParameter(f"Failed to download {len(errors)} file(s)")
    
    # Create __init__.pyi for device typehints if it doesn't exist
    if device_exists_remote and dev:
        device_typehints_pkg_dir = os.path.join(StoreManager.pkg_root(), "device", device_name_to_path(dev), "typehints", device_name_to_path(dev))
        init_pyi_path = os.path.join(device_typehints_pkg_dir, "__init__.pyi")
        
        if os.path.isdir(device_typehints_pkg_dir) and not os.path.exists(init_pyi_path):
            try:
                with open(init_pyi_path, "w", encoding="utf-8") as f:
                    f.write("# Type hints for device package\n")
            except Exception:
                pass  # Silently ignore if we can't create the file
    
    StoreManager.save_local_meta(local)
    
    if total == 0:
        # No updates needed
        if dev == core:
            message = f"Core [bright_green]{core}[/bright_green] is already up to date."
        else:
            message = f"Core [bright_green]{core}[/bright_green] and device [bright_green]{dev}[/bright_green] are already up to date."
    else:
        message = f"[bright_green]{download_target}[/bright_green]: [green]{total}[/green] file(s) downloaded. ( {new_count},  {upd_count})"
    
    OutputHelper.print_panel(
        message,
        title="Download Complete",
        border_style="green"
    )


INSTALL_HELP = """\
SPEC can be:
  (empty)             Install all core and device libs/files.
  core/               Install core libs into /lib/...
  device/             Install device libs into /lib/<device>/...
  ./foo.py            -> /lib/foo.mpy
  ./main.py|boot.py   -> /main.mpy or /boot.mpy
  ./app/              -> /app (folder)
  https://.../x.py    -> /lib/x.mpy
"""


def _install_spec_internal(spec: str, live=None, update_callback=None):
    if spec.startswith("core/") or spec.startswith("device/"):
        scope, rest = InstallHelper.resolve_spec(spec)
        base, local_list = InstallHelper.list_local_py_targets(scope, rest)
        if not local_list:
            raise typer.BadParameter("No local files to install. Run 'replx pkg download' first.")

        total = len(local_list)
        
        # Show compilation progress
        if not live and not update_callback:
            from rich.spinner import Spinner
            temp_panel = Panel(
                Spinner("dots", text=f" Preparing {total} files for installation..."),
                title="Compiling", title_align="left", border_style="cyan",
                box=get_panel_box(), width=CONSOLE_WIDTH
            )
            temp_live = Live(temp_panel, console=OutputHelper._console, refresh_per_second=10)
            temp_live.start()
        else:
            temp_live = None
        
        batch_specs = []
        unique_dirs = set()
        for abs_file, rel in local_list:
            rel_dir = os.path.dirname(rel)
            
            # Check if it's a Python file or other file
            is_python = abs_file.endswith(".py")
            
            if rel.startswith("ext/") and "/" in rel[4:]:
                parts = rel.split("/")
                if len(parts) >= 3:
                    rel_dir = "ext"
                    remote_dir = InstallHelper.remote_dir_for(scope, rel_dir)
                    
                    if is_python:
                        CompilerHelper.compile_to_staging(abs_file, base)
                        out_file = CompilerHelper.staging_out_for(abs_file, base, CompilerHelper.mpy_arch_tag())
                        remote_path = ("/" + remote_dir + os.path.splitext(parts[-1])[0] + ".mpy").replace("//", "/")
                    else:
                        # Non-Python file: copy as-is
                        out_file = abs_file
                        remote_path = ("/" + remote_dir + parts[-1]).replace("//", "/")
                    
                    batch_specs.append((out_file, remote_path))
                    unique_dirs.add(remote_dir)
                    continue
            
            remote_dir = InstallHelper.remote_dir_for(scope, rel_dir)
            
            if is_python:
                # Python file: compile to .mpy
                CompilerHelper.compile_to_staging(abs_file, base)
                out_file = CompilerHelper.staging_out_for(abs_file, base, CompilerHelper.mpy_arch_tag())
                remote_path = ("/" + remote_dir + os.path.splitext(os.path.basename(rel))[0] + ".mpy").replace("//", "/")
            else:
                # Non-Python file: copy as-is (e.g., .bin, .json, etc.)
                out_file = abs_file
                remote_path = ("/" + remote_dir + os.path.basename(rel)).replace("//", "/")
            
            batch_specs.append((out_file, remote_path))
            
            if remote_dir:
                unique_dirs.add(remote_dir)
        
        # Stop temporary compilation spinner
        if temp_live:
            temp_live.stop()
        
        if unique_dirs:
            client = _create_agent_client()
            
            all_paths = set()
            for remote_dir in sorted(unique_dirs):
                parts = [p for p in remote_dir.replace("\\", "/").strip("/").split("/") if p]
                path = ""
                for p in parts:
                    path = path + "/" + p
                    all_paths.add(path)
            
            for path in sorted(all_paths):
                try:
                    client.send_command('mkdir', path=path)
                except Exception:
                    pass
        
        client = _create_agent_client()
        
        file_sizes = []
        for local_path, _ in batch_specs:
            size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
            file_sizes.append(size)
        total_bytes_all = sum(file_sizes)
        
        progress_state = {
            "file_idx": 0,
            "total_files": total,
            "current_file": "",
            "bytes_sent": 0,
            "bytes_total": 0,
            "cumulative_bytes": 0,
            "total_bytes_all": total_bytes_all,
        }
        progress_lock = threading.Lock()
        
        def progress_callback(data):
            with progress_lock:
                if isinstance(data, dict):
                    progress_state["bytes_sent"] = data.get("current", 0)
                    progress_state["bytes_total"] = data.get("total", 0)
        
        def do_install(live_obj):
            for idx, (local_path, remote_path) in enumerate(batch_specs):
                filename = os.path.basename(local_path)
                file_size = file_sizes[idx]
                
                with progress_lock:
                    progress_state["file_idx"] = idx
                    progress_state["current_file"] = filename
                    progress_state["bytes_sent"] = 0
                    progress_state["bytes_total"] = file_size
                
                upload_result = [None]
                def do_upload_thread(lp=local_path, rp=remote_path):
                    try:
                        upload_result[0] = _upload_file_with_progress(client, lp, rp, progress_callback)
                    except Exception as e:
                        upload_result[0] = {"error": str(e)}
                
                upload_thread = threading.Thread(target=do_upload_thread, daemon=True)
                upload_thread.start()
                
                while upload_thread.is_alive():
                    with progress_lock:
                        bytes_sent = progress_state["bytes_sent"]
                        cumulative = progress_state["cumulative_bytes"]
                    
                    total_sent = cumulative + bytes_sent
                    
                    msg = f"[{idx+1}/{total}] {filename} ({_format_size(file_size)})"
                    
                    counter_text = f"({_format_size(total_sent)}/{_format_size(total_bytes_all)})"
                    panel = OutputHelper.create_progress_panel(total_sent, total_bytes_all, title=f"Installing {spec} to {STATE.device}", message=msg, counter_text=counter_text)
                    
                    if update_callback:
                        update_callback(panel)
                    else:
                        live_obj.update(panel)
                    time.sleep(0.1)
                
                upload_thread.join()
                
                with progress_lock:
                    progress_state["cumulative_bytes"] += file_size
                
                resp = upload_result[0]
                if resp and resp.get('error'):
                    pass
            
            panel = OutputHelper.create_progress_panel(total_bytes_all, total_bytes_all, title=f"Installing {spec} to {STATE.device}", message="Complete", counter_text=f"({_format_size(total_bytes_all)}/{_format_size(total_bytes_all)})")
            if update_callback:
                update_callback(panel)
            else:
                live_obj.update(panel)
        
        if live is not None:
            do_install(live)
        else:
            with Live(OutputHelper.create_progress_panel(0, total_bytes_all, title=f"Installing {spec} to {STATE.device}", message=f"Processing {total} file(s)...", counter_text=f"(0B/{_format_size(total_bytes_all)})"), console=OutputHelper._console, refresh_per_second=10) as internal_live:
                do_install(internal_live)
        
        return {"files": total, "bytes": total_bytes_all}
    else:
        raise typer.BadParameter(f"Invalid spec format: {spec}")


def _pkg_update(args: list[str], target_path: Optional[str] = None):
    _ensure_connected()

    StoreManager.ensure_home_store()
    
    expanded_args = []
    for arg in args:
        if '*' in arg or '?' in arg:
            matches = glob.glob(arg)
            if matches:
                expanded_args.extend(matches)
            else:
                expanded_args.append(arg)  # Keep original if no matches
        else:
            expanded_args.append(arg)
    
    spec = expanded_args[0] if expanded_args else None
    
    if len(expanded_args) > 1:
        py_files = [f for f in expanded_args if f.endswith('.py') and os.path.isfile(os.path.abspath(f))]
        if not py_files:
            OutputHelper.print_panel(
                f"No valid .py files found in: [yellow]{' '.join(expanded_args)}[/yellow]",
                title="Update Error",
                border_style="red"
            )
            raise typer.Exit(1)
        
        if target_path:
            target_dir = target_path
        else:
            target_dir = "lib"
        
        compiled_files = []
        total_size = 0
        for py_file in py_files:
            ap = os.path.abspath(py_file)
            CompilerHelper.compile_to_staging(ap, os.path.dirname(ap))
            name = os.path.basename(ap)
            remote = f"/{target_dir}/{name[:-3]}.mpy"
            out_mpy = CompilerHelper.staging_out_for(ap, os.path.dirname(ap), CompilerHelper.mpy_arch_tag())
            file_size = os.path.getsize(out_mpy)
            compiled_files.append((out_mpy, remote, target_dir, file_size))
            total_size += file_size
        
        client = _create_agent_client()
        
        total_files = len(compiled_files)
        
        progress_state = {"cumulative": 0, "current": 0}
        progress_lock = threading.Lock()
        
        def progress_callback(data):
            with progress_lock:
                if isinstance(data, dict):
                    progress_state["current"] = data.get("current", 0)
        
        with Live(OutputHelper.create_progress_panel(0, total_size, title=f"Updating {total_files} files to {STATE.device}", message=f"Preparing...", counter_text=f"0/{_format_size(total_size)}"), console=OutputHelper._console, refresh_per_second=10) as live:
            for idx, (local_mpy, remote, _, file_size) in enumerate(compiled_files):
                filename = os.path.basename(local_mpy)
                
                with progress_lock:
                    progress_state["current"] = 0
                
                upload_result = [None]
                def do_upload(lp=local_mpy, rp=remote):
                    try:
                        upload_result[0] = client.send_command_streaming(
                            'put_from_local_streaming',
                            progress_callback=progress_callback,
                            local_path=lp,
                            remote_path=rp
                        )
                    except Exception as e:
                        upload_result[0] = {"error": str(e)}
                
                upload_thread = threading.Thread(target=do_upload, daemon=True)
                upload_thread.start()
                
                while upload_thread.is_alive():
                    with progress_lock:
                        current_bytes = progress_state["current"]
                        cumulative = progress_state["cumulative"]
                    total_sent = cumulative + current_bytes
                    live.update(OutputHelper.create_progress_panel(total_sent, total_size, title=f"Updating {total_files} files to {STATE.device}", message=f"[{idx+1}/{total_files}] {filename} ({_format_size(file_size)})", counter_text=f"{_format_size(total_sent)}/{_format_size(total_size)}"))
                    time.sleep(0.05)
                
                upload_thread.join()
                
                with progress_lock:
                    progress_state["cumulative"] += file_size
            
            live.update(OutputHelper.create_progress_panel(total_size, total_size, title=f"Updating {total_files} files to {STATE.device}", message="Complete", counter_text=f"{_format_size(total_size)}/{_format_size(total_size)}"))
        
        target_display = f"/{target_dir}/" if target_path else "/lib/"
        OutputHelper.print_panel(
            f"[green]{total_files}[/green] file(s) ([cyan]{_format_size(total_size)}[/cyan]) updated to [cyan]{target_display}[/cyan]",
            title="Update Complete",
            border_style="green"
        )
        return

    def _install_local_folder(abs_dir: str, target_path: Optional[str] = None):
        py_files = []
        other_files = []
        for dp, _, fns in os.walk(abs_dir):
            for fn in fns:
                full_path = os.path.join(dp, fn)
                if fn.endswith(".py"):
                    py_files.append(full_path)
                else:
                    other_files.append(full_path)
        
        total = len(py_files) + len(other_files)
        if total == 0:
            OutputHelper.print_panel(
                f"No files found in [yellow]{abs_dir}[/yellow]",
                title="Update",
                border_style="yellow"
            )
            return 0
        
        base = abs_dir
        folder_name = os.path.basename(abs_dir)
        
        if target_path:
            base_target = f"{target_path}/{folder_name}"
        else:
            base_target = f"lib/{folder_name}"
        
        upload_files = []
        total_size = 0
        
        for ap in py_files:
            CompilerHelper.compile_to_staging(ap, base)
            rel = os.path.relpath(ap, base).replace("\\", "/")
            remote = f"/{base_target}/{rel}"
            remote = remote[:-3] + ".mpy"
            out_mpy = CompilerHelper.staging_out_for(ap, base, CompilerHelper.mpy_arch_tag())
            rel_dir = f"{base_target}/{os.path.dirname(rel)}".rstrip("/")
            file_size = os.path.getsize(out_mpy)
            upload_files.append((out_mpy, remote, rel_dir, file_size))
            total_size += file_size
        
        for ap in other_files:
            rel = os.path.relpath(ap, base).replace("\\", "/")
            remote = f"/{base_target}/{rel}"
            rel_dir = f"{base_target}/{os.path.dirname(rel)}".rstrip("/")
            file_size = os.path.getsize(ap)
            upload_files.append((ap, remote, rel_dir, file_size))
            total_size += file_size
        
        unique_dirs = set()
        for _, _, rel_dir, _ in upload_files:
            if rel_dir:
                unique_dirs.add(rel_dir)
        
        client = _create_agent_client()

        for rel_dir in unique_dirs:
            try:
                InstallHelper.ensure_remote_dir(rel_dir, client=client)
            except Exception:
                pass
        
        total_files = len(upload_files)
        folder_name = os.path.basename(abs_dir)
        
        progress_state = {"cumulative": 0, "current": 0}
        progress_lock = threading.Lock()
        
        def progress_callback(data):
            with progress_lock:
                if isinstance(data, dict):
                    progress_state["current"] = data.get("current", 0)
        
        with Live(OutputHelper.create_progress_panel(0, total_size, title=f"Updating {folder_name} to {STATE.device}", message=f"Preparing...", counter_text=f"0/{_format_size(total_size)}"), console=OutputHelper._console, refresh_per_second=10) as live:
            for idx, (local_file, remote, _, file_size) in enumerate(upload_files):
                filename = os.path.basename(local_file)
                
                with progress_lock:
                    progress_state["current"] = 0
                
                upload_result = [None]
                def do_upload(lp=local_file, rp=remote):
                    try:
                        upload_result[0] = client.send_command_streaming(
                            'put_from_local_streaming',
                            progress_callback=progress_callback,
                            local_path=lp,
                            remote_path=rp
                        )
                    except Exception as e:
                        upload_result[0] = {"error": str(e)}
                
                upload_thread = threading.Thread(target=do_upload, daemon=True)
                upload_thread.start()
                
                while upload_thread.is_alive():
                    with progress_lock:
                        current_bytes = progress_state["current"]
                        cumulative = progress_state["cumulative"]
                    total_sent = cumulative + current_bytes
                    live.update(OutputHelper.create_progress_panel(total_sent, total_size, title=f"Updating {folder_name} to {STATE.device}", message=f"[{idx+1}/{total_files}] {filename} ({_format_size(file_size)})", counter_text=f"{_format_size(total_sent)}/{_format_size(total_size)}"))
                    time.sleep(0.05)
                
                upload_thread.join()
                
                with progress_lock:
                    progress_state["cumulative"] += file_size
            
            live.update(OutputHelper.create_progress_panel(total_size, total_size, title=f"Updating {folder_name} to {STATE.device}", message="Complete", counter_text=f"{_format_size(total_size)}/{_format_size(total_size)}"))
        
        target_display = f"/{base_target}/"
        OutputHelper.print_panel(
            f"[green]{total_files}[/green] file(s) ([cyan]{_format_size(total_size)}[/cyan]) updated to [cyan]{target_display}[/cyan]",
            title="Update Complete",
            border_style="green"
        )
        return total_files

    def _install_single_file(abs_file: str, target_path: Optional[str] = None):
        base = os.path.dirname(abs_file)
        name = os.path.basename(abs_file)
        
        if target_path:
            target_dir = target_path
        else:
            target_dir = "lib"
        
        
        if abs_file.endswith('.py'):
            CompilerHelper.compile_to_staging(abs_file, base)
            local_file = CompilerHelper.staging_out_for(abs_file, base, CompilerHelper.mpy_arch_tag())
            remote = f"/{target_dir}/{name[:-3]}.mpy"
        else:
            local_file = abs_file
            remote = f"/{target_dir}/{name}"
        
        file_size = os.path.getsize(local_file)
        client = _create_agent_client()
        InstallHelper.ensure_remote_dir(target_dir, client=client)
        
        progress_state = {"current": 0}
        progress_lock = threading.Lock()
        
        def progress_callback(data):
            with progress_lock:
                if isinstance(data, dict):
                    progress_state["current"] = data.get("current", 0)
        
        with Live(OutputHelper.create_progress_panel(0, file_size, title=f"Updating {name} to {STATE.device}", message="Uploading...", counter_text=f"0/{_format_size(file_size)}"), console=OutputHelper._console, refresh_per_second=10) as live:
            upload_result = [None]
            def do_upload():
                try:
                    upload_result[0] = client.send_command_streaming(
                        'put_from_local_streaming',
                        progress_callback=progress_callback,
                        local_path=local_file,
                        remote_path=remote
                    )
                except Exception as e:
                    upload_result[0] = {"error": str(e)}
            
            upload_thread = threading.Thread(target=do_upload, daemon=True)
            upload_thread.start()
            
            while upload_thread.is_alive():
                with progress_lock:
                    current_bytes = progress_state["current"]
                live.update(OutputHelper.create_progress_panel(current_bytes, file_size, title=f"Updating {name} to {STATE.device}", message="Uploading...", counter_text=f"{_format_size(current_bytes)}/{_format_size(file_size)}"))
                time.sleep(0.05)
            
            upload_thread.join()
            
            resp = upload_result[0]
            if resp and not resp.get('success'):
                OutputHelper.print_panel(
                    f"Upload failed: [red]{resp.get('error', 'Unknown error')}[/red]",
                    title="Update Failed",
                    border_style="red"
                )
                return 0
            live.update(OutputHelper.create_progress_panel(file_size, file_size, title=f"Updating {name} to {STATE.device}", message="Complete", counter_text=f"{_format_size(file_size)}/{_format_size(file_size)}"))
        
        target_display = f"/{target_dir}/" if target_path else "/lib/"
        OutputHelper.print_panel(
            f"[green]1[/green] file ([cyan]{_format_size(file_size)}[/cyan]) updated to [cyan]{target_display}[/cyan]",
            title="Update Complete",
            border_style="green"
        )
        return 1

    if spec and (spec == "core/" or spec == "device/"):
        if target_path:
            OutputHelper.print_panel(
                "[yellow]--target[/yellow] option is not supported for [cyan]core/[/cyan] or [cyan]device/[/cyan] targets.\n\n"
                "The target path is automatically determined for these targets.",
                title="Update Warning",
                border_style="yellow"
            )
        _install_spec_internal(spec)
        return

    if spec and InstallHelper.is_url(spec):
        u = urlparse(spec)
        fname = os.path.basename(u.path)
        if not fname.endswith(".py"):
            OutputHelper.print_panel(
                f"Only single .py file is supported for URL installs.\n\n"
                f"URL: [yellow]{spec}[/yellow]",
                title="Update Error",
                border_style="red"
            )
            raise typer.Exit(1)
        dl_dir = StoreManager.HOME_STAGING / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        dst = str(dl_dir / fname)
        try:
            with urllib.request.urlopen(spec) as r, open(dst, "wb") as f:
                f.write(r.read())
        except Exception as e:
            OutputHelper.print_panel(
                f"Download failed: [red]{e}[/red]\n\n"
                f"URL: [yellow]{spec}[/yellow]",
                title="Update Error",
                border_style="red"
            )
            raise typer.Exit(1)
        try:
            _install_single_file(dst, target_path)
        finally:
            try:
                os.remove(dst)
            except Exception:
                pass
        return

    if not spec:
        OutputHelper.print_panel(
            "Specify a target:\n\n"
            "  [bright_blue]core/[/bright_blue]         Install core libraries for connected board\n"
            "  [bright_blue]device/[/bright_blue]       Install device libraries for connected board\n"
            "  [bright_blue]<file>[/bright_blue]        Upload single file to /lib/ (.py → .mpy)\n"
            "  [bright_blue]<folder>[/bright_blue]      Upload folder to /lib/<folder>/\n"
            "  [bright_blue]<URL>[/bright_blue]         Download and install from URL\n\n"
            "Options:\n"
            "  [bright_blue]--target PATH[/bright_blue] Specify board target path (for file/folder)\n\n"
            "Use [bright_blue]replx init[/bright_blue] to install all core and device libraries.",
            title="Update Error",
            border_style="red"
        )
        raise typer.Exit(1)

    target = spec
    ap = os.path.abspath(target)
    if os.path.isdir(ap):
        _install_local_folder(ap, target_path)
        return
    if os.path.isfile(ap):
        _install_single_file(ap, target_path)
        return

    OutputHelper.print_panel(
        f"Target not found: [red]{spec}[/red]\n\n"
        "Valid targets:\n"
        "  [bright_blue]core/[/bright_blue]         Install core libraries for connected board\n"
        "  [bright_blue]device/[/bright_blue]       Install device libraries for connected board\n"
        "  [bright_blue]<file>[/bright_blue]        Upload single file to /lib/ (.py → .mpy)\n"
        "  [bright_blue]<folder>[/bright_blue]      Upload folder to /lib/<folder>/\n"
        "  [bright_blue]<URL>[/bright_blue]         Download and install from URL\n\n"
        "Options:\n"
        "  [bright_blue]--target PATH[/bright_blue] Specify board target path (for file/folder only)",
        title="Update Error",
        border_style="red"
    )
    raise typer.Exit(1)


def _pkg_clean(args: list[str]):
    import shutil
    
    _ensure_connected()
    
    pkg_root = StoreManager.pkg_root()
    core_path = os.path.join(pkg_root, "core", STATE.core)
    # Important: Convert device name (e.g., "ticle-lite" -> "ticle_lite")
    device_path = os.path.join(pkg_root, "device", device_name_to_path(STATE.device))
    meta_path = StoreManager.local_meta_path()
    
    core_exists = os.path.isdir(core_path)
    device_exists = os.path.isdir(device_path)
    meta_exists = os.path.isfile(meta_path)
    
    if not core_exists and not device_exists:
        OutputHelper.print_panel(
            f"No libraries for [yellow]{STATE.core}/{STATE.device}[/yellow] found in local store.",
            title="Clean",
            border_style="yellow"
        )
        return
    
    removed_items = []
    total_size = 0
    
    def get_dir_size(path: str) -> int:
        total = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
        return total
    
    try:
        # Remove core and device folders
        if core_exists:
            size = get_dir_size(core_path)
            shutil.rmtree(core_path)
            removed_items.append(f"core/{STATE.core}/ ({_format_size(size)})")
            total_size += size
        
        if device_exists:
            size = get_dir_size(device_path)
            shutil.rmtree(device_path)
            removed_items.append(f"device/{STATE.device}/ ({_format_size(size)})")
            total_size += size
        
        # Remove entries from local registry (not the file itself)
        if meta_exists:
            local_meta = StoreManager.load_local_meta()
            local_packages = local_meta.get("packages", {})
            
            # Remove core package entries
            # Important: Use device_name_to_path() to match registry keys
            core_prefix = f"core:{STATE.core}:"
            device_prefix = f"device:{device_name_to_path(STATE.device)}:"
            
            keys_to_remove = [k for k in local_packages.keys() 
                             if k.startswith(core_prefix) or k.startswith(device_prefix)]
            
            entries_removed = 0
            if keys_to_remove:
                for key in keys_to_remove:
                    del local_packages[key]
                entries_removed += len(keys_to_remove)
            
            # Remove platform_cores entry for this core
            if core_exists and "platform_cores" in local_meta:
                if STATE.core in local_meta["platform_cores"]:
                    del local_meta["platform_cores"][STATE.core]
                    entries_removed += 1
            
            # Remove device_configs entry for this device
            if device_exists and "device_configs" in local_meta:
                device_key = device_name_to_path(STATE.device)
                if device_key in local_meta["device_configs"]:
                    del local_meta["device_configs"][device_key]
                    entries_removed += 1
            
            if entries_removed > 0:
                StoreManager.save_local_meta(local_meta)
                removed_items.append(f"registry.json ({entries_removed} entries removed)")
        
        if removed_items:
            items_text = "\n".join(f"  [green]✓[/green] {item}" for item in removed_items)
            OutputHelper.print_panel(
                f"Removed from local store:\n{items_text}\n\n"
                f"Total freed: [bright_yellow]{_format_size(total_size)}[/bright_yellow]",
                title="Clean Complete",
                border_style="green"
            )
        
    except Exception as e:
        OutputHelper.print_panel(
            f"Failed to clean local store: [red]{e}[/red]",
            title="Clean Error",
            border_style="red"
        )
        raise typer.Exit(1)


@app.command(name="mpy", rich_help_panel="Package Management")
def mpy(
    files: list[str] = typer.Argument(None, help="Python files or directories to compile"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output path (for single file)"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Compile Python files to MicroPython bytecode (.mpy).

[bold cyan]Usage:[/bold cyan] replx mpy [yellow]FILE(s)[/yellow] [OPTIONS]

[bold cyan]Options:[/bold cyan]
  [yellow]-o, --output PATH[/yellow]     Output path (single file only)

[bold cyan]Examples:[/bold cyan]
  replx mpy main.py                  [dim]# Compile single file[/dim]
  replx mpy main.py -o out.mpy       [dim]# Specify output path[/dim]
  replx mpy *.py                     [dim]# Compile all .py files[/dim]
  replx mpy src/                     [dim]# Compile folder[/dim]

[bold cyan]Output:[/bold cyan]
  • Single file: same directory as source (or -o path)
  • Multiple files: same directory as each source
  • Folder: parallel structure in folder

[bold cyan]Note:[/bold cyan]
  • Requires board connection (architecture auto-detected)
  • Compiled .mpy files are smaller and use less RAM"""
        OutputHelper.print_panel(help_text, border_style="dim")
        console.print()
        raise typer.Exit()
    
    if not files:
        OutputHelper.print_panel(
            "Specify Python files to compile:\n\n"
            "  [bright_green]replx mpy main.py[/bright_green]          [dim]# Single file[/dim]\n"
            "  [bright_green]replx mpy *.py[/bright_green]             [dim]# Multiple files[/dim]\n"
            "  [bright_green]replx mpy src/[/bright_green]             [dim]# Folder[/dim]\n\n"
            "Use [bright_blue]replx mpy --help[/bright_blue] for details.",
            title="MPY Compiler",
            border_style="yellow"
        )
        raise typer.Exit(1)
    
    # Ensure board is connected to get architecture
    _ensure_connected()
    target_arch = STATE.core
    
    if not target_arch:
        OutputHelper.print_panel(
            "Could not determine board architecture.\n\n"
            "Please reconnect the board and try again.",
            title="Architecture Unknown",
            border_style="red"
        )
        raise typer.Exit(1)
    
    # Expand globs and collect files
    py_files = []
    for pattern in files:
        if '*' in pattern or '?' in pattern:
            matches = glob.glob(pattern)
            for m in matches:
                if m.endswith('.py') and os.path.isfile(m):
                    py_files.append(os.path.abspath(m))
        elif os.path.isdir(pattern):
            for root, _, fnames in os.walk(pattern):
                for fn in fnames:
                    if fn.endswith('.py'):
                        py_files.append(os.path.abspath(os.path.join(root, fn)))
        elif os.path.isfile(pattern):
            if pattern.endswith('.py'):
                py_files.append(os.path.abspath(pattern))
            else:
                OutputHelper.print_panel(
                    f"Not a Python file: [red]{pattern}[/red]",
                    title="Invalid File",
                    border_style="red"
                )
                raise typer.Exit(1)
        else:
            OutputHelper.print_panel(
                f"File not found: [red]{pattern}[/red]",
                title="File Not Found",
                border_style="red"
            )
            raise typer.Exit(1)
    
    if not py_files:
        OutputHelper.print_panel(
            "No Python files found to compile.",
            title="No Files",
            border_style="yellow"
        )
        raise typer.Exit(1)
    
    # Check output option validity
    if output and len(py_files) > 1:
        OutputHelper.print_panel(
            "The [yellow]-o/--output[/yellow] option can only be used with a single file.",
            title="Invalid Option",
            border_style="red"
        )
        raise typer.Exit(1)
    
    version = STATE.version if STATE.version else "1.24.0"
    # Use CompilerHelper mapping to keep `mpy` in sync with `pkg update`.
    arch_args = CompilerHelper._march_for_core(target_arch, version)
    
    compiled = []
    failed = []
    total_src_size = 0
    total_mpy_size = 0
    
    try:
        import mpy_cross
    except ImportError:
        OutputHelper.print_panel(
            "mpy-cross is not installed.\n\n"
            "Install it with: [bright_green]pip install mpy-cross[/bright_green]",
            title="Missing Dependency",
            border_style="red"
        )
        raise typer.Exit(1)
    
    for py_file in py_files:
        src_size = os.path.getsize(py_file)
        total_src_size += src_size
        
        # Determine output path
        if output:
            out_mpy = output if output.endswith('.mpy') else output + '.mpy'
        else:
            out_mpy = os.path.splitext(py_file)[0] + '.mpy'
        
        # Compile
        args = [py_file, '-o', out_mpy] + arch_args
        
        try:
            mpy_cross.run(*args)
            
            # Verify output
            if os.path.exists(out_mpy) and os.path.getsize(out_mpy) > 0:
                mpy_size = os.path.getsize(out_mpy)
                total_mpy_size += mpy_size
                compiled.append((py_file, out_mpy, src_size, mpy_size))
            else:
                failed.append((py_file, "Output file not created"))
        except Exception as e:
            failed.append((py_file, str(e)))
    
    # Report results
    if compiled:
        if len(compiled) == 1:
            py_file, out_mpy, src_size, mpy_size = compiled[0]
            ratio = (1 - mpy_size / src_size) * 100 if src_size > 0 else 0
            OutputHelper.print_panel(
                f"[green]✓[/green] {os.path.basename(py_file)} → {os.path.basename(out_mpy)}\n\n"
                f"  Source: [cyan]{format_size(src_size)}[/cyan]\n"
                f"  Output: [cyan]{format_size(mpy_size)}[/cyan] [dim]({ratio:.0f}% smaller)[/dim]\n"
                f"  Arch:   [yellow]{target_arch}[/yellow]",
                title="Compiled",
                border_style="green"
            )
        else:
            lines = []
            for py_file, out_mpy, src_size, mpy_size in compiled:
                lines.append(f"[green]✓[/green] {os.path.basename(py_file)} → {os.path.basename(out_mpy)} ({format_size(mpy_size)})")
            
            ratio = (1 - total_mpy_size / total_src_size) * 100 if total_src_size > 0 else 0
            lines.append("")
            lines.append(f"Total: [cyan]{format_size(total_src_size)}[/cyan] → [cyan]{format_size(total_mpy_size)}[/cyan] [dim]({ratio:.0f}% smaller)[/dim]")
            lines.append(f"Arch:  [yellow]{target_arch}[/yellow]")
            
            OutputHelper.print_panel(
                "\n".join(lines),
                title=f"Compiled {len(compiled)} files",
                border_style="green"
            )
    
    if failed:
        lines = [f"[red]✗[/red] {os.path.basename(f)}: {err}" for f, err in failed]
        OutputHelper.print_panel(
            "\n".join(lines),
            title=f"Failed ({len(failed)} files)",
            border_style="red"
        )
        raise typer.Exit(1)
