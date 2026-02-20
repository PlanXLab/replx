import os
import re
import time
import urllib.request
import urllib.error
import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from ..helpers import (
    OutputHelper, StoreManager,
    get_panel_box, CONSOLE_WIDTH
)
from ..connection import (
    _ensure_connected, _create_agent_client
)

from ..app import app


SUPPORTED_FIRMWARE_DEVICES = {
    "ticle-lite",
    "ticle-sensor", 
}

FIRMWARE_REPO_OWNER = "PlanXLab"
FIRMWARE_REPO_NAME = "ticle_firmware"
FIRMWARE_REPO_BRANCH = "main"


def _get_firmware_local_dir(device: str) -> Path:
    return StoreManager.HOME_STORE / "firmware" / device


def _ensure_firmware_dir(device: str) -> Path:
    firmware_dir = _get_firmware_local_dir(device)
    firmware_dir.mkdir(parents=True, exist_ok=True)
    return firmware_dir


def _parse_version(version_str: str) -> tuple:
    try:
        parts = version_str.split('.')
        return tuple(int(p) for p in parts)
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _get_remote_firmware_info(device: str) -> dict:
    api_url = f"https://api.github.com/repos/{FIRMWARE_REPO_OWNER}/{FIRMWARE_REPO_NAME}/releases/latest"
    
    headers = {"User-Agent": "replx"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            release_data = json.load(response)
            
        version = release_data.get('tag_name', '').lstrip('v')
        
        for asset in release_data.get('assets', []):
            asset_name = asset.get('name', '')
            if asset_name == f"{device}.uf2":
                return {
                    'version': version,
                    'download_url': asset.get('browser_download_url'),
                    'size': asset.get('size', 0)
                }
        
        download_url = f"https://github.com/{FIRMWARE_REPO_OWNER}/{FIRMWARE_REPO_NAME}/releases/download/v{version}/{device}.uf2"
        return {
            'version': version,
            'download_url': download_url,
            'size': 0  # Unknown
        }
        
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception:
        return None


def _get_local_firmware_versions(device: str) -> list[tuple[str, Path]]:
    firmware_dir = _get_firmware_local_dir(device)
    if not firmware_dir.exists():
        return []
    
    versions = []
    pattern = re.compile(rf"^{re.escape(device)}_v(.+)\.uf2$")
    
    for f in firmware_dir.iterdir():
        if f.is_file():
            match = pattern.match(f.name)
            if match:
                version = match.group(1)
                versions.append((version, f))
    
    versions.sort(key=lambda x: _parse_version(x[0]), reverse=True)
    return versions


def _cleanup_old_firmware(device: str, keep_version: str):
    versions = _get_local_firmware_versions(device)
    for version, path in versions:
        if version != keep_version:
            try:
                path.unlink()
            except Exception:
                pass


def _download_firmware(device: str, version: str, url: str, target_path: Path, 
                       show_progress: bool = True) -> bool:
    
    try:
        headers = {"User-Agent": "replx"}
        req = urllib.request.Request(url, headers=headers)
        
        if show_progress:
            spinner = Spinner("dots", text=Text(f" Downloading {device} firmware v{version}...", style="bright_cyan"))
            with Live(spinner, console=OutputHelper._console, refresh_per_second=10, transient=True):
                with urllib.request.urlopen(req, timeout=60) as response:
                    content = response.read()
        else:
            with urllib.request.urlopen(req, timeout=60) as response:
                content = response.read()
        
        target_path.parent.mkdir(parents=True, exist_ok=True)
        
        temp_path = target_path.with_suffix('.tmp')
        with open(temp_path, 'wb') as f:
            f.write(content)
        
        temp_path.rename(target_path)
        return True
        
    except urllib.error.HTTPError as e:
        if show_progress:
            OutputHelper.print_panel(
                f"Download failed: HTTP {e.code}\n\nURL: {url}",
                title="Download Error",
                border_style="red"
            )
        return False
    except Exception as e:
        if show_progress:
            OutputHelper.print_panel(
                f"Download failed: {str(e)}",
                title="Download Error",
                border_style="red"
            )
        return False


def _find_uf2_drive() -> Optional[Path]:
    import platform
    system = platform.system()
    
    if system == "Windows":
        import ctypes
        import string
        
        drives = []
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if bitmask & 1:
                drives.append(f"{letter}:")
            bitmask >>= 1
        
        for drive in drives:
            try:
                info_file = Path(drive) / "INFO_UF2.TXT"
                if info_file.exists():
                    return Path(drive)
            except Exception:
                continue
                
    elif system == "Darwin":
        volumes = Path("/Volumes")
        if volumes.exists():
            for vol in volumes.iterdir():
                try:
                    # Check for RPI-RP2 or RP2040/RP2350 bootloader
                    vol_name_upper = vol.name.upper()
                    if any(name in vol_name_upper for name in ["RPI", "RP2", "PICO", "BOOTLOADER"]):
                        info_file = vol / "INFO_UF2.TXT"
                        if info_file.exists():
                            return vol
                except (PermissionError, OSError):
                    continue
                        
    elif system == "Linux":
        # Linux: Check common mount points
        user = os.getenv('USER', '')
        search_paths = [
            Path("/media"),
            Path("/mnt"),
            Path(f"/media/{user}") if user else None,
            Path(f"/run/media/{user}") if user else None,
        ]
        for base in search_paths:
            if base and base.exists():
                try:
                    for mount in base.iterdir():
                        if mount.is_dir():
                            # Check for INFO_UF2.TXT or typical bootloader volume name
                            try:
                                info_file = mount / "INFO_UF2.TXT"
                                if info_file.exists():
                                    return mount
                                # Also check volume name
                                mount_name_upper = mount.name.upper()
                                if any(name in mount_name_upper for name in ["RPI", "RP2", "PICO", "BOOTLOADER"]):
                                    if (mount / "INDEX.HTM").exists() or (mount / "INDEX.HTML").exists():
                                        return mount
                            except (PermissionError, OSError):
                                continue
                except (PermissionError, OSError):
                    continue
    
    return None


def _wait_for_uf2_drive(timeout: int = 30, live=None) -> Optional[Path]:
    """Wait for UF2 bootloader drive to appear.
    
    Args:
        timeout: Maximum seconds to wait
        live: Optional Live object to update (if None, no UI updates)
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        drive = _find_uf2_drive()
        if drive:
            return drive
        time.sleep(0.5)
    
    return None


@app.command(rich_help_panel="Device Management")
def firmware(
    args: list[str] = typer.Argument(None, help="Firmware command: download or update"),
    force: bool = typer.Option(False, "--force", "-f", help="Force update even if already up to date"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    if show_help:
        help_text = """\
Firmware management for RP2350-based ticle devices.

[bold cyan]Usage:[/bold cyan] replx firmware [yellow]SUBCOMMAND[/yellow] [[green]options[/green]]

[bold cyan]Subcommands:[/bold cyan]
  [yellow]download[/yellow]        Download latest firmware to local store
  [yellow]update[/yellow]          Download (if needed) and install firmware

[bold cyan]Options:[/bold cyan]
  [green]-f, --force[/green]     Force reinstall even if version matches

[bold cyan]Examples:[/bold cyan]
  replx firmware download       [dim]Download firmware only[/dim]
  replx firmware update         [dim]Download and install[/dim]
  replx firmware update -f      [dim]Force reinstall[/dim]

[bold cyan]Update Process:[/bold cyan]
  1. Download firmware (if newer version available)
  2. Enter UF2 bootloader mode
  3. Copy .uf2 file to bootloader drive
  4. Device restarts with new firmware
  5. Auto-reconnect to device

[bold cyan]Note:[/bold cyan]
  • Supported: ticle-lite, ticle-sensor, ticle-autocon, etc.
  • USB connection required (RP2350 devices only)
  • Old versions auto-cleaned, keeps only latest"""
        OutputHelper._console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        OutputHelper._console.print()
        raise typer.Exit()
    
    if not args:
        OutputHelper.print_panel(
            "Specify a command: [bright_blue]download[/bright_blue] or [bright_blue]update[/bright_blue]\n\n"
            "Usage:\n"
            "  [bright_green]replx firmware download[/bright_green]  [dim]# Download only[/dim]\n"
            "  [bright_green]replx firmware update[/bright_green]    [dim]# Download and install[/dim]",
            title="Firmware",
            border_style="yellow"
        )
        raise typer.Exit(1)
    
    cmd = args[0].lower()
    
    if cmd == "download":
        _firmware_download()
    elif cmd == "update":
        _firmware_update(force=force)
    else:
        OutputHelper.print_panel(
            f"Unknown command: [red]{cmd}[/red]\n\n"
            "Use: [bright_blue]replx firmware download[/bright_blue] or [bright_blue]replx firmware update[/bright_blue]",
            title="Firmware Error",
            border_style="red"
        )
        raise typer.Exit(1)


def _firmware_download():
    status = _ensure_connected()
    
    device = status.get('device', '').lower()
    if not device:
        OutputHelper.print_panel(
            "Could not determine device type from connected board.",
            title="Firmware Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    if device not in SUPPORTED_FIRMWARE_DEVICES:
        OutputHelper.print_panel(
            f"Device [yellow]{device}[/yellow] is not supported for firmware management.\n\n"
            f"Supported devices: {', '.join(sorted(SUPPORTED_FIRMWARE_DEVICES))}",
            title="Firmware Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    current_version = status.get('version', 'unknown')
    
    console = Console()
    spinner = Spinner("dots", text=Text(" Checking for firmware updates...", style="bright_cyan"))
    
    with Live(spinner, console=console, refresh_per_second=10, transient=True):
        remote_info = _get_remote_firmware_info(device)
    
    if not remote_info:
        OutputHelper.print_panel(
            f"Could not find firmware for [yellow]{device}[/yellow] in remote repository.",
            title="Firmware Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    remote_version = remote_info['version']
    download_url = remote_info['download_url']
    
    local_versions = _get_local_firmware_versions(device)
    local_latest = local_versions[0][0] if local_versions else None
    
    if local_latest and _parse_version(local_latest) >= _parse_version(remote_version):
        firmware_path = _get_firmware_local_dir(device) / f"{device}_v{local_latest}.uf2"
        OutputHelper.print_panel(
            f"Firmware is already up to date.\n\n"
            f"  Device:      [bright_yellow]{device}[/bright_yellow]\n"
            f"  Current:     [bright_cyan]v{current_version}[/bright_cyan]\n"
            f"  Local:       [bright_green]v{local_latest}[/bright_green]\n"
            f"  Remote:      v{remote_version}\n\n"
            f"  File: [dim]{firmware_path}[/dim]",
            title="Firmware Up to Date",
            border_style="green"
        )
        return
    
    firmware_dir = _ensure_firmware_dir(device)
    target_path = firmware_dir / f"{device}_v{remote_version}.uf2"
    
    if _download_firmware(device, remote_version, download_url, target_path):
        _cleanup_old_firmware(device, remote_version)
        
        OutputHelper.print_panel(
            f"Firmware downloaded successfully.\n\n"
            f"  Device:      [bright_yellow]{device}[/bright_yellow]\n"
            f"  Version:     [bright_green]v{remote_version}[/bright_green]\n"
            f"  Current:     [dim]v{current_version}[/dim]\n\n"
            "To install, run: [bright_blue]replx firmware update[/bright_blue]",
            title="Firmware Downloaded",
            border_style="green"
        )
    else:
        raise typer.Exit(1)


def _firmware_update(force: bool = False):
    status = _ensure_connected()
    
    device = status.get('device', '').lower()
    port = status.get('port', '')
    core = status.get('core', '')
    
    if not device:
        OutputHelper.print_panel(
            "Could not determine device type from connected board.",
            title="Firmware Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    if device not in SUPPORTED_FIRMWARE_DEVICES:
        OutputHelper.print_panel(
            f"Device [yellow]{device}[/yellow] is not supported for firmware management.\n\n"
            f"Supported devices: {', '.join(sorted(SUPPORTED_FIRMWARE_DEVICES))}",
            title="Firmware Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    if core and not core.startswith("RP2"):
        OutputHelper.print_panel(
            f"Device core [yellow]{core}[/yellow] does not support UF2 firmware updates.\n\n"
            "UF2 updates are only supported for RP2040/RP2350 devices.",
            title="Firmware Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    current_version = status.get('version', 'unknown')
    
    spinner = Spinner("dots", text=Text(" Checking for firmware updates...", style="bright_cyan"))
    with Live(spinner, console=OutputHelper._console, refresh_per_second=10, transient=True):
        remote_info = _get_remote_firmware_info(device)
    
    if not remote_info:
        OutputHelper.print_panel(
            f"Could not find firmware for [yellow]{device}[/yellow] in remote repository.",
            title="Firmware Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    remote_version = remote_info['version']
    download_url = remote_info['download_url']
    
    local_versions = _get_local_firmware_versions(device)
    local_latest = local_versions[0][0] if local_versions else None
    firmware_path = None
    
    if local_latest and _parse_version(local_latest) >= _parse_version(remote_version):
        firmware_path = _get_firmware_local_dir(device) / f"{device}_v{local_latest}.uf2"
        target_version = local_latest
    else:
        firmware_dir = _ensure_firmware_dir(device)
        firmware_path = firmware_dir / f"{device}_v{remote_version}.uf2"
        
        if not _download_firmware(device, remote_version, download_url, firmware_path):
            raise typer.Exit(1)
        
        _cleanup_old_firmware(device, remote_version)
        target_version = remote_version
    
    if not force and _parse_version(current_version) >= _parse_version(target_version):
        OutputHelper.print_panel(
            f"Firmware is already up to date.\n\n"
            f"  Device:      [bright_yellow]{device}[/bright_yellow]\n"
            f"  Current:     [bright_green]v{current_version}[/bright_green]\n"
            f"  Available:   v{target_version}\n\n"
            "Use [bright_blue]--force[/bright_blue] to reinstall anyway.",
            title="No Update Needed",
            border_style="green"
        )
        return
    
    def format_bytes(b):
        if b < 1024:
            return f"{b}B"
        elif b < 1024 * 1024:
            return f"{b/1024:.1f}KB"
        else:
            return f"{b/(1024*1024):.1f}MB"
    
    spinner = Spinner("dots", style="bright_cyan")
    
    def make_update_panel(step: str, progress: float = None, detail: str = None, use_spinner: bool = True, 
                          complete: bool = False, success: bool = True, final_version: str = None):
        """Create a unified update panel with current step and optional progress."""
        from rich.table import Table
        from rich.console import Group
        
        # Always use the same basic structure for info
        info_text = Text()
        info_text.append(f"  Device:      ")
        info_text.append(f"{device}", style="bright_yellow")
        info_text.append(f"\n  Current:     ")
        info_text.append(f"v{current_version}", style="dim")
        info_text.append(f"\n  Target:      ")
        info_text.append(f"v{target_version}", style="bright_green")
        
        # Always add empty line
        separator = Text("\n")
        
        if complete:
            # Add completion message
            status_display = Text()
            status_display.append("  ")
            if success:
                status_display.append("✓", style="bright_green")
                status_display.append(" Firmware update complete!")
                if final_version and final_version != current_version:
                    status_display.append(f"\n  New version: ")
                    status_display.append(f"v{final_version}", style="bright_green")
            else:
                status_display.append("!", style="yellow")
                status_display.append(" Firmware installed but auto-reconnect failed.")
                status_display.append(f"\n  Please reconnect: ")
                status_display.append(f"replx --port {port} setup", style="bright_blue")
            
            content = Group(info_text, separator, status_display)
            border_style = "cyan"  # Keep same border style
        elif progress is not None:
            bar_width = 50
            filled = int(bar_width * progress)
            bar_filled = "█" * filled
            bar_empty = "░" * (bar_width - filled)
            pct = int(progress * 100)
            
            progress_text = Text()
            progress_text.append(f"  [")
            progress_text.append(bar_filled, style="green")
            progress_text.append(bar_empty, style="dim")
            progress_text.append(f"] {pct}%")
            if detail:
                progress_text.append(f" {detail}", style="dim")
            
            content = Group(info_text, separator, progress_text)
            border_style = "cyan"
        elif use_spinner:
            step_table = Table.grid(padding=(0, 1))
            step_table.add_column(width=3)
            step_table.add_column()
            step_table.add_row(spinner, Text(step, style="default"))
            
            content = Group(info_text, separator, step_table)
            border_style = "cyan"
        else:
            step_text = Text()
            step_text.append(f"  ")
            step_text.append("●", style="bright_cyan")
            step_text.append(f" {step}")
            content = Group(info_text, separator, step_text)
            border_style = "cyan"
        
        return Panel(
            content,
            title="Firmware Update",
            title_align="left",
            border_style=border_style,
            box=get_panel_box(),
            width=CONSOLE_WIDTH
        )
    
    reconnected = False
    new_version = None
    was_foreground = False
    
    # Start with empty text to avoid initial render
    from rich.text import Text as RichText
    with Live(RichText(""), console=OutputHelper._console, refresh_per_second=10, transient=False) as live:
        # Step 1: Save current connection state
        live.update(make_update_panel("Preparing device for firmware update..."))
        try:
            with _create_agent_client() as client:
                result = client.send_command('session_info', timeout=2.0)
                was_foreground = result.get('is_foreground', False)
        except Exception:
            was_foreground = False
        
        # Step 2: Send bootloader command while connected, then disconnect
        live.update(make_update_panel("Entering bootloader mode..."))
        try:
            with _create_agent_client() as client:
                # Send bootloader command
                client.send_command('exec', code='import machine; machine.bootloader()', timeout=1.0)
        except Exception:
            pass
        
        # Step 3: Fully disconnect the serial port
        time.sleep(0.3)
        try:
            with _create_agent_client() as client:
                client.send_command('disconnect_port', port=port, timeout=2.0)
        except Exception:
            pass
        
        time.sleep(1.0)
        
        # Step 4: Wait for UF2 bootloader drive
        live.update(make_update_panel("Waiting for bootloader drive..."))
        uf2_drive = _wait_for_uf2_drive(timeout=15, live=live)
        
        if not uf2_drive:
            live.stop()
            OutputHelper.print_panel(
                "Bootloader drive not found.\n\n"
                "Please manually:\n"
                "  1. Hold BOOTSEL button and press RESET\n"
                "  2. Copy the UF2 file to the drive\n\n"
                f"  File: [dim]{firmware_path}[/dim]",
                title="Manual Installation Required",
                border_style="yellow"
            )
            raise typer.Exit(1)
        
        # Step 5: Copy firmware to UF2 drive
        import platform
        try:
            target_uf2 = uf2_drive / f"{device}.uf2"
            file_size = firmware_path.stat().st_size
            copied = 0
            chunk_size = 64 * 1024  # 64KB chunks
            
            with open(firmware_path, 'rb') as src, open(target_uf2, 'wb') as dst:
                while True:
                    chunk = src.read(chunk_size)
                    if not chunk:
                        break
                    dst.write(chunk)
                    copied += len(chunk)
                    progress = copied / file_size
                    live.update(make_update_panel(
                        "Copying firmware...",
                        progress=progress,
                        detail=f"{format_bytes(copied)} / {format_bytes(file_size)}"
                    ))
                # Ensure all data is written to disk
                dst.flush()
                os.fsync(dst.fileno())
            
            # Let OS flush all buffers before device auto-reboots
            if platform.system() == "Darwin":
                # On macOS, ensure all writes are committed
                try:
                    os.sync()
                except (AttributeError, OSError):
                    pass
                # Give macOS time to complete all I/O operations
                # This prevents "disk not ejected properly" warning
                time.sleep(1.5)
            elif platform.system() == "Linux":
                try:
                    os.sync()
                except (AttributeError, OSError):
                    pass
                time.sleep(1.0)
            else:
                time.sleep(1.0)
            
            # Device will auto-reboot and drive will disappear
        except Exception as e:
            live.stop()
            OutputHelper.print_panel(
                f"Failed to copy firmware: {str(e)}\n\n"
                f"Please manually copy:\n"
                f"  From: [dim]{firmware_path}[/dim]\n"
                f"  To:   [dim]{uf2_drive}[/dim]",
                title="Copy Failed",
                border_style="red"
            )
            raise typer.Exit(1)
        
        # Step 7: Device reboot (automatic after UF2 copy)
        live.update(make_update_panel("Waiting for device to restart..."))
        time.sleep(3.0)
        
        # Step 8: Reconnect using saved port and foreground state
        live.update(make_update_panel("Reconnecting..."))
        time.sleep(1.0)
        
        # Try reconnection with saved foreground state
        for attempt in range(20):
            try:
                time.sleep(0.5)
                with _create_agent_client() as client:
                    result = client.send_command(
                        'session_setup',
                        port=port,
                        core=core,
                        device=device,
                        as_foreground=was_foreground,
                        timeout=2.0
                    )
                    if result.get('connected'):
                        reconnected = True
                        new_version = result.get('version', 'unknown')
                        break
            except Exception:
                continue
        
        if not reconnected:
            try:
                _ensure_connected()
                with _create_agent_client() as client:
                    new_status = client.send_command('status', timeout=2.0)
                    new_version = new_status.get('version', 'unknown')
                reconnected = True
            except Exception:
                pass
        
        # Final update in Live context - this will stay on screen
        live.update(make_update_panel(
            "", complete=True, success=reconnected, final_version=new_version
        ))
    # Live exits here and leaves the final panel on screen - DO NOT print anything after
