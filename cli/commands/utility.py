import os
import re
import time
import threading
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.spinner import Spinner

from replx import __version__
from replx.utils import device_name_to_path
from ..agent.client import AgentClient, get_cached_session_id
from ..helpers import (
    OutputHelper, StoreManager,
    get_panel_box, CONSOLE_WIDTH, set_global_context
)
from ..config import (
    STATE, DEFAULT_AGENT_PORT,
    _resolve_connection, _get_global_options,
    _find_env_file, _get_default_connection
)
from ..connection import (
    _ensure_connected, _create_agent_client,
    _get_current_agent_port
)
from ..app import app
from .package import _install_spec_internal


def _port_sort_key(port: str) -> tuple:
    """Stable sort key for ports.

    - Sorts by trailing number when present (COM24 -> 24)
    - Falls back to lexicographic
    """
    if port is None:
        return ("", -1)
    p = str(port).strip()
    m = re.search(r"(\d+)$", p)
    if m:
        return (p[: m.start()].lower(), int(m.group(1)))
    return (p.lower(), 0)


def _sorted_unique_ports(ports: list[str | None]) -> list[str]:
    seen = set()
    out: list[str] = []
    for p in ports:
        if not p:
            continue
        s = str(p).strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    out.sort(key=_port_sort_key)
    return out


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _get_connection_info_for_port(connections: dict, port: Optional[str]) -> dict:
    """Return connection info dict for a port.

    On Windows, COM ports are case-insensitive, but different call sites may
    preserve different casing (e.g. 'com24' vs 'COM24'). This lookup makes
    status/session display resilient to that.
    """
    if not port or not connections:
        return {}

    if port in connections:
        return connections.get(port, {}) or {}

    if _is_windows():
        upper = port.upper()
        if upper in connections:
            return connections.get(upper, {}) or {}
        lower = port.lower()
        if lower in connections:
            return connections.get(lower, {}) or {}

        lower_key = lower
        for key, value in connections.items():
            if isinstance(key, str) and key.lower() == lower_key:
                return value or {}

    return {}


@app.command(name="version", hidden=True)
def version_cmd(
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Show replx version information.
    
    Alias: replx -v
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Show replx version information.

[bold cyan]Usage:[/bold cyan]
  replx version
  replx -v                [dim]# Short alias[/dim]

[bold cyan]Examples:[/bold cyan]
  replx version           [dim]# Show version[/dim]
  replx -v                [dim]# Same as above[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    OutputHelper.print_panel(
        f"replx [green]{__version__}[/green]",
        title="Version",
        border_style="cyan"
    )



def _get_session_list_data():
    """
    Get all sessions data for display.
    Returns tuple of (sessions_data, current_ppid, error).
    
    sessions_data structure:
    {
        'sessions': [
            {
                'ppid': int,
                'foreground': str or None,
                'backgrounds': [str],
                'is_current': bool  # True if this is current terminal's session
            }
        ],
        'connections': {
            'COM19': {'version': '1.24.1', 'core': 'RP2350', 'device': 'ticle', 'manufacturer': 'Raspberry Pi'}
        }
    }
    """
    try:
        with AgentClient(port=_get_current_agent_port()) as client:
            session_info = client.send_command('session_info', timeout=1.5)
    except Exception:
        return None, None, "no_server"
    
    current_ppid = get_cached_session_id()
    
    sessions = []
    for session in session_info.get('sessions', []):
        sessions.append({
            'ppid': session.get('ppid'),
            'foreground': session.get('foreground'),
            'backgrounds': session.get('backgrounds', []),
            'is_current': session.get('ppid') == current_ppid,
            'default_port': session.get('default_port')
        })
    
    connections = {}
    for conn in session_info.get('connections', []):
        port = conn.get('port')
        if port:
            connections[port] = {
                'version': conn.get('version', '?'),
                'core': conn.get('core', '?'),
                'device': conn.get('device', '?'),
                'manufacturer': conn.get('manufacturer', ''),
                'busy': conn.get('busy', False)
            }
    
    if not sessions:
        return None, current_ppid, "no_sessions"
    
    total_connections = 0
    for sess in sessions:
        if sess.get('foreground'):
            total_connections += 1
        total_connections += len(sess.get('backgrounds', []))
    
    if total_connections == 0:
        return None, current_ppid, "no_sessions"
    
    current_session = None
    for sess in sessions:
        if sess.get('is_current'):
            current_session = sess
            break
    
    current_session_empty = (
        current_session is None or 
        (not current_session.get('foreground') and not current_session.get('backgrounds'))
    )
    
    return {
        'sessions': sessions,
        'connections': connections,
        'current_session_empty': current_session_empty
    }, current_ppid, None


def _num_to_bracket(n: int) -> str:
    """
    Convert number to bracketed format for consistent terminal display.
    Returns: " [1]", "[12]", etc. (4 chars, right-aligned)
    """
    return f"[{n}]".rjust(4)


def _print_session_list_interactive(sessions_data, current_ppid):
    """
    Print formatted multi-session list with interactive selection using Rich Table.
    
    Shows ALL sessions with their fg/bg connections, grouped by session.
    Current session is highlighted with brackets.
    User can select any connection (from any session) to set as current session's fg.
    
    Returns: (selected_port, action) - port to promote or action like 'stop'
    """
    sessions = sessions_data['sessions']
    connections = sessions_data['connections']
    
    color_map = ["yellow", "green", "blue"]
    
    sessions_sorted = sorted(sessions, key=lambda s: (not s.get('is_current'), s.get('ppid', 0)))
    
    current_fg = None
    for sess in sessions:
        if sess.get('is_current') and sess.get('foreground'):
            current_fg = sess['foreground']
            break
    
    all_ports = list(connections.keys())
    all_versions = [c.get('version', '?') for c in connections.values()]
    all_cores = [c.get('core', '?') for c in connections.values()]
    all_devices = [c.get('device', '?') for c in connections.values()]
    
    PORT_W = max((len(p) for p in all_ports), default=4)
    STATUS_W = 4
    VER_W = max((len(v) for v in all_versions), default=6)
    CORE_W = max((len(c) for c in all_cores), default=6)
    DEV_W = max((len(d) for d in all_devices), default=6)
    
    content_lines = []
    selectable_map = {}
    select_num = 1
    row_idx = 0
    
    numbered_ports = set()
    if current_fg:
        numbered_ports.add(current_fg)
    
    def make_row(selector, port, version, core, device, manufacturer, color, dimmed=False):
        """Build a row with proper alignment."""
        if dimmed:
            return f"{selector:>4}  {port:>{PORT_W}}  {version:<{VER_W}}  {core:<{CORE_W}}  {device:<{DEV_W}}  {manufacturer}"
        else:
            return f"{selector:>4}  [{color}]{port:>{PORT_W}}[/{color}]  {version:<{VER_W}}  {core:<{CORE_W}}  [{color}]{device:<{DEV_W}}[/{color}]  [dim]{manufacturer}[/dim]"
    
    for sess in sessions_sorted:
        ppid = sess.get('ppid')
        is_current = sess.get('is_current', False)
        fg_port = sess.get('foreground')
        bg_ports = sess.get('backgrounds', [])
        
        if is_current:
            content_lines.append(f"[bold bright_cyan]\\[SID: {ppid}][/bold bright_cyan]")
        else:
            content_lines.append(f"[dim]SID: {ppid}[/dim]")
        
        # Sort all ports within a session so output is deterministic and matches scan.
        ordered_ports = _sorted_unique_ports(([fg_port] if fg_port else []) + list(bg_ports or []))

        if ordered_ports:
            conn_info = _get_connection_info_for_port(connections, fg_port)
            for p in ordered_ports:
                is_fg = bool(fg_port and p == fg_port)
                conn_info = _get_connection_info_for_port(connections, p)
                version = conn_info.get('version', '?')
                core = conn_info.get('core', '?')
                device = conn_info.get('device', '?')
                manufacturer = conn_info.get('manufacturer', '')
                is_busy = conn_info.get('busy', False)
                status = 'busy' if is_busy else 'idle'
                status_color = 'red' if is_busy else 'green'

                color = color_map[row_idx % len(color_map)]
                row_idx += 1

                p_disp = OutputHelper.format_port(p)

                if is_current and is_fg:
                    selector = f"[{color}] 󱓥  [/{color}]"
                    line = f"{selector}  [{color}]{p_disp:>{PORT_W}}[/{color}]  [{status_color}]{status:<{STATUS_W}}[/{status_color}]  {version:<{VER_W}}  {core:<{CORE_W}}  [{color}]{device:<{DEV_W}}[/{color}]  [dim]{manufacturer}[/dim]"
                    content_lines.append(line)
                elif p not in numbered_ports:
                    bracket = _num_to_bracket(select_num)
                    selectable_map[select_num] = p
                    numbered_ports.add(p)
                    select_num += 1
                    selector = f"[bright_cyan]{bracket}[/bright_cyan]"
                    icon = "󱓥" if is_fg else " "
                    line = f"{selector}  [{color}]{p_disp:>{PORT_W}}[/{color}]  [{status_color}]{status:<{STATUS_W}}[/{status_color}]  {version:<{VER_W}}  {core:<{CORE_W}}  [{color}]{device:<{DEV_W}}[/{color}]  [dim]{manufacturer}[/dim]"
                    if icon and is_fg and is_current:
                        # current FG already has 󱓥 row above; keep simple
                        pass
                    content_lines.append(line)
                else:
                    selector = "[dim] 󰌹  [/dim]"
                    line = f"{selector}  [dim]{p_disp:>{PORT_W}}  {status:<{STATUS_W}}  {version:<{VER_W}}  {core:<{CORE_W}}  {device:<{DEV_W}}  {manufacturer}[/dim]"
                    content_lines.append(line)
        
        content_lines.append("")
    
    content_lines.append("[dim] 󱓥 foreground, 󰌹 duplicate[/dim]")
    
    OutputHelper.print_panel(
        "\n".join(content_lines),
        title="Sessions",
        border_style="cyan"
    )
    
    if not selectable_map:
        return None, None
    
    console = Console(width=CONSOLE_WIDTH)
    max_num = max(selectable_map.keys())
    prompt = f"\n[bright_cyan]\\[1-{max_num}] switch, \\[Enter] quit: [/bright_cyan]"
    
    while True:
        try:
            choice = console.input(prompt)
            choice = choice.strip().lower()
            
            if choice == 'q' or choice == '':
                return None, None
            
            try:
                num = int(choice)
                if num in selectable_map:
                    return selectable_map[num], None
                else:
                    console.print(f"[red]Invalid selection: {num}[/red]")
            except ValueError:
                console.print(f"[red]Invalid input: {choice}[/red]")
        except KeyboardInterrupt:
            console.print()
            return None, None


def _print_session_list_status(sessions_data, current_ppid):
    """
    Print formatted session list (status only, no interactive selection).
    Uses column-based coloring consistent with scan output.
    """
    from rich.table import Table
    from rich.console import Console
    from io import StringIO
    from rich.text import Text
    from rich.panel import Panel
    from ..helpers import CONSOLE_WIDTH
    from ..helpers.output import get_panel_box
    
    sessions = sessions_data['sessions']
    connections = sessions_data['connections']
    
    all_defaults = set()
    for sess in sessions:
        if sess.get('default_port'):
            all_defaults.add(sess['default_port'])

    def _port_key(p: str) -> str:
        if not p:
            return ""
        s = str(p).strip()
        return s.lower() if _is_windows() else s

    all_defaults_keyed = {_port_key(p) for p in all_defaults}
    
    sessions_sorted = sorted(sessions, key=lambda s: (not s.get('is_current'), s.get('ppid', 0)))
    
    COLUMN_PADDING = 2
    
    string_io = StringIO()
    temp_console = Console(file=string_io, force_terminal=True, width=300)
    
    def get_conn_marker(is_foreground, is_current):
        if not is_foreground:
            return ""
        return "[green]󱓥[/green]" if is_current else "[dim]󱓥[/dim]"
    
    def get_default_marker(port, is_current):
        is_default = _port_key(port) in all_defaults_keyed
        if not is_default:
            return ""
        return "[bright_yellow]󰷌[/bright_yellow]" if is_current else "[dim]󰷌[/dim]"
    
    for sess in sessions_sorted:
        ppid = sess.get('ppid')
        is_current = sess.get('is_current', False)
        fg_port = sess.get('foreground')
        bg_ports = sess.get('backgrounds', [])
        
        if is_current:
            temp_console.print(f"[bold bright_cyan]\\[SID: {ppid}][/bold bright_cyan]")
        else:
            temp_console.print(f"[dim]SID: {ppid}[/dim]")
        
        session_table = Table(show_header=False, box=None, padding=(0, COLUMN_PADDING), collapse_padding=True, expand=False)
        session_table.add_column("conn", no_wrap=True)
        session_table.add_column("port", no_wrap=True, justify="right")
        session_table.add_column("default", no_wrap=True)
        session_table.add_column("status", no_wrap=True)
        session_table.add_column("version", no_wrap=True)
        session_table.add_column("core", no_wrap=True)
        session_table.add_column("device", no_wrap=True)
        session_table.add_column("manufacturer", no_wrap=True, overflow="ignore")
        
        has_connections = False
        
        ordered_ports = _sorted_unique_ports(([fg_port] if fg_port else []) + list(bg_ports or []))

        for p in ordered_ports:
            has_connections = True
            is_fg = bool(fg_port and p == fg_port)
            conn_info = _get_connection_info_for_port(connections, p)
            version = conn_info.get('version', '?')
            core = conn_info.get('core', '?')
            device = conn_info.get('device', '?')
            manufacturer = conn_info.get('manufacturer', '')
            is_busy = conn_info.get('busy', False)
            if is_current:
                status_text = Text("busy", style="red") if is_busy else Text("idle", style="green")
                version_text = Text(version)
                core_text = Text(core, style="bright_green")
                device_text = Text(device, style="bright_yellow")
                manufacturer_text = Text(manufacturer, style="dim")
                port_text = Text(OutputHelper.format_port(p), style="bright_cyan")
            else:
                status_text = Text("busy", style="dim red") if is_busy else Text("idle", style="dim green")
                version_text = Text(version, style="dim")
                core_text = Text(core, style="dim")
                device_text = Text(device, style="dim")
                manufacturer_text = Text(manufacturer, style="dim")
                port_text = Text(OutputHelper.format_port(p), style="dim")

            session_table.add_row(
                get_conn_marker(is_fg, is_current),
                port_text,
                get_default_marker(p, is_current),
                status_text,
                version_text,
                core_text,
                device_text,
                manufacturer_text
            )
        
        if has_connections:
            temp_console.print(session_table)
        
        temp_console.print()
    
    temp_console.print("[dim] 󱓥 foreground  󰷌 default[/dim]")
    
    output_text = string_io.getvalue().rstrip()
    text_content = Text.from_ansi(output_text)
    text_content.no_wrap = True
    
    panel = Panel(
        text_content,
        title="Sessions",
        title_align="left",
        border_style="cyan",
        box=get_panel_box(),
        width=CONSOLE_WIDTH
    )
    Console(width=CONSOLE_WIDTH).print(panel)


@app.command(rich_help_panel="Connection & Session")
def status(
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Show all sessions and connections.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Show all active sessions and their board connections.

Displays a table of all terminal sessions with their connected boards.

[bold cyan]Usage:[/bold cyan]
  replx status

[bold cyan]What you'll see:[/bold cyan]
  [bold]Columns:[/bold]
    • [yellow]Session[/yellow]  - Terminal session identifier (current in [bright_white]brackets[/bright_white])
    • [yellow]FG[/yellow]       - Foreground connection (primary board)
    • [yellow]BG[/yellow]       - Background connections (secondary boards)
    • [yellow]Default[/yellow]  - Workspace default (󰷌 marked)

  [bold]Status icons:[/bold]
    󱓥  Serial connection (COM port)

[bold cyan]Example output:[/bold cyan]
    Session      FG           BG         Default
    [1234]       󱓥 COM19     COM3       󰷌 COM19
     5678        󱓥 COM3

[bold cyan]Understanding sessions:[/bold cyan]
  • Each terminal window has its own session
  • Sessions can connect to multiple boards
  • FG (foreground) is used by default for commands
  • BG (background) boards are available for switching

[bold cyan]Note:[/bold cyan]
  This command ignores -p/--port option.

[bold cyan]Related:[/bold cyan]
  replx fg COM3         [dim]# Switch FG to different board[/dim]
  replx disconnect      [dim]# Release current FG connection[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    sessions_data, current_ppid, error = _get_session_list_data()
    
    if error == "no_server":
        OutputHelper.print_panel(
            "Agent is not running. No active connections.\n\n"
            "Start by running a command like:\n"
            "  [bright_green]replx ls[/bright_green]\n"
            "  [bright_green]replx exec \"print('hello')\"[/bright_green]",
            title="No Connections",
            border_style="dim"
        )
        raise typer.Exit()
    
    if error == "no_sessions":
        OutputHelper.print_panel(
            "No active connections.\n\n"
            "Connect to a board with:\n"
            "  [bright_green]replx ls[/bright_green]\n"
            "  [bright_green]replx --port COM19 setup[/bright_green]",
            title="No Connections",
            border_style="dim"
        )
        raise typer.Exit()
    
    _print_session_list_status(sessions_data, current_ppid)


@app.command(rich_help_panel="Connection & Session")
def fg(
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Switch foreground connection to another board.

When you have multiple boards connected, use this to switch between them.

[bold cyan]Usage:[/bold cyan]
  replx fg                    [dim]# Interactive: select from list[/dim]
  replx [cyan]PORT[/cyan] fg                [dim]# Direct: switch to PORT[/dim]

[bold cyan]Examples:[/bold cyan]
  replx fg                    [dim]# Show menu to pick a board[/dim]
  replx COM19 fg              [dim]# Switch to COM19[/dim]
  replx COM3 fg               [dim]# Switch to COM3[/dim]

[bold cyan]What happens:[/bold cyan]
  • The specified board becomes the foreground connection
  • All subsequent commands (ls, run, cat, etc.) use this board
  • Previous foreground moves to background (still connected)
  • If board isn't connected yet, it will be connected

[bold cyan]Interactive mode (no arguments):[/bold cyan]
  • Shows list of all connected boards
  • Use arrow keys to select
  • Press Enter to switch

[bold cyan]Related:[/bold cyan]
  replx status            [dim]# See all active connections[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    global_opts = _get_global_options()
    port = global_opts.get('port')
    
    if port:
        switch_target = port

        # Windows: agent connection keys are case-sensitive, but COM ports are
        # not. Resolve `com4` -> `COM4` (or whatever the agent registered) by
        # looking up current connections.
        if sys.platform.startswith("win"):
            sessions_data, _current_ppid, _error = _get_session_list_data()
            if sessions_data and not _error:
                needle = str(switch_target).strip().lower()
                for key in sessions_data.get('connections', {}).keys():
                    if isinstance(key, str) and key.strip().lower() == needle:
                        switch_target = key
                        break
        try:
            with AgentClient(port=_get_current_agent_port()) as client:
                result = client.send_command('session_switch_fg', port=switch_target, timeout=3.0)
            
            if result.get('success'):
                OutputHelper.print_panel(
                    f"Switched foreground to [bright_green]{OutputHelper.format_port(switch_target)}[/bright_green]",
                    title="Foreground Switched",
                    border_style="green"
                )
            else:
                OutputHelper.print_panel(
                    f"Failed to switch foreground: {result.get('error', 'Unknown error')}",
                    title="Error",
                    title_align="left",
                    border_style="red"
                )
        except Exception as e:
            OutputHelper.print_panel(
                f"Failed to switch foreground: {str(e)}",
                title="Error",
                title_align="left",
                border_style="red"
            )
        return
    
    sessions_data, current_ppid, error = _get_session_list_data()
    
    if error == "no_server":
        OutputHelper.print_panel(
            "Agent is not running. No active connections.",
            title="No Connections",
            border_style="dim"
        )
        raise typer.Exit()
    
    if error == "no_sessions":
        OutputHelper.print_panel(
            "No active connections.",
            title="No Connections",
            border_style="dim"
        )
        raise typer.Exit()
    
    selected_port, action = _print_session_list_interactive(sessions_data, current_ppid)
    
    if selected_port:
        try:
            with AgentClient(port=_get_current_agent_port()) as client:
                result = client.send_command('session_switch_fg', port=selected_port, timeout=3.0)
            
            if result.get('success'):
                OutputHelper.print_panel(
                    f"Switched foreground to [bright_green]{OutputHelper.format_port(selected_port)}[/bright_green]",
                    title="Foreground Switched",
                    border_style="green"
                )
            else:
                OutputHelper.print_panel(
                    f"Failed to switch foreground: {result.get('error', 'Unknown error')}",
                    title="Error",
                    title_align="left",
                    border_style="red"
                )
        except Exception as e:
            OutputHelper.print_panel(
                f"Failed to switch foreground: {str(e)}",
                title="Error",
                title_align="left",
                border_style="red"
            )


@app.command(rich_help_panel="Connection & Session")
def whoami(
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Show current foreground connection.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Show which board your commands are currently talking to.

Quickly check your active foreground connection.

[bold cyan]Usage:[/bold cyan]
  replx whoami

[bold cyan]Output shows:[/bold cyan]
  • Serial port
  • MicroPython version
  • Chip type (RP2350, ESP32, etc.)
  • Device name
  • Manufacturer

[bold cyan]Example output:[/bold cyan]
  COM19  1.24.1  RP2350  ticle  Raspberry Pi

[bold cyan]Note:[/bold cyan]
  This command ignores -p/--port option.
  It always shows the current session's foreground.

[bold cyan]Related:[/bold cyan]
  replx status            [dim]# See all active connections[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    sessions_data, current_ppid, error = _get_session_list_data()
    
    if error == "no_server":
        OutputHelper.print_panel(
            "Not connected.",
            title="No Connection",
            border_style="dim"
        )
        raise typer.Exit()
    
    if error == "no_sessions":
        OutputHelper.print_panel(
            "Not connected.",
            title="No Connection",
            border_style="dim"
        )
        raise typer.Exit()
    
    current_session = None
    for sess in sessions_data['sessions']:
        if sess.get('is_current'):
            current_session = sess
            break
    
    if not current_session or not current_session.get('foreground'):
        OutputHelper.print_panel(
            "No foreground connection in current session.",
            title="No Foreground",
            border_style="dim"
        )
        raise typer.Exit()
    
    fg_port = current_session['foreground']
    conn_info = sessions_data['connections'].get(fg_port, {})
    
    version = conn_info.get('version', '?')
    core = conn_info.get('core', '?')
    device = conn_info.get('device', '?')
    manufacturer = conn_info.get('manufacturer', '')
    
    OutputHelper.print_panel(
        f"[green]󱓥[/green]  [bright_cyan]{OutputHelper.format_port(fg_port)}[/bright_cyan]  {version}  [bright_green]{core}[/bright_green]  [bright_yellow]{device}[/bright_yellow]  [dim]{manufacturer}[/dim]",
        title="Foreground",
        border_style="green"
    )


@app.command(rich_help_panel="Connection & Session")
def disconnect(
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Release connection from session.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Close a board connection.

The connection is closed and removed from ALL sessions that reference it.

[bold cyan]Usage:[/bold cyan]
  replx disconnect              [dim]# Close foreground connection[/dim]
  replx COM3 disconnect         [dim]# Close specific serial connection[/dim]

[bold cyan]What happens:[/bold cyan]
  • The board connection is closed
  • Port becomes available for other programs
  • The CONN is removed from ALL sessions (not just current)
  • Agent server keeps running (fast reconnect later)

[bold cyan]When to use:[/bold cyan]
  • Before using another tool like Thonny or esptool
  • To release the connection without stopping everything
  • When done with a specific board

[bold cyan]Note:[/bold cyan]
  • Next replx command will auto-reconnect if workspace has saved connection
  • For full cleanup (stop agent too), use [yellow]replx shutdown[/yellow]

[bold cyan]Related:[/bold cyan]
  replx shutdown          [dim]# Stop ALL connections and agent[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    global_opts = _get_global_options()
    port = global_opts.get('port')
    
    if not port:
        sessions_data, current_ppid, error = _get_session_list_data()
        
        if error or not sessions_data:
            OutputHelper.print_panel(
                "Not connected to any device.",
                title="No Connection",
                border_style="dim"
            )
            raise typer.Exit()
        
        for sess in sessions_data['sessions']:
            if sess.get('is_current') and sess.get('foreground'):
                port = sess['foreground']
                break
    
    if not port:
        OutputHelper.print_panel(
            "No foreground connection to disconnect.",
            title="No Connection",
            border_style="dim"
        )
        raise typer.Exit()
    
    try:
        with AgentClient(port=_get_current_agent_port()) as client:
            result = client.send_command('session_disconnect', port=port, timeout=3.0)
        
        if result.get('freed_port'):
            OutputHelper.print_panel(
                f"Disconnected [bright_blue]{OutputHelper.format_port(port)}[/bright_blue]",
                title="Disconnected",
                border_style="blue"
            )
        else:
            OutputHelper.print_panel(
                f"Failed to disconnect: {result.get('error', 'Unknown error')}",
                title="Error",
                title_align="left",
                border_style="red"
            )
    except Exception as e:
        OutputHelper.print_panel(
            f"Failed to disconnect: {str(e)}",
            title="Error",
            title_align="left",
            border_style="red"
        )


def _do_shutdown():
    """
    Stop all connections and the agent.
    """
    agent_port = _get_current_agent_port()
    
    try:
        AgentClient.stop_agent(port=agent_port)
        
        OutputHelper.print_panel(
            "Stopped all connections and agent.\n"
            "[dim]Run any replx command to reconnect.[/dim]",
            title="Shutdown Complete",
            border_style="blue"
        )
    except Exception as e:
        # Agent might not be running or other error
        OutputHelper.print_panel(
            "Agent is not running or already stopped.",
            title="Shutdown",
            border_style="dim"
        )


@app.command(rich_help_panel="Connection & Session")
def shutdown(
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Stop all connections and agent.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Completely stop the replx agent and release all connections.

This is the "full cleanup" command - stops everything.

[bold cyan]Usage:[/bold cyan]
  replx shutdown

[bold cyan]What happens:[/bold cyan]
  • All board connections are closed (all sessions)
  • All ports/sockets are released
  • The background agent process is terminated
  • You'll need to reconnect on next command

[bold cyan]When to use:[/bold cyan]
  • Before physically disconnecting boards
  • To free all resources when done working
  • If the agent seems stuck or behaving oddly
  • Before running system updates

[bold cyan]Note:[/bold cyan]
  • Next replx command will automatically restart the agent
  • Startup takes slightly longer after shutdown
  • To release just one connection, use [yellow]replx disconnect[/yellow]

[bold cyan]Related:[/bold cyan]
  replx disconnect        [dim]# Release only one connection[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    _do_shutdown()


@app.command(rich_help_panel="Device Management")
def format(
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Format the file system of the connected device.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Format (erase) the filesystem on the connected device.

[bold yellow]⚠ WARNING: This deletes ALL files on the device![/bold yellow]

[bold cyan]Usage:[/bold cyan]
  replx format

[bold cyan]What happens:[/bold cyan]
  • All files are deleted (boot.py, main.py, /lib, everything)
  • Filesystem is reformatted to empty state
  • Device may reset automatically after format

[bold cyan]When to use:[/bold cyan]
  • To start fresh with a clean device
  • If filesystem is corrupted
  • Before selling/giving away a device
  • To free up all storage space

[bold cyan]Before formatting:[/bold cyan]
  Back up any important files:
  replx get /*.py ./backup
  replx get /lib ./backup

[bold cyan]After formatting:[/bold cyan]
  • Device has empty filesystem
  • Reinstall libraries: [green]replx pkg update[/green]
  • Or use [green]replx init[/green] instead (format + install)

[bold cyan]Related:[/bold cyan]
  replx init              [dim]# Format AND install libraries[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    _ensure_connected()
    
    global _is_stop_spinner

    _is_stop_spinner = False
    frame_idx = [0]
    
    def _spinner_task(live):
        try:
            while not _is_stop_spinner:
                live.update(OutputHelper.create_spinner_panel(
                    f"Formatting file system on {STATE.device}...",
                    title="Format File System",
                    frame_idx=frame_idx[0]
                ))
                frame_idx[0] += 1
                time.sleep(0.1)
        except Exception:
            pass
    
    ret = None
    error = None
    
    try:
        with Live(OutputHelper.create_spinner_panel(
            f"Formatting file system on {STATE.device}...",
            title="Format File System",
            frame_idx=0
        ), console=OutputHelper._console, refresh_per_second=10) as live:
            spinner_thread = threading.Thread(target=_spinner_task, args=(live,), daemon=True)
            spinner_thread.start()
            
            try:
                client = _create_agent_client()
                result = client.send_command('format')
                ret = result.get('formatted', True)
            except Exception as e:
                error = e
            finally:
                _is_stop_spinner = True
                spinner_thread.join(timeout=1.0)
    
    except KeyboardInterrupt:
        _is_stop_spinner = True
        OutputHelper.print_panel(
            "Format operation cancelled by user.",
            title="Format Cancelled",
            border_style="red"
        )
        return False
    
    if error:
        OutputHelper.print_panel(
            f"Format failed: [red]{error}[/red]",
            title="Format Failed",
            border_style="red"
        )
        return False
    
    if ret:
        if STATE.core == 'EFR32MG':
            try:
                time.sleep(1.0)
                
                client = _create_agent_client()
                global_opts = _get_global_options()
                client.send_command('connect', port=global_opts.get('port'))
            except Exception:
                pass
        
        OutputHelper.print_panel(
            f"File system on [bright_yellow]{STATE.device}[/bright_yellow] has been formatted successfully.",
            title="Format Complete",
            border_style="green"
        )
    else:
        OutputHelper.print_panel(
            f"Device [red]{STATE.device}[/red] does not support formatting.",
            title="Format Failed",
            border_style="red"
        )
    return ret



@app.command(rich_help_panel="Device Management")
def init(
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Initialize the device by formatting and installing libraries.
    
    This command combines 'format' and 'install' operations:
    1. Format the device file system
    2. Install core and device libraries
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Completely reset device: format filesystem and install all libraries.

[bold yellow]⚠ WARNING: This deletes ALL files and reinstalls from scratch![/bold yellow]

[bold cyan]Usage:[/bold cyan]
  replx init

[bold cyan]What happens:[/bold cyan]
  1. [yellow]Format:[/yellow] Erases entire filesystem
  2. [yellow]Install:[/yellow] Installs core libraries for your chip
  3. [yellow]Install:[/yellow] Installs device-specific libraries

[bold cyan]When to use:[/bold cyan]
  • First-time setup of a new device
  • Resetting device to known working state
  • After MicroPython firmware update
  • When libraries are corrupted

[bold cyan]Before init:[/bold cyan]
  Back up any custom code:
  replx get /*.py ./backup

[bold cyan]After init:[/bold cyan]
  • Device has fresh libraries installed
  • Ready for your code: [green]replx put main.py /[/green]
  • Or set up workspace: [green]replx setup[/green]

[bold cyan]Equivalent to:[/bold cyan]
  replx pkg download      [dim]# Download libraries (if needed)[/dim]
  replx format            [dim]# Erase device storage[/dim]
  replx pkg update        [dim]# Install to device[/dim]

[bold cyan]Related:[/bold cyan]
  replx format            [dim]# Just erase (no install)[/dim]
  replx pkg update        [dim]# Just install (no format)[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    _ensure_connected()
    
    StoreManager.ensure_home_store()
    
    meta_path = StoreManager.local_meta_path()
    if not os.path.isfile(meta_path):
        OutputHelper.print_panel(
            "[red]Local store is not ready.[/red]\n\n"
            "The package registry is missing. Please run:\n"
            "  [cyan]replx pkg download[/cyan]\n\n"
            "This will download the required libraries to local store.",
            title="Initialization Failed",
            border_style="red"
        )
        raise typer.Exit(1)
    
    core_src = os.path.join(StoreManager.pkg_root(), "core", STATE.core, "src")
    if not os.path.isdir(core_src):
        OutputHelper.print_panel(
            f"[red]Core library for '{STATE.core}' not found.[/red]\n\n"
            "The core library is required for initialization. Please run:\n"
            "  [cyan]replx pkg download[/cyan]\n\n"
            "This will download the required libraries to local store.",
            title="Initialization Failed",
            border_style="red"
        )
        raise typer.Exit(1)
    
    def format_bytes(b):
        if b < 1024:
            return f"{b}B"
        elif b < 1024 * 1024:
            return f"{b/1024:.1f}KB"
        else:
            return f"{b/(1024*1024):.1f}MB"
    
    console = Console(width=CONSOLE_WIDTH)
    
    def make_format_panel(message: str, title: str = "Formatting") -> Panel:
        """Create a panel with spinner animation for format step."""
        spinner = Spinner("dots", text=f" {message}")
        return Panel(spinner, title=title, title_align="left", border_style="cyan", 
                     box=get_panel_box(), width=CONSOLE_WIDTH)
    
    try:
        with Live(make_format_panel("Formatting file system..."), 
                  console=console, refresh_per_second=10) as live:
            client = _create_agent_client()
            result = client.send_command('format')
            format_result = result.get('formatted', True)
            
            if not format_result:
                live.stop()
                OutputHelper.print_panel(
                    "Initialization failed: Format operation was unsuccessful.",
                    title="Initialization Failed",
                    border_style="red"
                )
                return False
            
            live.update(make_format_panel("Reconnecting to device..."))
            
            global_opts = _get_global_options()
            explicit_port = global_opts.get('port')
            
            conn = _resolve_connection(explicit_port)
            if not conn:
                raise RuntimeError("No connection configuration found")
            
            agent_port = conn.get('agent_port', DEFAULT_AGENT_PORT)
            port = conn['connection']
            core = conn.get('core') or STATE.core
            device = conn.get('device') or STATE.device
            
            client = AgentClient(port=agent_port)
            
            needs_reconnect = (core == "EFR32MG")
            
            if needs_reconnect:
                was_foreground = True
                try:
                    session_info = client.send_command('session_info', timeout=1.0)
                    current_ppid = get_cached_session_id()
                    for sess in session_info.get('sessions', []):
                        if sess.get('ppid') == current_ppid:
                            fg_port = sess.get('foreground')
                            if fg_port:
                                was_foreground = (fg_port == port)
                            else:
                                bg_ports = sess.get('backgrounds', [])
                                was_foreground = not (port and port in bg_ports)
                            break
                except Exception:
                    pass
                try:
                    client.send_command('disconnect_port', port=port)
                except Exception:
                    pass
                
                time.sleep(3.0)
                
                live.update(make_format_panel("Reconnecting..."))
                if not AgentClient.is_agent_running(port=agent_port):
                    AgentClient.start_agent(port=agent_port)
                    time.sleep(0.5)
                
                live.update(make_format_panel("Verifying connection..."))
                last_error = None
                for attempt in range(3):
                    try:
                        client = AgentClient(port=agent_port, device_port=port if port else None)
                        resp = client.send_command('session_setup', port=port,
                                                   core=core, device=device, as_foreground=was_foreground)
                        if resp.get('connected'):
                            STATE.core = resp.get('core', core)
                            STATE.device = resp.get('device', device)
                            STATE.device_root_fs = resp.get('device_root_fs', STATE.device_root_fs)
                            set_global_context(STATE.core, STATE.device, STATE.version, STATE.device_root_fs, STATE.device_path)
                            break
                        last_error = RuntimeError(f"Reconnect failed: {resp}")
                    except Exception as e:
                        last_error = e
                    
                    if attempt < 2:
                        time.sleep(2.0)
                else:
                    raise last_error or RuntimeError("Reconnect failed after 3 attempts")
            
            live.update(Panel(
                f"[green]✓[/green] File system on [bright_yellow]{STATE.device}[/bright_yellow] formatted successfully.",
                title="Format Complete", title_align="left", border_style="green", 
                box=get_panel_box(), width=CONSOLE_WIDTH
            ))
    
    except Exception as e:
        OutputHelper.print_panel(
            f"Format failed: [red]{e}[/red]",
            title="Format Failed",
            border_style="red"
        )
        return False
    
    install_stats = {}
    
    try:
        specs_to_install = ["core/"]
        
        dev_src = os.path.join(StoreManager.pkg_root(), "device", device_name_to_path(STATE.device), "src")
        if os.path.isdir(dev_src):
            specs_to_install.append("device/")
        
        from rich.panel import Panel as RichPanel
        initial_panel = RichPanel(
            "Preparing installation...",
            title="Installing", title_align="left", border_style="cyan",
            box=get_panel_box(), width=CONSOLE_WIDTH
        )
        
        with Live(initial_panel, console=console, refresh_per_second=10) as live:
            for spec_item in specs_to_install:
                result = _install_spec_internal(spec_item, live=live, update_callback=lambda p: live.update(p))
                if result:
                    install_stats[spec_item] = result
            
            summary_parts = []
            for spec_key in ["core/", "device/"]:
                if spec_key in install_stats:
                    stats = install_stats[spec_key]
                    label = spec_key.rstrip("/")
                    summary_parts.append(f"🔧 {label}/ {stats['files']} file(s) {format_bytes(stats['bytes'])}")
            
            summary_line = "    ".join(summary_parts)
            live.update(RichPanel(
                summary_line,
                title="Installation Complete", title_align="left", border_style="green",
                box=get_panel_box(), width=CONSOLE_WIDTH
            ))
        
    except Exception as e:
        OutputHelper.print_panel(
            f"Initialization failed during install: [red]{e}[/red]",
            title="Initialization Failed",
            border_style="red"
        )
        return False
    
    OutputHelper.print_panel(
        f"Device [bright_yellow]{STATE.device}[/bright_yellow] has been initialized successfully.\n"
        "The device is now ready to use.",
        title="Complete",
        border_style="green"
    )
    return True


