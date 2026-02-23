import os
import sys
import time
import re
import json
from io import StringIO

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from replx.terminal import IS_WINDOWS
from replx.utils import device_name_to_path
from ..agent.client import AgentClient
from ..helpers import (
    OutputHelper, DeviceScanner,
    StoreManager,
    get_panel_box, CONSOLE_WIDTH
)
from ..config import (
    STATE,
    _find_env_file, _find_or_create_vscode_dir,
    _read_env_ini,
    _get_connection_config, _update_connection_config, _get_default_connection,
    _find_available_agent_port, _find_running_agent_port,
    _get_global_options
)
from ..connection import (
    _ensure_connected, _create_agent_client
)

from ..app import app


def _normalize_path_for_comparison(path: str) -> str:
    """Normalize file path for comparison.
    
    Windows: case-insensitive
    Linux/macOS: case-sensitive
    """
    path = os.path.normpath(path)
    if sys.platform.startswith("win"):
        return path.lower()
    else:
        return path


def _serial_port_cmp_key(port: str) -> str:
    """Comparison key for serial ports.

    Policy:
    - Display: keep OS-provided casing.
    - Compare: on Windows, treat COM ports case-insensitively.
    """
    if port is None:
        return ""
    p = str(port).strip()
    return p.lower() if IS_WINDOWS else p


def _serial_port_display(port: str) -> str:
    """Format a serial port name for display.

    Requirement: on Windows, always show port names in uppercase.
    """
    if port is None:
        return ""
    p = str(port).strip()
    return p.upper() if IS_WINDOWS else p


def _resolve_os_serial_port_name(port: str) -> str:
    """Resolve port name to the OS-enumerated spelling (best-effort).

    This is mainly for Windows where users might type `com1` but the OS reports
    `COM1`. We keep display as OS-provided and use case-insensitive comparison
    separately.
    """
    if not port:
        return port
    p = str(port).strip()
    if not IS_WINDOWS:
        return p

    try:
        from serial.tools.list_ports import comports as list_ports_comports

        needle = p.lower()
        for info in list_ports_comports():
            dev = getattr(info, "device", None)
            if isinstance(dev, str) and dev.lower() == needle:
                return dev
    except Exception:
        pass

    return p


def _check_vscode_version(vscode_dir: str) -> bool:
    settings_file = os.path.join(vscode_dir, "settings.json")
    
    for item in os.listdir(vscode_dir):
        if item.endswith('.pyi'):
            return True
    
    if os.path.exists(settings_file):
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                settings = json.load(f)
            extra_paths = settings.get("python.analysis.extraPaths", [])
            
            if not extra_paths:
                return False
            
            has_replx_typehints = False
            for path in extra_paths:
                norm_path = os.path.normpath(path)
                if 'replx' in norm_path and 'typehints' in norm_path:
                    has_replx_typehints = True
                    break
            
            vscode_norm = _normalize_path_for_comparison(vscode_dir)
            for path in extra_paths:
                norm_path = _normalize_path_for_comparison(path)
                if norm_path.startswith(vscode_norm):
                    return True
            
            if extra_paths and not has_replx_typehints:
                return True
                
        except (json.JSONDecodeError, IOError):
            return True
    
    return False


def _create_vscode_files_and_typehints(vscode_dir: str, core: str, device: str, overwrite: bool = False):
    task_file = os.path.join(vscode_dir, "tasks.json")
    settings_file = os.path.join(vscode_dir, "settings.json")
    launch_file = os.path.join(vscode_dir, "launch.json")
    
    task_file_contents = """{
    "version": "2.0.0",
    "tasks": [
        {
            "label": "Run micropython with replx",
            "type": "shell",
            "command": "replx",
            "args": ["${file}"],
            "problemMatcher": [],
            "group": { "kind": "build", "isDefault": true }
        }
    ]
}
"""
    
    extra_paths = []
    
    # 1. MicroPython standard library typehints (included in replx tool)
    from replx.utils.device_info import is_std_micropython
    if is_std_micropython(core):
        comm_path = StoreManager.comm_typehints_path()
    else:
        comm_path = StoreManager.comm_separate_typehints_path(core)
    if comm_path and os.path.isdir(comm_path):
        extra_paths.append(comm_path)
    
    # 2. Core builtin typehints (included in replx tool)
    if core:
        core_builtin = StoreManager.core_typehints_path(core)
        if core_builtin and os.path.isdir(core_builtin):
            extra_paths.append(core_builtin)
    
    # 3. Core library typehints (downloaded via 'replx pkg download')
    if core:
        core_lib_typehints = os.path.join(StoreManager.pkg_root(), "core", core, "typehints")
        if os.path.isdir(core_lib_typehints):
            extra_paths.append(core_lib_typehints)
    
    # 4. Device builtin typehints (included in replx tool)
    if device:
        device_path = device_name_to_path(device)
        device_builtin = StoreManager.device_typehints_path(device_path)
        if device_builtin and os.path.isdir(device_builtin):
            extra_paths.append(device_builtin)
    
    # 5. Device library typehints (downloaded via 'replx pkg download')
    # Note: device name like 'ticle-lite' becomes 'ticle_lite' in filesystem
    if device:
        device_path = device_name_to_path(device)
        device_typehints = os.path.join(StoreManager.pkg_root(), "device", device_path, "typehints")
        if os.path.isdir(device_typehints):
            extra_paths.append(device_typehints)
    
    settings_dict = {
        "files.exclude": {
            "**/.vscode": True
        },
        "python.languageServer": "Pylance",
        "python.analysis.diagnosticSeverityOverrides": {
            "reportMissingModuleSource": "none"
        },
        "python.analysis.extraPaths": extra_paths
    }

    if comm_path and os.path.isdir(comm_path):
        settings_dict["python.analysis.stubPath"] = comm_path
    settings_file_contents = json.dumps(settings_dict, indent=4) + "\n"
    
    launch_file_contents = """{
    "version": "0.2.0",
    "configurations": [
      {
        "name": "Python: Current file debug",
        "type": "debugpy",
        "request": "launch",
        "program": "${file}",
        "console": "integratedTerminal"
      }
    ]
}
"""

    if overwrite or not os.path.exists(task_file):
        with open(task_file, "w", encoding="utf-8") as f:
            f.write(task_file_contents)
    if overwrite or not os.path.exists(settings_file):
        with open(settings_file, "w", encoding="utf-8") as f:
            f.write(settings_file_contents)
    if overwrite or not os.path.exists(launch_file):
        with open(launch_file, "w", encoding="utf-8") as f:
            f.write(launch_file_contents)
    
    return extra_paths


@app.command(rich_help_panel="Connection & Session")
def setup(
    args: list[str] = typer.Argument(None, help="Subcommand: clean"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Initialize MicroPython development environment for VSCode.

Run this once per project folder to set up your workspace.

[bold cyan]What this command does:[/bold cyan]
  1. Connects to your MicroPython board
  2. Creates [yellow].vscode/[/yellow] folder with configuration files:
     • [dim]settings.json[/dim] - Python/Pylance settings
     • [dim]tasks.json[/dim]    - Build task (Ctrl+Shift+B to run)
     • [dim]launch.json[/dim]   - Debug configuration
  3. Copies [yellow]typehints (.pyi)[/yellow] for IntelliSense/autocomplete
  4. Saves connection info for subsequent commands

[bold cyan]Usage:[/bold cyan]
  replx [yellow]PORT[/yellow] setup             [dim]# Serial: COM3, /dev/ttyUSB0[/dim]
  replx [yellow]PORT[/yellow] setup clean       [dim]# Reset config with only this port[/dim]

[bold cyan]Subcommands:[/bold cyan]
  [yellow]clean[/yellow]   Reset .replx file: set PORT as default and remove all history
          This clears all other saved connections.

[bold cyan]Examples:[/bold cyan]
  replx COM3 setup              [dim]# Windows serial port[/dim]
  replx /dev/ttyACM0 setup      [dim]# Linux serial port[/dim]
  replx COM3 setup clean        [dim]# Reset config, keep only COM3[/dim]

[bold cyan]After setup, you can:[/bold cyan]
  replx ls                      [dim]# List files on device[/dim]
  replx run main.py             [dim]# Run local script on device[/dim]
  replx -c "print('hello')"     [dim]# Execute single command[/dim]
  replx repl                    [dim]# Enter interactive Python[/dim]
  [dim]Press Ctrl+Shift+B[/dim]            [dim]# Run current file (VSCode)[/dim]

[bold cyan]Note:[/bold cyan]
  Connection is remembered. After setup, just use [green]replx ls[/green] without port.
  To switch boards, run setup again with the new port."""
        OutputHelper.print_panel(help_text, border_style="dim")
        console.print()
        raise typer.Exit()
    
    clean_mode = args and len(args) > 0 and args[0].lower() == "clean"
    
    global_opts = _get_global_options()
    port = global_opts.get('port')
    agent_port = global_opts.get('agent_port')

    # Store/use the OS-enumerated port spelling when possible.
    port = _resolve_os_serial_port_name(port)

    if not port:
        OutputHelper.print_panel(
            "[bright_blue]--port[/bright_blue] is required for setup.\n\n"
            "Examples:\n"
            "  [bright_green]replx --port COM3 setup[/bright_green]\n"
            "  [bright_green]replx --port /dev/ttyACM0 setup[/bright_green]",
            title="Connection Required",
            border_style="yellow"
        )
        raise typer.Exit(1)
    
    env_path = _find_env_file()
    if agent_port is None:
        # Use original port for connection lookup
        if env_path and port:
            existing_config = _get_connection_config(env_path, port)
            if existing_config and existing_config.get('agent_port'):
                agent_port = existing_config['agent_port']
        
        if agent_port is None and env_path:
            running_agent_port = _find_running_agent_port(env_path)
            if running_agent_port:
                agent_port = running_agent_port
        
        if agent_port is None and env_path:
            default_conn = _get_default_connection(env_path)
            if default_conn:
                default_config = _get_connection_config(env_path, default_conn)
                if default_config and default_config.get('agent_port'):
                    agent_port = default_config['agent_port']
        
        if agent_port is None:
            agent_port = _find_available_agent_port(env_path)
    
    if AgentClient.is_agent_running(port=agent_port):
        try:
            with AgentClient(port=agent_port) as client:
                status = client.send_command('status', timeout=1.0)
            
            if not status.get('connected'):
                try:
                    AgentClient.stop_agent(port=agent_port)
                    time.sleep(0.5)
                except Exception:
                    pass
            elif status.get('connected'):
                current_port = status.get('port', '')
                
                if IS_WINDOWS:
                    same_config = current_port.upper() == port.upper()
                else:
                    same_config = current_port == port
                
                if same_config:
                    device = status.get('device', 'unknown')
                    core = status.get('core', 'unknown')
                    version = status.get('version', '?')
                    manufacturer = status.get('manufacturer', '')
                    
                    vscode_dir = _find_or_create_vscode_dir()
                    env_path = os.path.join(vscode_dir, ".replx")
                    workspace = os.path.dirname(vscode_dir)
                    
                    # Use original port for storing connection
                    _update_connection_config(
                        env_path, port,
                        version=version, core=core, device=device,
                        manufacturer=manufacturer,
                        agent_port=agent_port,
                        set_default=True
                    )
                    
                    try:
                        with AgentClient(port=agent_port) as client:
                            client.send_command('set_default', port=port, update_session=True, timeout=1.0)
                    except Exception:
                        pass
                    
                    typehint_paths = _create_vscode_files_and_typehints(vscode_dir, core, device)
                    
                    content = f"Connection: [bright_blue]{_serial_port_display(current_port)}[/bright_blue] [dim](Default)[/dim]\n"
                    content += f"Version: [yellow]{version}[/yellow]\n"
                    content += f"Core: [bright_green]{core}[/bright_green]\n"
                    content += f"Device: [bright_yellow]{device}[/bright_yellow]\n"
                    if manufacturer:
                        content += f"Manufacturer: [bright_magenta]{manufacturer}[/bright_magenta]\n"
                    content += f"Workspace: [dim]{workspace}[/dim]\n"
                    if typehint_paths:
                        content += f"\nTypehints: [dim]{len(typehint_paths)} path(s) configured[/dim]\n"
                    content += "\n[dim]Already connected with same configuration.[/dim]"
                    
                    OutputHelper.print_panel(
                        content,
                        title="Current Connection",
                        border_style="green"
                    )
                    raise typer.Exit()
                else:
                    # Use original port for session setup
                    try:
                        with AgentClient(port=agent_port, device_port=port) as client:
                            result = client.send_command(
                                'session_setup',
                                as_foreground=True,
                                set_default=True, 
                                local_default=port,
                                timeout=10.0
                            )
                        
                        STATE.core = result.get('core', '')
                        STATE.device = result.get('device', 'unknown')
                        STATE.version = result.get('version', '?')
                        STATE.manufacturer = result.get('manufacturer', '')
                        
                        vscode_dir = _find_or_create_vscode_dir()
                        env_path = os.path.join(vscode_dir, ".replx")
                        
                        # Use original port for storing connection
                        _update_connection_config(
                            env_path, port,
                            version=STATE.version, core=STATE.core, device=STATE.device,
                            manufacturer=STATE.manufacturer,
                            agent_port=agent_port,
                            set_default=True 
                        )
                        
                        typehint_paths = _create_vscode_files_and_typehints(vscode_dir, STATE.core, STATE.device)
                        
                        workspace = os.path.dirname(vscode_dir)
                        content = f"Connection: [bright_blue]{_serial_port_display(port)}[/bright_blue] [dim](Default)[/dim]\n"
                        content += f"Version: [yellow]{STATE.version}[/yellow]\n"
                        content += f"Core: [bright_green]{STATE.core}[/bright_green]\n"
                        content += f"Device: [bright_yellow]{STATE.device}[/bright_yellow]\n"
                        if STATE.manufacturer:
                            content += f"Manufacturer: [bright_magenta]{STATE.manufacturer}[/bright_magenta]\n"
                        content += f"Workspace: [dim]{workspace}[/dim]\n"
                        if typehint_paths:
                            content += f"Typehints: [dim]{len(typehint_paths)} path(s) configured[/dim]\n"
                        content += f"Previous fg: [dim]{_serial_port_display(current_port)}[/dim] → bg"
                        
                        OutputHelper.print_panel(
                            content,
                            title="Connection Added",
                            border_style="green"
                        )
                        raise typer.Exit()
                    except typer.Exit:
                        raise
                    except Exception as e:
                        OutputHelper.print_panel(
                            f"Failed to add connection: {str(e)}\nRestarting agent...",
                            title="Reconnecting",
                            border_style="yellow"
                        )
                        try:
                            AgentClient.stop_agent(port=agent_port)
                            time.sleep(0.5)
                        except Exception:
                            pass
        except typer.Exit:
            raise
        except Exception:
            try:
                AgentClient.stop_agent(port=agent_port)
                time.sleep(0.5)
            except Exception:
                pass
    
    if AgentClient.is_agent_running(port=agent_port):
        try:
            AgentClient.stop_agent(port=agent_port)
            time.sleep(0.5)
        except Exception:
            pass
    
    try:
        AgentClient.start_agent(port=agent_port)
    except Exception as e:
        OutputHelper.print_panel(
            f"Failed to start agent: {str(e)}",
            title="Agent Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    # Use original port for session setup
    try:
        with AgentClient(port=agent_port, device_port=port) as client:
            result = client.send_command('session_setup', 
                                        port=port,
                                        as_foreground=True,
                                        local_default=port)
        
        STATE.core = result.get('core', '')
        STATE.device = result.get('device', 'unknown')
        STATE.version = result.get('version', '?')
        STATE.manufacturer = result.get('manufacturer', '')
    except Exception as e:
        try:
            AgentClient.stop_agent()
        except Exception:
            pass
        
        conn_info = port

        OutputHelper.print_panel(
            f"Connection failure on configured device ([bright_blue]{conn_info}[/bright_blue]).\n\n"
            f"Error details: [red]{str(e)}[/red]\n\n"
            "Please check:\n"
            "  • Device is powered on and connected\n"
            "  • Serial cable is properly attached\n\n"
            "[dim]Run 'replx --port PORT setup' to reconfigure if needed.[/dim]",
            title="Connection Error",
            border_style="red"
        )
        raise typer.Exit(1)

    vscode_dir = _find_or_create_vscode_dir()
    env_path = os.path.join(vscode_dir, ".replx")
    
    need_recreate = False
    if os.path.exists(env_path):
        existing_data = _read_env_ini(env_path)
        existing_default = existing_data.get('default')
        if existing_default:
            existing_config = existing_data['connections'].get(existing_default, {})
            existing_core = existing_config.get('core', '')
            existing_device = existing_config.get('device', '')
            
            if existing_core and existing_device:
                if existing_core != STATE.core or existing_device != STATE.device:
                    need_recreate = True
    
    if not need_recreate and os.path.isdir(vscode_dir):
        if _check_vscode_version(vscode_dir):
            need_recreate = True
    
    if need_recreate:
        import shutil
        for item in os.listdir(vscode_dir):
            item_path = os.path.join(vscode_dir, item)
            try:
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                else:
                    os.remove(item_path)
            except Exception:
                pass
    
    # Use original port for storing connection
    set_as_default = True
    
    if clean_mode:
        from ..config import ConfigManager
        fresh_connections = {
            port: {
                'version': STATE.version,
                'core': STATE.core,
                'device': STATE.device,
                'manufacturer': STATE.manufacturer,
                'agent_port': agent_port
            }
        }
        ConfigManager.write(env_path, fresh_connections, default=port)
    else:
        _update_connection_config(
            env_path,
            port,
            version=STATE.version,
            core=STATE.core,
            device=STATE.device,
            manufacturer=STATE.manufacturer,
            agent_port=agent_port,
            set_default=set_as_default
        )
    
    typehint_paths = _create_vscode_files_and_typehints(vscode_dir, STATE.core, STATE.device, overwrite=True)
    
    display_conn = port
    workspace = os.path.dirname(vscode_dir)
    content = f"Connection: [bright_blue]{_serial_port_display(display_conn)}[/bright_blue] [dim](Default)[/dim]\n"
    content += f"Version: [yellow]{STATE.version}[/yellow]\n"
    content += f"Core: [bright_green]{STATE.core}[/bright_green]\n"
    content += f"Device: [bright_yellow]{STATE.device}[/bright_yellow]\n"
    if STATE.manufacturer:
        content += f"Manufacturer: [bright_magenta]{STATE.manufacturer}[/bright_magenta]\n"
    content += f"Workspace: [dim]{workspace}[/dim]\n"
    if typehint_paths:
        content += f"Typehints: [dim]{len(typehint_paths)} path(s) configured[/dim]"
    if clean_mode:
        content += "\n\n[dim]History cleared. Only this connection is saved.[/dim]"
    
    OutputHelper.print_panel(
        content,
        title="Setup Complete" + (" (Clean)" if clean_mode else ""),
        border_style="green"
    )


@app.command(rich_help_panel="Device Management")
def usage(
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Show memory and storage usage of the connected device.

[bold cyan]Usage:[/bold cyan]
  replx usage
  replx COM3 usage              [dim]# Usage for specific board[/dim]

[bold cyan]Information displayed:[/bold cyan]
  [bold]Memory (RAM)[/bold]
    • Used / Free / Total
    • Visual usage bar with percentage

  [bold]Storage (Flash)[/bold]
    • Used / Free / Total
    • Visual usage bar with percentage

[bold cyan]Example output:[/bold cyan]
  󰍛 Memory
     ████████░░░░░░░░░░░░ 38.2%
     Used: 156 KB  Free: 252 KB  Total: 408 KB

  󰋊 Storage
     ██░░░░░░░░░░░░░░░░░░  8.5%
     Used:  68 KB  Free: 732 KB  Total: 800 KB

[bold cyan]Related:[/bold cyan]
  replx whoami              [dim]# See connected device info[/dim]
  replx ls -r               [dim]# See what's using storage[/dim]"""
        OutputHelper.print_panel(help_text, border_style="dim")
        console.print()
        raise typer.Exit()
    
    _ensure_connected()
    
    try:
        client = _create_agent_client()
        mem_result = client.send_command('mem')
        mem = mem_result.get('mem')
        df_result = client.send_command('df')
        lines = []
        
        def make_bar(used_pct, width=30):
            filled = int(width * used_pct / 100)
            empty = width - filled
            if used_pct < 50:
                color = "green"
            elif used_pct < 80:
                color = "yellow"
            else:
                color = "red"
            return f"[{color}]{'█' * filled}[/{color}][dim]{'░' * empty}[/dim]"
        
        if mem:
            mem_total = mem[2]
            mem_used = mem[1]
            mem_free = mem[0]
            mem_pct = mem[3]
            lines.append("[bold cyan][#87C05A]󰍛[/#87C05A] Memory[/bold cyan]")
            lines.append(f"   {make_bar(mem_pct)} [bold]{mem_pct:.1f}%[/bold]")
            lines.append(f"   [dim]Used:[/dim]  {mem_used//1024:>5} KB  [dim]Free:[/dim] {mem_free//1024:>5} KB  [dim]Total:[/dim] {mem_total//1024:>5} KB")
        else:
            lines.append("[bold cyan][#87C05A]󰍛[/#87C05A] Memory[/bold cyan]  [dim]unavailable[/dim]")
        
        lines.append("")
        
        if df_result:
            fs_total = df_result.get('total', 0)
            fs_used = df_result.get('used', 0)
            fs_free = df_result.get('free', 0)
            fs_pct = df_result.get('percent', 0)
            lines.append("[bold cyan][#D98C53]󰋊[/#D98C53] Storage[/bold cyan]")
            lines.append(f"   {make_bar(fs_pct)} [bold]{fs_pct:.1f}%[/bold]")
            lines.append(f"   [dim]Used:[/dim]  {fs_used//1024:>5} KB  [dim]Free:[/dim] {fs_free//1024:>5} KB  [dim]Total:[/dim] {fs_total//1024:>5} KB")
        else:
            lines.append("[bold cyan][#D98C53]󰋊[/#D98C53] Storage[/bold cyan]  [dim]unavailable[/dim]")
        
        OutputHelper.print_panel("\n".join(lines), title="Usage", border_style="bright_blue")
        
    except Exception as e:
        OutputHelper.print_panel(f"Error: {str(e)}", title="Error", border_style="red")
        raise typer.Exit(1)


@app.command(rich_help_panel="Connection & Session")
def scan(
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Find and list all connected MicroPython boards.

Scans all serial ports to detect MicroPython devices.

[bold cyan]Usage:[/bold cyan]
  replx scan

[bold cyan]Output shows:[/bold cyan]
  [bold]Columns:[/bold]
    • [yellow]Port[/yellow]     - Serial port (COM3, /dev/ttyUSB0)
    • [yellow]Version[/yellow]  - MicroPython version
    • [yellow]Core[/yellow]     - Chip type (RP2350, ESP32, etc.)
    • [yellow]Device[/yellow]   - Board name (ticle, pico, etc.)

  [bold]Status icons:[/bold]
    󱓦  Currently connected (in a session)
    󰷌  Default connection (saved in .replx)

[bold cyan]Example output:[/bold cyan]
  PORT     VERSION  CORE    DEVICE  MANUFACTURER
  ──────────────────────────────────────────────
  󱓦 COM19  1.24.1   RP2350  ticle   Raspberry Pi
    COM3   1.23.0   ESP32   ESP32   Espressif

[bold cyan]Tips:[/bold cyan]
  • Scan does not connect - it only detects available boards
  • Already-connected boards show 󱓦 icon
  • Use the port from scan results with other commands

[bold cyan]Note:[/bold cyan]
  This command ignores -p/--port option.

[bold cyan]Related:[/bold cyan]
  replx COM3 setup        [dim]# Connect to a scanned board[/dim]"""
        OutputHelper.print_panel(help_text, border_style="dim")
        console.print()
        raise typer.Exit()

    serial_results = []
    connected_serial_ports_cmp = set()
    exclude_serial_ports = set()
    
    history_connections = {}
    history_default = None

    env_path = _find_env_file()
    if env_path:
        env_data = _read_env_ini(env_path)
        history_connections = env_data.get('connections', {})
        history_default = env_data.get('default')
    
    history_default_cmp = _serial_port_cmp_key(history_default) if history_default else None

    if AgentClient.is_agent_running():
        try:
            with AgentClient() as client:
                session_info = client.send_command('session_info', timeout=1.0)
                
                all_connected_ports = set()
                for session in session_info.get('sessions', []):
                    if session.get('foreground'):
                        all_connected_ports.add(session['foreground'])
                    for bg in session.get('backgrounds', []):
                        all_connected_ports.add(bg)
                
                for port_key in all_connected_ports:
                    try:
                        status = client.send_command('status', port=port_key, timeout=1.0)
                        if status.get('connected'):
                            display_port = _serial_port_display(port_key)
                            connected_serial_ports_cmp.add(_serial_port_cmp_key(display_port))
                            exclude_serial_ports.add(display_port)
                            serial_results.append((
                                display_port,
                                status.get('version') or '?',
                                status.get('core') or '?',
                                status.get('device') or '?',
                                status.get('manufacturer') or 'Unknown'
                            ))
                    except Exception:
                        pass
        except Exception:
            pass

    exclude_ports = list(exclude_serial_ports) if exclude_serial_ports else None
    scanned = DeviceScanner.scan_serial_ports(max_workers=10, exclude_ports=exclude_ports)

    for port_device, board_info in scanned:
        version, core, device, manufacturer = board_info
        port_device = _serial_port_display(port_device)
        serial_results.append((
            port_device,
            version,
            core,
            device,
            manufacturer
        ))

    def port_sort_key(item):
        port = item[0]
        match = re.search(r'(\d+)$', port)
        if match:
            return (port[:match.start()], int(match.group(1)))
        return (port, 0)
    
    seen_ports_cmp = set()
    unique_results = []
    for result in serial_results:
        port = _serial_port_display(result[0])
        key = _serial_port_cmp_key(port)
        if key not in seen_ports_cmp:
            seen_ports_cmp.add(key)
            unique_results.append((port, result[1], result[2], result[3], result[4]))
    serial_results = unique_results
    
    serial_results.sort(key=port_sort_key)

    scanned_serial_ports_cmp = set(_serial_port_cmp_key(r[0]) for r in serial_results)

    os_port_cmp = set()
    try:
        from serial.tools.list_ports import comports as _lp_comports
        for _lp in _lp_comports():
            os_port_cmp.add(_serial_port_cmp_key(_lp.device))
    except Exception:
        pass

    serial_history_data = []
    
    history_keys_cmp = set(_serial_port_cmp_key(k) for k in history_connections.keys())

    for conn_key, conn_data in history_connections.items():
        if '.' in conn_key:
            continue
        
        port_cmp = _serial_port_cmp_key(conn_key)
        if port_cmp in scanned_serial_ports_cmp:
            continue

        version = conn_data.get('version', '-') or '-'
        core = conn_data.get('core', '-') or '-'
        device = conn_data.get('device', '-') or '-'
        manufacturer = conn_data.get('manufacturer', '-') or '-'

        if port_cmp in os_port_cmp:
            serial_results.append((_serial_port_display(conn_key), version, core, device, manufacturer))
            scanned_serial_ports_cmp.add(port_cmp)
        else:
            serial_history_data.append((_serial_port_display(conn_key), version, core, device, manufacturer))
    
    serial_results.sort(key=port_sort_key)
    serial_history_data.sort(key=port_sort_key)
    
    def get_conn_marker(is_connected):
        return "[green]󱓦[/green]" if is_connected else " "
    
    def get_default_marker(conn_id, is_history=False):
        conn_cmp = _serial_port_cmp_key(conn_id)
        is_default = bool(history_default_cmp and conn_cmp == history_default_cmp)
        is_in_hist = conn_cmp in history_keys_cmp
        if is_default:
            return "[bright_yellow]󰷌[/bright_yellow]"
        elif is_in_hist or is_history:
            return "[dim][/dim]"
        return ""
    
    COLUMN_PADDING = 2
    
    max_port_width = max((len(r[0]) for r in serial_results), default=4) if serial_results else 4
    max_port_width = max(max_port_width, max((len(r[0]) for r in serial_history_data), default=4) if serial_history_data else 4)
    
    serial_table = Table(show_header=False, box=None, padding=(0, COLUMN_PADDING), collapse_padding=True, expand=False)
    serial_table.add_column("conn", no_wrap=True, width=1)
    serial_table.add_column("port", style="bright_cyan", no_wrap=True, justify="right", width=max_port_width)
    serial_table.add_column("default", no_wrap=True)
    serial_table.add_column("version", no_wrap=True)
    serial_table.add_column("core", style="bright_green", no_wrap=True)
    serial_table.add_column("device", style="bright_yellow", no_wrap=True)
    serial_table.add_column("manufacturer", style="dim", no_wrap=True, overflow="ignore")
    
    has_serial = False
    for port, version, core, device, manufacturer in serial_results:
        has_serial = True
        is_connected = _serial_port_cmp_key(port) in connected_serial_ports_cmp
        serial_table.add_row(
            get_conn_marker(is_connected),
            port,
            get_default_marker(port),
            version,
            core,
            device,
            manufacturer
        )
    
    if serial_history_data:
        for port, version, core, device, manufacturer in serial_history_data:
            is_connected = _serial_port_cmp_key(port) in connected_serial_ports_cmp
            if is_connected:
                serial_table.add_row(
                    get_conn_marker(True),
                    port,
                    get_default_marker(port, is_history=True),
                    version,
                    core,
                    device,
                    manufacturer
                )
            else:
                serial_table.add_row(
                    " ",
                    f"[dim]{_serial_port_display(port)}[/dim]",
                    get_default_marker(port, is_history=True),
                    f"[dim]{version}[/dim]",
                    f"[dim]{core}[/dim]",
                    f"[dim]{device}[/dim]",
                    f"[dim]{manufacturer}[/dim]"
                )
            has_serial = True
    
    string_io = StringIO()
    temp_console = Console(file=string_io, force_terminal=True, width=300)
    
    if has_serial:
        temp_console.print(serial_table)
        temp_console.print()
        temp_console.print("[dim]  󱓦 connected    󰷌 default[/dim]")
    else:
        temp_console.print("  [dim]No serial devices found[/dim]")
    
    output_text = string_io.getvalue().rstrip()
    
    text_content = Text.from_ansi(output_text)
    text_content.no_wrap = True
    
    OutputHelper.print_panel(
        text_content,
        title="MicroPython Devices",
        border_style="cyan",
        title_align="left"
    )


@app.command(rich_help_panel="Connectivity")
def wifi(
    args: list[str] = typer.Argument(None, help="WiFi arguments"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Manage WiFi connection on the connected device.

[bold cyan]Usage:[/bold cyan]
  replx wifi                       [dim]# Show WiFi status[/dim]
  replx wifi connect [yellow]SSID PW[/yellow]       [dim]# Connect and save config[/dim]
  replx wifi connect               [dim]# Connect using saved credentials[/dim]
  replx wifi off                   [dim]# Disable WiFi[/dim]
  replx wifi scan                  [dim]# Scan for networks[/dim]
  replx wifi boot on               [dim]# Enable auto-connect on boot[/dim]
  replx wifi boot off              [dim]# Disable auto-connect on boot[/dim]

[bold cyan]Commands:[/bold cyan]
  [yellow](no args)[/yellow]            Show current WiFi status
  [yellow]connect SSID PW[/yellow]      Connect to network and save to wifi_config.py
  [yellow]connect[/yellow]              Connect using saved wifi_config.py
  [yellow]off[/yellow]                  Disconnect and disable WiFi interface
  [yellow]scan[/yellow]                 Scan and list available networks
  [yellow]boot on[/yellow]              Add WiFi auto-connect to boot.py
  [yellow]boot off[/yellow]             Remove WiFi auto-connect from boot.py

[bold cyan]Examples:[/bold cyan]
  replx wifi                       [dim]# Check status[/dim]
  replx wifi connect Net pass123   [dim]# Connect and save[/dim]
  replx wifi connect               [dim]# Use saved credentials[/dim]
  replx wifi scan                  [dim]# Find networks[/dim]
  replx wifi boot on               [dim]# Auto-connect after reboot[/dim]

[bold cyan]Note:[/bold cyan]
  • Credentials saved to /wifi_config.py on device
  • boot on: Non-blocking (no boot delay)"""
        OutputHelper.print_panel(help_text, border_style="dim")
        console.print()
        raise typer.Exit()
    
    _ensure_connected()
    
    client = _create_agent_client()
    
    if not args:
        _wifi_status(client)
    elif args[0] == "off":
        _wifi_off(client)
    elif args[0] == "scan":
        _wifi_scan(client)
    elif args[0] == "connect":
        if len(args) >= 3:
            ssid, pw = args[1], args[2]
            _wifi_connect(client, ssid, pw)
        else:
            _wifi_connect_from_config(client)
    elif args[0] == "boot":
        if len(args) >= 2 and args[1] == "on":
            _wifi_boot_on(client)
        elif len(args) >= 2 and args[1] == "off":
            _wifi_boot_off(client)
        else:
            OutputHelper.print_panel(
                "Usage: [bright_blue]replx wifi boot on[/bright_blue] or [bright_blue]replx wifi boot off[/bright_blue]",
                title="WiFi Error",
                border_style="red"
            )
            raise typer.Exit(1)
    else:
        OutputHelper.print_panel(
            "Invalid arguments.\n\nUse [bright_blue]replx wifi --help[/bright_blue] for usage.",
            title="WiFi Error",
            border_style="red"
        )
        raise typer.Exit(1)


WIFI_STATUS_MESSAGES = {
    0: ("Idle", "dim"),
    1: ("Connecting...", "yellow"),
    2: ("Wrong password", "red"),
    3: ("AP not found", "red"),
    4: ("Connection failed", "red"),
    5: ("Got IP", "green"),
    -1: ("Connection failed", "red"),
    -2: ("No AP found", "red"),
    -3: ("Wrong password", "red"),
}

def _wifi_status(client: AgentClient):
    """Show WiFi status."""
    code = '''
import network
import json

wlan = network.WLAN(network.STA_IF)
result = {
    "active": wlan.active(),
    "connected": wlan.isconnected(),
    "status": None,
    "ifconfig": None,
    "ssid": None
}

if wlan.active():
    try:
        result["status"] = wlan.status()
    except:
        pass

if wlan.isconnected():
    result["ifconfig"] = wlan.ifconfig()
    try:
        result["ssid"] = wlan.config("essid")
    except:
        pass

print(json.dumps(result))
'''
    try:
        result = client.send_command('exec', code=code)
        output = result.get('output', '').strip()
        data = json.loads(output)
        
        lines = []
        
        if data["active"]:
            if data["connected"]:
                lines.append(f"[green]● Connected[/green]")
                if data["ssid"]:
                    lines.append(f"  SSID:     [bright_cyan]{data['ssid']}[/bright_cyan]")
                if data["ifconfig"]:
                    ip, netmask, gateway, dns = data["ifconfig"]
                    lines.append(f"  IP:       [bright_yellow]{ip}[/bright_yellow]")
                    lines.append(f"  Netmask:  {netmask}")
                    lines.append(f"  Gateway:  {gateway}")
                    lines.append(f"  DNS:      {dns}")
            else:
                status = data.get("status")
                if status is not None and status in WIFI_STATUS_MESSAGES:
                    msg, color = WIFI_STATUS_MESSAGES[status]
                    lines.append(f"[{color}]● {msg}[/{color}]")
                else:
                    lines.append(f"[yellow]● Active but not connected[/yellow]")
        else:
            lines.append(f"[dim]● WiFi disabled[/dim]")
        
        OutputHelper.print_panel(
            "\n".join(lines),
            title="WiFi Status",
            border_style="cyan"
        )
    except Exception as e:
        OutputHelper.print_panel(
            f"Failed to get WiFi status: {e}",
            title="WiFi Error",
            border_style="red"
        )
        raise typer.Exit(1)

def _wifi_get_status_message(status_code: int) -> tuple[str, str]:
    return WIFI_STATUS_MESSAGES.get(status_code, (f"Unknown status ({status_code})", "yellow"))

def _wifi_check_current_connection(client: AgentClient, target_ssid: str = None) -> dict:
    check_code = '''
import network
import json

wlan = network.WLAN(network.STA_IF)
result = {
    "active": wlan.active(),
    "connected": wlan.isconnected(),
    "ssid": None,
    "ifconfig": None
}

if wlan.isconnected():
    result["ifconfig"] = wlan.ifconfig()
    try:
        result["ssid"] = wlan.config("essid")
    except:
        pass

print(json.dumps(result))
'''
    try:
        res = client.send_command('exec', code=check_code)
        output = res.get('output', '').strip()
        data = json.loads(output)
        
        if target_ssid:
            current_ssid = data.get("ssid") or ""
            data["same_ssid"] = current_ssid.lower() == target_ssid.lower()
        
        return data
    except:
        return {"connected": False, "ssid": None, "ifconfig": None, "same_ssid": False}


def _wifi_do_connect(client: AgentClient, ssid: str, pw: str, timeout: int = 15) -> dict:
    connect_code = f'''
import network
import time
import json

wlan = network.WLAN(network.STA_IF)
wlan.active(True)

if wlan.isconnected():
    wlan.disconnect()
    time.sleep_ms(100)

wlan.connect("{ssid}", "{pw}")

timeout_ms = {timeout * 1000}
t0 = time.ticks_ms()
last_status = None
error_count = 0

while True:
    if wlan.isconnected():
        break
    
    elapsed = time.ticks_diff(time.ticks_ms(), t0)
    if elapsed > timeout_ms:
        break
    
    try:
        status = wlan.status()
        last_status = status
        if status in (3, 4, -1, -2):
            error_count += 1
            if error_count >= 3:
                break
        elif status in (2, -3):
            error_count += 1
            if error_count >= 5:
                break
        else:
            error_count = 0
    except:
        pass
    
    time.sleep_ms(200)

if not wlan.isconnected():
    for _ in range(10):
        time.sleep_ms(200)
        if wlan.isconnected():
            break

if wlan.isconnected():
    try:
        wlan.config(pm=network.WLAN.PM_NONE)
    except:
        pass

final_status = None
try:
    final_status = wlan.status()
except:
    pass

result = {{
    "connected": wlan.isconnected(),
    "ifconfig": wlan.ifconfig() if wlan.isconnected() else None,
    "status": final_status if final_status is not None else last_status
}}
print(json.dumps(result))
'''
    from rich.console import Console
    from rich.live import Live
    from rich.spinner import Spinner
    
    console = Console()
    spinner = Spinner("dots", text=Text(f" Connecting to {ssid}...", style="bright_cyan"))
    
    try:
        with Live(spinner, console=console, refresh_per_second=10, transient=True):
            result = client.send_command('exec', code=connect_code, timeout=timeout + 5.0)
        
        output = result.get('output', '').strip()
        data = None
        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('{') and line.endswith('}'):
                try:
                    data = json.loads(line)
                    if "connected" in data:
                        break
                except json.JSONDecodeError:
                    continue
        
        if data is None:
            data = {"connected": False, "ifconfig": None, "status": None}
        
        if not data.get("connected"):
            import time as pytime
            pytime.sleep(1.0)
            verify = _wifi_check_current_connection(client, target_ssid=ssid)
            if verify.get("connected") and verify.get("same_ssid"):
                return {
                    "connected": True,
                    "ifconfig": verify.get("ifconfig"),
                    "status": 5
                }
            
            status = data.get("status")
            if status is not None:
                msg, _ = _wifi_get_status_message(status)
                data["error"] = msg
            else:
                data["error"] = "Connection timeout"
        
        return data
        
    except Exception as e:
        error_str = str(e)
        json_match = re.search(r'\{[^{}]*"connected":\s*(true|false)[^{}]*\}', error_str)
        if json_match:
            try:
                data = json.loads(json_match.group())
                if data.get("connected"):
                    return data
            except json.JSONDecodeError:
                pass
        
        import time as pytime
        pytime.sleep(1.0)
        try:
            verify = _wifi_check_current_connection(client, target_ssid=ssid)
            if verify.get("connected") and verify.get("same_ssid"):
                return {
                    "connected": True,
                    "ifconfig": verify.get("ifconfig"),
                    "status": 5
                }
        except Exception:
            pass
        
        return {"connected": False, "ifconfig": None, "status": None, "error": error_str}


def _wifi_connect(client: AgentClient, ssid: str, pw: str):
    
    current = _wifi_check_current_connection(client, target_ssid=ssid)
    
    if current.get("connected") and current.get("same_ssid"):
        ip = current["ifconfig"][0] if current.get("ifconfig") else "unknown"
        
        _wifi_save_config(client, ssid, pw)
        
        OutputHelper.print_panel(
            f"[green]● Already connected to [bright_cyan]{ssid}[/bright_cyan][/green]\n\n"
            f"  IP: [bright_yellow]{ip}[/bright_yellow]",
            title="WiFi Status",
            border_style="green"
        )
        return
    
    result = _wifi_do_connect(client, ssid, pw)
    
    if result.get("connected"):
        ip = result["ifconfig"][0] if result.get("ifconfig") else "unknown"
        
        _wifi_save_config(client, ssid, pw)
        
        OutputHelper.print_panel(
            f"[green]Connected to [bright_cyan]{ssid}[/bright_cyan][/green]\n\n"
            f"  IP: [bright_yellow]{ip}[/bright_yellow]\n\n"
            f"[dim]Config saved to /wifi_config.py[/dim]",
            title="WiFi Connected",
            border_style="green"
        )
    else:
        error = result.get("error", "Unknown error")
        status = result.get("status")
        
        error_detail = f"[red]{error}[/red]"
        hints = []
        if status in (2, -3): 
            hints.append("• Check if the password is correct")
            hints.append("• Password is case-sensitive")
        elif status in (3, -2):
            hints.append("• Check if the SSID is correct")
            hints.append("• Make sure the router is powered on")
            hints.append("• Move closer to the access point")
        else:
            hints.append("• Check SSID and password")
            hints.append("• Try running [bright_blue]replx wifi scan[/bright_blue] to see available networks")
        
        hint_text = "\n".join(hints)
        
        OutputHelper.print_panel(
            f"Failed to connect to [bright_cyan]{ssid}[/bright_cyan]\n\n"
            f"  Error: {error_detail}\n\n"
            f"[dim]{hint_text}[/dim]",
            title="WiFi Connection Failed",
            border_style="red"
        )
        raise typer.Exit(1)


def _wifi_save_config(client: AgentClient, ssid: str, pw: str):
    check_code = '''
import json
try:
    from wifi_config import WIFI_SSID, WIFI_PASS
    print(json.dumps({"ssid": WIFI_SSID, "pw": WIFI_PASS}))
except:
    print(json.dumps({"ssid": None, "pw": None}))
'''
    try:
        result = client.send_command('exec', code=check_code)
        output = result.get('output', '').strip()
        existing = json.loads(output)
        
        if existing.get("ssid") == ssid and existing.get("pw") == pw:
            return
    except:
        pass

    escaped_ssid = ssid.replace('\\', '\\\\').replace('"', '\\"')
    escaped_pw = pw.replace('\\', '\\\\').replace('"', '\\"')
    
    save_code = f'''
f = open("/wifi_config.py", "w")
f.write('WIFI_SSID = "{escaped_ssid}"\\n')
f.write('WIFI_PASS = "{escaped_pw}"\\n')
f.close()
'''
    client.send_command('exec', code=save_code)


def _wifi_connect_from_config(client: AgentClient):
    read_config_code = '''
import json
try:
    from wifi_config import WIFI_SSID, WIFI_PASS
    print(json.dumps({"ssid": WIFI_SSID, "pw": WIFI_PASS}))
except ImportError:
    print(json.dumps({"error": "no_config"}))
'''
    try:
        result = client.send_command('exec', code=read_config_code)
        output = result.get('output', '').strip()
        config = json.loads(output)
        
        if config.get("error") == "no_config":
            OutputHelper.print_panel(
                "No saved credentials found.\n\n"
                "Use: [bright_blue]replx wifi connect <SSID> <PASSWORD>[/bright_blue]",
                title="WiFi Error",
                border_style="red"
            )
            raise typer.Exit(1)
        
        ssid = config.get("ssid")
        pw = config.get("pw")
        
        if not ssid:
            OutputHelper.print_panel(
                "Invalid wifi_config.py (missing SSID).\n\n"
                "Use: [bright_blue]replx wifi connect <SSID> <PASSWORD>[/bright_blue]",
                title="WiFi Error",
                border_style="red"
            )
            raise typer.Exit(1)
            
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(
            f"Failed to read wifi_config.py: {e}",
            title="WiFi Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    current = _wifi_check_current_connection(client, target_ssid=ssid)
    
    if current.get("connected") and current.get("same_ssid"):
        ip = current["ifconfig"][0] if current.get("ifconfig") else "unknown"
        OutputHelper.print_panel(
            f"[green]● Already connected to [bright_cyan]{ssid}[/bright_cyan][/green]\n\n"
            f"  IP: [bright_yellow]{ip}[/bright_yellow]",
            title="WiFi Status",
            border_style="green"
        )
        return

    result = _wifi_do_connect(client, ssid, pw)
    
    if result.get("connected"):
        ip = result["ifconfig"][0] if result.get("ifconfig") else "unknown"
        OutputHelper.print_panel(
            f"[green]Connected to [bright_cyan]{ssid}[/bright_cyan][/green]\n\n"
            f"  IP: [bright_yellow]{ip}[/bright_yellow]",
            title="WiFi Connected",
            border_style="green"
        )
    else:
        error = result.get("error", "Unknown error")
        status = result.get("status")
        
        error_detail = f"[red]{error}[/red]"
        
        hints = []
        if status in (2, -3):
            hints.append("• Check if the password is correct")
        elif status in (3, -2):
            hints.append("• Check if the SSID in wifi_config.py is correct")
            hints.append("• Make sure the router is powered on")
        else:
            hints.append("• Check SSID and password in wifi_config.py")
        
        hint_text = "\n".join(hints)
        
        OutputHelper.print_panel(
            f"Failed to connect to [bright_cyan]{ssid}[/bright_cyan]\n\n"
            f"  Error: {error_detail}\n\n"
            f"[dim]{hint_text}[/dim]",
            title="WiFi Connection Failed",
            border_style="red"
        )
        raise typer.Exit(1)


def _wifi_boot_on(client: AgentClient):
    check_code = '''
import json
try:
    from wifi_config import WIFI_SSID, WIFI_PASS
    print(json.dumps({"exists": True}))
except:
    print(json.dumps({"exists": False}))
'''
    try:
        result = client.send_command('exec', code=check_code)
        output = result.get('output', '').strip()
        data = json.loads(output)
        
        if not data.get("exists"):
            OutputHelper.print_panel(
                "No wifi_config.py found.\n\n"
                "First connect to WiFi: [bright_blue]replx wifi connect <SSID> <PW>[/bright_blue]",
                title="WiFi Error",
                border_style="red"
            )
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(
            f"Failed to check config: {e}",
            title="WiFi Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    wifi_boot_code = '''# --- replx wifi auto-connect ---
try:
    import network
    from wifi_config import WIFI_SSID, WIFI_PASS
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASS)
    wlan.config(pm=network.WLAN.PM_NONE)
except:
    pass
# --- end replx wifi ---
'''

    update_code = f'''
import json

WIFI_BLOCK_START = "# --- replx wifi auto-connect ---"
WIFI_BLOCK_END = "# --- end replx wifi ---"
WIFI_CODE = """{wifi_boot_code}"""

try:
    with open("/boot.py", "r") as f:
        content = f.read()
    
    if WIFI_BLOCK_START in content:
        print(json.dumps({{"status": "already_enabled"}}))
    else:
        with open("/boot.py", "a") as f:
            if not content.endswith("\\n"):
                f.write("\\n")
            f.write("\\n")
            f.write(WIFI_CODE)
        print(json.dumps({{"status": "added"}}))
except OSError:
    with open("/boot.py", "w") as f:
        f.write(WIFI_CODE)
    print(json.dumps({{"status": "created"}}))
'''
    try:
        result = client.send_command('exec', code=update_code)
        output = result.get('output', '').strip()
        
        data = None
        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('{'):
                try:
                    data = json.loads(line)
                    break
                except:
                    continue
        
        if data is None:
            raise ValueError("No response")
        
        status = data.get("status")
        if status == "already_enabled":
            OutputHelper.print_panel(
                "WiFi auto-connect is already enabled in boot.py",
                title="WiFi Boot",
                border_style="cyan"
            )
        elif status == "created":
            OutputHelper.print_panel(
                "[green]WiFi auto-connect enabled[/green]\n\n"
                "Created /boot.py with WiFi auto-connect.\n"
                "[dim]WiFi will connect automatically on reboot (non-blocking).[/dim]",
                title="WiFi Boot On",
                border_style="green"
            )
        else:
            OutputHelper.print_panel(
                "[green]WiFi auto-connect enabled[/green]\n\n"
                "Added WiFi auto-connect to /boot.py.\n"
                "[dim]WiFi will connect automatically on reboot (non-blocking).[/dim]",
                title="WiFi Boot On",
                border_style="green"
            )
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(
            f"Failed to update boot.py: {e}",
            title="WiFi Error",
            border_style="red"
        )
        raise typer.Exit(1)


def _wifi_boot_off(client: AgentClient):
    remove_code = '''
import json

WIFI_BLOCK_START = "# --- replx wifi auto-connect ---"
WIFI_BLOCK_END = "# --- end replx wifi ---"

try:
    with open("/boot.py", "r") as f:
        content = f.read()
    
    if WIFI_BLOCK_START not in content:
        print(json.dumps({"status": "not_found"}))
    else:
        lines = content.split("\\n")
        new_lines = []
        skip = False
        for line in lines:
            if WIFI_BLOCK_START in line:
                skip = True
                continue
            if WIFI_BLOCK_END in line:
                skip = False
                continue
            if not skip:
                new_lines.append(line)
        
        while new_lines and new_lines[-1].strip() == "":
            new_lines.pop()
        
        new_content = "\\n".join(new_lines)
        
        if new_content.strip() == "":
            import os
            os.remove("/boot.py")
            print(json.dumps({"status": "deleted"}))
        else:
            with open("/boot.py", "w") as f:
                f.write(new_content)
                if not new_content.endswith("\\n"):
                    f.write("\\n")
            print(json.dumps({"status": "removed"}))
except OSError:
    print(json.dumps({"status": "no_bootpy"}))
'''
    try:
        result = client.send_command('exec', code=remove_code)
        output = result.get('output', '').strip()
        
        data = None
        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('{'):
                try:
                    data = json.loads(line)
                    break
                except:
                    continue
        
        if data is None:
            raise ValueError("No response")
        
        status = data.get("status")
        if status == "no_bootpy":
            OutputHelper.print_panel(
                "No boot.py found. Nothing to disable.",
                title="WiFi Boot",
                border_style="dim"
            )
        elif status == "not_found":
            OutputHelper.print_panel(
                "WiFi auto-connect not found in boot.py",
                title="WiFi Boot",
                border_style="dim"
            )
        elif status == "deleted":
            OutputHelper.print_panel(
                "[green]WiFi auto-connect disabled[/green]\n\n"
                "Removed /boot.py (was empty after removal).",
                title="WiFi Boot Off",
                border_style="green"
            )
        else:  # removed
            OutputHelper.print_panel(
                "[green]WiFi auto-connect disabled[/green]\n\n"
                "Removed WiFi auto-connect from /boot.py.",
                title="WiFi Boot Off",
                border_style="green"
            )
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(
            f"Failed to update boot.py: {e}",
            title="WiFi Error",
            border_style="red"
        )
        raise typer.Exit(1)


def _wifi_off(client: AgentClient):
    code = '''
import network
wlan = network.WLAN(network.STA_IF)
if wlan.isconnected():
    wlan.disconnect()
wlan.active(False)
print("WiFi disabled")
'''
    try:
        client.send_command('exec', code=code)
        OutputHelper.print_panel(
            "WiFi interface disabled.",
            title="WiFi Off",
            border_style="dim"
        )
    except Exception as e:
        OutputHelper.print_panel(
            f"Failed to disable WiFi: {e}",
            title="WiFi Error",
            border_style="red"
        )
        raise typer.Exit(1)


def _wifi_scan(client: AgentClient):
    code = '''
import network
import json

wlan = network.WLAN(network.STA_IF)
was_active = wlan.active()
wlan.active(True)

aps = wlan.scan()
results = []
for ap in aps:
    ssid, bssid, channel, rssi, auth, hidden = ap
    auth_names = ["Open", "WEP", "WPA-PSK", "WPA2-PSK", "WPA/WPA2-PSK", "WPA2-ENT", "WPA3-PSK", "WPA2/WPA3-PSK"]
    auth_str = auth_names[auth] if auth < len(auth_names) else f"Auth{auth}"
    results.append({
        "ssid": ssid.decode() if ssid else "(hidden)",
        "rssi": rssi,
        "channel": channel,
        "auth": auth_str,
        "hidden": hidden
    })

if not was_active:
    wlan.active(False)

results.sort(key=lambda x: x["rssi"], reverse=True)
print(json.dumps(results))
'''
    try:
        result = client.send_command('exec', code=code)
        output = result.get('output', '').strip()
        
        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('['):
                data = json.loads(line)
                break
        else:
            data = []
        
        if not data:
            OutputHelper.print_panel(
                "No networks found.",
                title="WiFi Scan",
                border_style="dim"
            )
            return
        
        table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
        table.add_column("SSID", style="bright_white", width=32)
        table.add_column("Signal", justify="right", width=8)
        table.add_column("", justify="left", width=2)
        table.add_column("Ch", justify="center", width=4)
        table.add_column("Security", width=14)
        
        for ap in data:
            ssid_display = ap["ssid"] if ap["ssid"] else "[dim](hidden)[/dim]"
            table.add_row(
                ssid_display,
                _signal_str(ap["rssi"]),
                _signal_icon(ap["rssi"]),
                str(ap["channel"]),
                ap["auth"]
            )
        
        console = Console(width=CONSOLE_WIDTH)
        console.print(Panel(
            table,
            title=f"WiFi Networks ({len(data)} found)",
            border_style="cyan",
            box=get_panel_box(),
            width=CONSOLE_WIDTH
        ))
    except Exception as e:
        OutputHelper.print_panel(
            f"Failed to scan: {e}",
            title="WiFi Scan Error",
            border_style="red"
        )
        raise typer.Exit(1)


def _signal_icon(rssi: int) -> str:
    if rssi >= -50:
        icon, color = chr(0xF0928), "green"
    elif rssi >= -65:
        icon, color = chr(0xF0925), "green"
    elif rssi >= -75:
        icon, color = chr(0xF0922), "yellow"
    elif rssi >= -85:
        icon, color = chr(0xF091F), "yellow"
    else:
        icon, color = chr(0xF092F), "red"
    return f"[{color}]{icon}[/{color}]"


def _signal_str(rssi: int) -> str:
    """Return a colored dBm string (no icon)."""
    if rssi >= -65:
        color = "green"
    elif rssi >= -75:
        color = "yellow"
    else:
        color = "red"
    return f"[{color}]{rssi}dBm[/{color}]"


def _ble_is_bt_addr(s: str) -> bool:
    return bool(re.match(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$', s))


def _ble_parse_uuid_arg(uuid_arg: str) -> dict:
    s = uuid_arg.strip()
    if s.startswith('#'):
        try:
            return {'type': 'handle', 'value': int(s[1:])}
        except ValueError:
            raise ValueError(f"Invalid handle: {s!r}. Use #<number> e.g. #9")
    if '-' in s:
        return {'type': 'full', 'value': s.lower()}
    if re.match(r'^[0-9a-fA-F]{1,8}$', s):
        return {'type': 'suffix', 'value': s.lower()}
    raise ValueError(
        f"Invalid UUID argument: {s!r}\n"
        "Use #handle (e.g. #9), suffix (e.g. 0002), or full UUID (with hyphens)"
    )


def _ble_parse_write_value(value_str: str) -> bytes:
    if value_str.startswith('b') and len(value_str) > 1:
        rest = value_str[1:]
        if re.match(r'^[0-9a-fA-F]+$', rest) and len(rest) % 2 == 0:
            return bytes.fromhex(rest)
    return value_str.encode('utf-8')


def _mp_preamble() -> str:
    return (
        "import bluetooth as _bt, json, time\n"
        "from micropython import const\n"
        "_IS=const(5);_ID=const(6);_IC=const(7);_IX=const(8)\n"
        "_ISD=const(10);_ICR=const(11);_ICE=const(12)\n"
        "_IRR=const(15);_IWE=const(17);_INO=const(18)\n"
        "def _as(b): return ':'.join('%02X'%x for x in bytes(b))\n"
        "def _dn(d):\n"
        " i=0\n"
        " while i+1<len(d):\n"
        "  l=d[i];t=d[i+1];s=i+2;e=s+l-1\n"
        "  if l==0 or e>len(d): break\n"
        "  if t in(8,9):\n"
        "   try: return d[s:e].decode()\n"
        "   except: return ''\n"
        "  i+=1+l\n"
        " return ''\n"
        "def _us(u):\n"
        " s=str(u)\n"
        " return s[6:-2] if s.startswith(\"UUID('\") else s\n"
        "def _wt(fn,ms):\n"
        " t=time.ticks_ms()\n"
        " while not fn():\n"
        "  if time.ticks_diff(time.ticks_ms(),t)>=ms: return False\n"
        "  time.sleep_ms(50)\n"
        " return True\n"
        "ble=_bt.BLE();ble.active(True)\n"
    )


def _mp_resolve(target: str, addr_type: int, scan_ms: int) -> str:
    if _ble_is_bt_addr(target):
        addr_bytes = [int(x, 16) for x in target.split(':')]
        at = max(addr_type, 0)
        return f"tat={at};taddr=bytes({addr_bytes});tname={json.dumps(target)}\n"
    else:
        return (
            f"_tn={json.dumps(target)};_sd=[False];_tf=[None]\n"
            "def _si(e,d):\n"
            " if e==_IS:\n"
            "  at,addr,adt,rssi,adv=d;n=_dn(bytes(adv))\n"
            "  if n==_tn and _tf[0] is None:\n"
            "   _tf[0]=(at,bytes(addr));ble.gap_scan(None);_sd[0]=True\n"
            " elif e==_ID:_sd[0]=True\n"
            "ble.irq(_si)\n"
            f"ble.gap_scan({scan_ms},30000,30000,True)\n"
            f"_wt(lambda:_sd[0],{scan_ms+3000})\n"
            "ble.gap_scan(None)\n"
            "if _tf[0] is None:\n"
            " print(json.dumps({'error':'not found: '+_tn}));ble.active(False);raise SystemExit()\n"
            "tat,taddr=_tf[0];tname=_tn\n"
        )


def _mp_connect_discover(need_discover: bool = True) -> str:
    code = (
        "ch=[None];co=[False];sdd=[False];cdd=[False];chars=[]\n"
        "def _ci(e,d):\n"
        " if e==_IC:\n"
        "  h,at,addr=d\n"
        "  if bytes(addr)==taddr:ch[0]=h;co[0]=True\n"
        " elif e==_IX:\n"
        "  h,at,addr=d\n"
        "  if ch[0]==h:co[0]=False;ch[0]=None\n"
        " elif e==_ISD:\n"
        "  h,st=d\n"
        "  if h==ch[0]:sdd[0]=True\n"
        " elif e==_ICR:\n"
        "  h,dh,vh,pr,uuid=d\n"
        "  if h==ch[0]:chars.append((dh,vh,pr,uuid))\n"
        " elif e==_ICE:\n"
        "  h,st=d\n"
        "  if h==ch[0]:cdd[0]=True\n"
        "ble.irq(_ci)\n"
        "ble.gap_connect(tat,taddr)\n"
        "if not _wt(lambda:co[0],7000):\n"
        " print(json.dumps({'error':'connect failed'}));ble.active(False);raise SystemExit()\n"
    )
    if need_discover:
        code += (
            "ble.gattc_discover_services(ch[0])\n"
            "_wt(lambda:sdd[0],5000)\n"
            "ble.gattc_discover_characteristics(ch[0],1,0xffff)\n"
            "_wt(lambda:cdd[0],5000)\n"
        )
    return code


def _mp_find_handle(uuid_info: dict) -> str:
    if uuid_info['type'] == 'handle':
        return f"vh={uuid_info['value']};vh_def=None;vh_props=0xff\n"
    elif uuid_info['type'] == 'suffix':
        sfx = uuid_info['value']
        return (
            "vh=None;vh_def=None;vh_props=0\n"
            "for dh,v,pr,u in chars:\n"
            f" if _us(u).replace('-','').lower().endswith('{sfx}'):\n"
            "  vh=v;vh_def=dh;vh_props=pr;break\n"
            "if vh is None:\n"
            f" print(json.dumps({{'error':'char not found: {sfx}'}}));"
            "ble.gap_disconnect(ch[0]);_wt(lambda:not co[0],3000);ble.active(False);raise SystemExit()\n"
        )
    else:
        uuid_str = uuid_info['value']
        return (
            f"_tu=_bt.UUID('{uuid_str}')\n"
            "vh=None;vh_def=None;vh_props=0\n"
            "for dh,v,pr,u in chars:\n"
            " if u==_tu:vh=v;vh_def=dh;vh_props=pr;break\n"
            "if vh is None:\n"
            f" print(json.dumps({{'error':'char not found: {uuid_str}'}}));"
            "ble.gap_disconnect(ch[0]);_wt(lambda:not co[0],3000);ble.active(False);raise SystemExit()\n"
        )


def _mp_disconnect() -> str:
    return (
        "if ch[0] is not None:ble.gap_disconnect(ch[0]);_wt(lambda:not co[0],3000)\n"
        "ble.active(False)\n"
    )


def _ble_parse_json_output(output: str) -> dict:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith('{') or line.startswith('['):
            return json.loads(line)
    raise RuntimeError(f"No JSON in output: {output!r}")


def _ble_scan(client: 'AgentClient', scan_sec: int = 5) -> None:
    scan_ms = scan_sec * 1000
    code = _mp_preamble() + (
        f"found={{}};done=[False]\n"
        "def irq(e,d):\n"
        " if e==_IS:\n"
        "  at,addr,adt,rssi,adv=d;k=bytes(addr);n=_dn(bytes(adv))\n"
        "  ex=found.get(k)\n"
        "  if ex:\n"
        "   if n and not ex['name']:ex['name']=n\n"
        "   if int(rssi)>ex['rssi']:ex['rssi']=int(rssi)\n"
        "  else:found[k]={'at':at,'addr':_as(addr),'rssi':int(rssi),'name':n}\n"
        " elif e==_ID:done[0]=True\n"
        "ble.irq(irq)\n"
        f"ble.gap_scan({scan_ms},30000,30000,True)\n"
        f"_wt(lambda:done[0],{scan_ms+3000})\n"
        "ble.gap_scan(None)\n"
        "out=sorted(found.values(),key=lambda x:-x['rssi'])\n"
        "print(json.dumps(out))\n"
        "ble.active(False)\n"
    )
    from rich.live import Live
    from rich.spinner import Spinner
    try:
        _scan_console = Console()
        _spinner = Spinner("dots", text=Text(f" Scanning for BLE devices ({scan_sec}s)...", style="bright_cyan"))
        with Live(_spinner, console=_scan_console, refresh_per_second=10, transient=True):
            result = client.send_command('exec', code=code, timeout=scan_sec + 8)
        if result.get('error'):
            raise RuntimeError(result['error'])
        data = _ble_parse_json_output(result.get('output', ''))
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected response: {data}")

        if not data:
            OutputHelper.print_panel("No BLE devices found.", title="BLE Scan", border_style="yellow")
            return

        table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
        table.add_column("Address", style="bright_white", width=18)
        table.add_column("Name", width=24)
        table.add_column("AddrType", justify="center", width=9)
        table.add_column("Signal", justify="right", width=8)
        table.add_column("", justify="left", width=2)
        for entry in data:
            name = entry['name'] or "[dim](no-name)[/dim]"
            addr_type_str = "[bright_green]public[/bright_green]" if entry['at'] == 0 else "[bright_yellow]random[/bright_yellow]"
            table.add_row(
                entry['addr'], name,
                addr_type_str,
                _signal_str(entry['rssi']),
                _signal_icon(entry['rssi'])
            )
        console = Console(width=CONSOLE_WIDTH)
        console.print(Panel(
            table,
            title=f"BLE Scan  ({len(data)} devices, {scan_sec}s)",
            border_style="cyan", box=get_panel_box(), width=CONSOLE_WIDTH
        ))
    except Exception as e:
        OutputHelper.print_panel(f"BLE scan failed: {e}", title="BLE Error", border_style="red")
        raise typer.Exit(1)


def _ble_info(client: 'AgentClient', target: str, addr_type: int = -1, scan_sec: int = 5) -> None:
    scan_ms = scan_sec * 1000
    code = (
        _mp_preamble()
        + _mp_resolve(target, addr_type, scan_ms)
        + _mp_connect_discover(True)
        + (
            "out=[]\n"
            "for dh,vh,pr,u in chars:\n"
            " pn=[n for b,n in[(1,'broadcast'),(2,'read'),(4,'write_no_resp'),(8,'write'),(16,'notify'),(32,'indicate')]if pr&b]\n"
            " out.append({'def_h':dh,'val_h':vh,'props':pr,'pnames':pn,'uuid':_us(u)})\n"
            "print(json.dumps({'name':tname,'chars':out}))\n"
        )
        + _mp_disconnect()
    )
    try:
        result = client.send_command('exec', code=code, timeout=scan_sec + 25)
        if result.get('error'):
            raise RuntimeError(result['error'])
        data = _ble_parse_json_output(result.get('output', ''))
        if 'error' in data:
            raise RuntimeError(data['error'])

        table = Table(show_header=True, header_style="bold cyan", box=get_panel_box(), expand=True)
        table.add_column("Handle", width=7, justify="right")
        table.add_column("Def", width=5, justify="right")
        table.add_column("UUID")
        table.add_column("Properties", width=36)
        for ch in data['chars']:
            props = ", ".join(ch['pnames']) if ch['pnames'] else "—"
            table.add_row(str(ch['val_h']), str(ch['def_h']), ch['uuid'], props)
        console = Console(width=CONSOLE_WIDTH)
        console.print(Panel(
            table,
            title=f"BLE Info: {data['name']}  ({len(data['chars'])} characteristics)",
            border_style="cyan", box=get_panel_box(), width=CONSOLE_WIDTH
        ))
    except Exception as e:
        OutputHelper.print_panel(f"BLE info failed: {e}", title="BLE Error", border_style="red")
        raise typer.Exit(1)


def _ble_read(client: 'AgentClient', target: str, uuid_info: dict,
              addr_type: int = -1, scan_sec: int = 5) -> None:
    scan_ms = scan_sec * 1000
    need_disc = uuid_info['type'] != 'handle'
    code = (
        _mp_preamble()
        + _mp_resolve(target, addr_type, scan_ms)
        + _mp_connect_discover(need_disc)
        + _mp_find_handle(uuid_info)
        + (
            "rd=[None];rdd=[False]\n"
            "def _ri(e,d):\n"
            " if e==_IRR:\n"
            "  h,v,data=d\n"
            "  if h==ch[0] and v==vh:rd[0]=bytes(data);rdd[0]=True\n"
            " elif e==_IC or e==_IX:_ci(e,d)\n"
            "ble.irq(_ri)\n"
            "ble.gattc_read(ch[0],vh)\n"
            "_wt(lambda:rdd[0],5000)\n"
            "if rd[0] is None:\n"
            " print(json.dumps({'error':'read failed or timeout'}))\n"
            "else:\n"
            " try:print(json.dumps({'value_str':rd[0].decode('utf-8')}))\n"
            " except:\n"
            "  import binascii\n"
            "  print(json.dumps({'value_hex':binascii.hexlify(rd[0]).decode()}))\n"
        )
        + _mp_disconnect()
    )
    try:
        result = client.send_command('exec', code=code, timeout=scan_sec + 25)
        if result.get('error'):
            raise RuntimeError(result['error'])
        data = _ble_parse_json_output(result.get('output', ''))
        if 'error' in data:
            raise RuntimeError(data['error'])
        if 'value_str' in data:
            OutputHelper.print_panel(data['value_str'], title="BLE Read", border_style="cyan")
        else:
            OutputHelper.print_panel(data['value_hex'], title="BLE Read (hex)", border_style="cyan")
    except Exception as e:
        OutputHelper.print_panel(f"BLE read failed: {e}", title="BLE Error", border_style="red")
        raise typer.Exit(1)


def _ble_write(client: 'AgentClient', target: str, uuid_info: dict, value_bytes: bytes,
               addr_type: int = -1, scan_sec: int = 5) -> None:
    scan_ms = scan_sec * 1000
    need_disc = uuid_info['type'] != 'handle'
    val_list = list(value_bytes)
    code = (
        _mp_preamble()
        + _mp_resolve(target, addr_type, scan_ms)
        + _mp_connect_discover(need_disc)
        + _mp_find_handle(uuid_info)
        + (
            "from micropython import const as _c\n"
            "_PW=_c(0x08);_PWN=_c(0x04)\n"
            "cw=vh_props&_PW;cwn=vh_props&_PWN\n"
            "if not cw and not cwn:\n"
            " print(json.dumps({'error':'not writable props=0x{:02x}'.format(vh_props)}));"
            "ble.gap_disconnect(ch[0]);_wt(lambda:not co[0],3000);ble.active(False);raise SystemExit()\n"
            f"_dat=bytes({val_list})\n"
            "wd=[False];ws=[None]\n"
            "def _wi(e,d):\n"
            " if e==_IWE:\n"
            "  h,v,st=d\n"
            "  if h==ch[0]:ws[0]=st;wd[0]=True\n"
            " elif e==_IC or e==_IX:_ci(e,d)\n"
            "ble.irq(_wi)\n"
            "use_resp=1 if cw else 0\n"
            "ble.gattc_write(ch[0],vh,_dat,use_resp)\n"
            "if use_resp:\n"
            " _wt(lambda:wd[0],5000)\n"
            " st=ws[0] if ws[0] is not None else -1\n"
            " print(json.dumps({'ok':st==0,'status':st,'with_response':True}))\n"
            "else:\n"
            " time.sleep_ms(200)\n"
            " print(json.dumps({'ok':True,'status':0,'with_response':False}))\n"
        )
        + _mp_disconnect()
    )
    try:
        result = client.send_command('exec', code=code, timeout=scan_sec + 25)
        if result.get('error'):
            raise RuntimeError(result['error'])
        data = _ble_parse_json_output(result.get('output', ''))
        if 'error' in data:
            raise RuntimeError(data['error'])
        if data['ok']:
            resp_label = "with response" if data.get('with_response') else "no response"
            OutputHelper.print_panel(
                f"Write OK  ({resp_label})\nData: {value_bytes!r}",
                title="BLE Write", border_style="green"
            )
        else:
            OutputHelper.print_panel(
                f"Write failed (status={data['status']})",
                title="BLE Write", border_style="red"
            )
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(f"BLE write failed: {e}", title="BLE Error", border_style="red")
        raise typer.Exit(1)


def _ble_notify(target: str, uuid_info: dict, notify_sec: int = 10,
                addr_type: int = -1, scan_sec: int = 5) -> None:
    import signal as _signal
    scan_ms = scan_sec * 1000
    notify_ms = notify_sec * 1000
    need_disc = uuid_info['type'] != 'handle'
    code = (
        _mp_preamble()
        + _mp_resolve(target, addr_type, scan_ms)
        + _mp_connect_discover(need_disc)
        + _mp_find_handle(uuid_info)
        + (
            "cccd_h=vh+1;ncount=[0]\n"
            "def _ni(e,d):\n"
            " if e==_IWE:\n"
            "  h,v,st=d\n"
            "  if h==ch[0]:print(json.dumps({'cccd_ok':st==0,'status':st}))\n"
            " elif e==_INO:\n"
            "  h,v,data=d\n"
            "  if h==ch[0] and v==vh:\n"
            "   ncount[0]+=1\n"
            "   try:val=bytes(data).decode('utf-8')\n"
            "   except:\n"
            "    import binascii;val=binascii.hexlify(bytes(data)).decode()\n"
            "   print(json.dumps({'n':ncount[0],'v':val}))\n"
            " elif e==_IC or e==_IX:_ci(e,d)\n"
            "ble.irq(_ni)\n"
            "ble.gattc_write(ch[0],cccd_h,bytes([1,0]),1)\n"
            f"_st=time.ticks_ms()\n"
            f"while time.ticks_diff(time.ticks_ms(),_st)<{notify_ms}:time.sleep_ms(100)\n"
            "print(json.dumps({'done':True,'total':ncount[0]}))\n"
        )
        + _mp_disconnect()
    )

    OutputHelper.print_panel(
        f"Target: [bright_cyan]{target}[/bright_cyan]  Duration: [yellow]{notify_sec}s[/yellow]\n\n"
        "Press [yellow]Ctrl+C[/yellow] to stop early.",
        title="BLE Notify — Streaming",
        border_style="cyan"
    )

    stop_requested = [False]

    def output_callback(data: bytes, stream_type: str = 'stdout') -> None:
        text = data.decode('utf-8', errors='replace')
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if 'n' in msg:
                    print(f"  [{msg['n']:3d}]  {msg['v']}")
                elif 'cccd_ok' in msg:
                    if msg['cccd_ok']:
                        print("  CCCD enabled, waiting for notifications...")
                    else:
                        print(f"  CCCD write failed (status={msg['status']})")
                elif 'done' in msg:
                    print(f"\n  Total notifications: {msg['total']}")
                elif 'error' in msg:
                    print(f"  Error: {msg['error']}")
            except Exception:
                print(f"  {line}")

    client = _create_agent_client()
    try:
        original_sigint = _signal.getsignal(_signal.SIGINT)

        def _sigint(sig, frame):
            stop_requested[0] = True

        _signal.signal(_signal.SIGINT, _sigint)
        try:
            client.run_interactive(
                script_content=code,
                echo=False,
                output_callback=output_callback,
                stop_check=lambda: stop_requested[0]
            )
        except Exception as e:
            if not stop_requested[0]:
                OutputHelper.print_panel(f"BLE notify failed: {e}", title="BLE Error", border_style="red")
                raise typer.Exit(1)
        finally:
            _signal.signal(_signal.SIGINT, original_sigint)
    finally:
        try:
            client.__exit__(None, None, None)
        except Exception:
            pass


@app.command(rich_help_panel="Connectivity")
def ble(
    args: list[str] = typer.Argument(None, help="BLE arguments"),
    time_opt: int = typer.Option(0, "--time", "-t", help="Duration in seconds (scan default:5, notify default:10)"),
    addr_type: int = typer.Option(-1, "--addr-type", help="BT address type: 0=public 1=random -1=auto(default)"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    if show_help or not args:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
BLE (Bluetooth Low Energy) operations via the connected device.

[bold cyan]Usage:[/bold cyan]
  replx ble scan [--time SEC]
  replx ble info  {NAME|ADDR}
  replx ble read  {NAME|ADDR} {UUID|SUFFIX|#HANDLE}
  replx ble write {NAME|ADDR} {UUID|SUFFIX|#HANDLE} VALUE
  replx ble notify {NAME|ADDR} {UUID|SUFFIX|#HANDLE} [--time SEC]

[bold cyan]UUID argument formats:[/bold cyan]
  [yellow]#9[/yellow]                    Handle number from 'ble info' (skip discovery)
  [yellow]0002[/yellow]                  UUID suffix — last hex digits of UUID
  [yellow]A1B2C3D4-...-0002[/yellow]    Full 128-bit UUID

[bold cyan]Write value formats:[/bold cyan]
  [yellow]"hello"[/yellow]               String → UTF-8 bytes
  [yellow]b68656c6c6f[/yellow]           Hex bytes (prefix b + even-length hex)

[bold cyan]Options:[/bold cyan]
  [yellow]--time SEC[/yellow]       Scan duration (default 5s) or notify duration (default 10s)
  [yellow]--addr-type N[/yellow]    BT address type 0=public 1=random, default=-1 (auto via scan)

[bold cyan]Examples:[/bold cyan]
  replx ble scan
  replx ble scan --time 10
  replx ble info "Pico2W-BLE"
  replx ble read "Pico2W-BLE" 0002
  replx ble read "Pico2W-BLE" #9
  replx ble write "Pico2W-BLE" 0003 "hello"
  replx ble write "Pico2W-BLE" 0003 b68656c6c6f
  replx ble notify "Pico2W-BLE" 0004 --time 10
  replx ble notify "Pico2W-BLE" #13

[bold cyan]Workflow:[/bold cyan]
  1. replx ble scan               [dim]# discover nearby devices[/dim]
  2. replx ble info "DeviceName"  [dim]# see handles and UUIDs[/dim]
  3. replx ble read/write/notify  [dim]# use suffix or #handle[/dim]"""
        OutputHelper.print_panel(help_text, border_style="dim")
        console.print()
        raise typer.Exit()

    subcmd = args[0]

    if subcmd == 'scan':
        _ensure_connected()
        with _create_agent_client() as client:
            _ble_scan(client, scan_sec=time_opt if time_opt > 0 else 5)
        return

    if subcmd not in ('info', 'read', 'write', 'notify'):
        OutputHelper.print_panel(
            f"Unknown subcommand: [red]{subcmd}[/red]\n\n"
            "Use [bright_blue]replx ble --help[/bright_blue] for usage.",
            title="BLE Error", border_style="red"
        )
        raise typer.Exit(1)

    if len(args) < 2:
        OutputHelper.print_panel(
            f"[yellow]replx ble {subcmd}[/yellow] requires a target (NAME or ADDR).",
            title="BLE Error", border_style="red"
        )
        raise typer.Exit(1)

    target = args[1]
    _ensure_connected()

    if subcmd == 'info':
        with _create_agent_client() as client:
            _ble_info(client, target, addr_type, scan_sec=time_opt if time_opt > 0 else 5)
        return

    if len(args) < 3:
        OutputHelper.print_panel(
            f"[yellow]replx ble {subcmd}[/yellow] requires a UUID/handle argument.\n\n"
            "Run [bright_blue]replx ble info {target}[/bright_blue] to list available handles.",
            title="BLE Error", border_style="red"
        )
        raise typer.Exit(1)

    try:
        uuid_info = _ble_parse_uuid_arg(args[2])
    except ValueError as e:
        OutputHelper.print_panel(str(e), title="BLE Error", border_style="red")
        raise typer.Exit(1)

    if subcmd == 'read':
        with _create_agent_client() as client:
            _ble_read(client, target, uuid_info, addr_type, scan_sec=time_opt if time_opt > 0 else 5)
        return

    if subcmd == 'write':
        if len(args) < 4:
            OutputHelper.print_panel(
                "[yellow]replx ble write[/yellow] requires a value argument.\n\n"
                "String: [yellow]\"hello\"[/yellow]   Hex bytes: [yellow]b68656c6c6f[/yellow]",
                title="BLE Error", border_style="red"
            )
            raise typer.Exit(1)
        try:
            value_bytes = _ble_parse_write_value(args[3])
        except Exception as e:
            OutputHelper.print_panel(f"Invalid write value: {e}", title="BLE Error", border_style="red")
            raise typer.Exit(1)
        with _create_agent_client() as client:
            _ble_write(client, target, uuid_info, value_bytes, addr_type, scan_sec=time_opt if time_opt > 0 else 5)
        return

    if subcmd == 'notify':
        notify_sec = time_opt if time_opt > 0 else 10
        _ble_notify(target, uuid_info, notify_sec, addr_type, scan_sec=5)
        return
