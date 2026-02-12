"""
Connection management utilities for CLI commands.

This module provides connection-related functionality used across CLI commands:
- Agent client creation and management
- Connection establishment and validation
- Error handling for connection issues
"""

import os
import sys
from typing import Optional

import typer

from .agent.client import AgentClient, get_cached_session_id
from .helpers import OutputHelper, DeviceScanner, set_global_context
from .config import (
    STATE,
    DEFAULT_AGENT_PORT,
    _find_env_file, _find_or_create_vscode_dir,
    _read_env_ini,
    _update_connection_config, _get_default_connection,
    _resolve_connection,
    _get_global_options,
)


def _auto_detect_port() -> Optional[str]:
    """Auto-detect the first available MicroPython REPL port."""
    results = DeviceScanner.scan_serial_ports(max_workers=5)
    if results:
        return results[0][0]
    return None


def _print_auto_connect_info(port: str, version: str, core: str, device: str, manufacturer: str = ""):
    """
    Print auto-connect notification when automatically connecting to default board.
    Similar to 'replx whoami' output but with different title.
    """
    OutputHelper.print_panel(
        f"[bright_green]{port}[/bright_green]  {version}  {core}  [bright_green]{device}[/bright_green]  [dim]{manufacturer}[/dim]",
        title="Auto-connected",
        border_style="dim"
    )


def _handle_connection_error(e: Exception, port: str = None, stop_agent: bool = False):
    """
    Handle connection errors by showing appropriate message.
    
    Args:
        e: The exception that occurred
        port: Serial port for connection
        stop_agent: If True, stop the agent (only for critical errors like fg connection failure)
    """
    # Build connection info string
    conn_info = port if port else "unknown"
    
    # Stop agent only if explicitly requested (e.g., fg connection failure when no other connections)
    if stop_agent:
        try:
            AgentClient.stop_agent()
        except Exception:
            pass
    
    # Build detailed error message
    error_detail = str(e)
    if error_detail:
        error_msg = (
            f"Connection failure on configured device ([bright_blue]{conn_info}[/bright_blue]).\n\n"
            f"[yellow]Error details:[/yellow] {error_detail}\n\n"
            "Please check:\n"
            "  • Device is powered on and connected\n"
            "  • Serial cable is properly attached\n"
            "  • Port is not in use by another program (PuTTY, Arduino IDE, etc.)\n\n"
            "[dim]Run 'replx --port PORT setup' to reconfigure if needed.[/dim]"
        )
    else:
        error_msg = (
            f"Connection failure on configured device ([bright_blue]{conn_info}[/bright_blue]).\n\n"
            "Please check:\n"
            "  • Device is powered on and connected\n"
            "  • Serial cable is properly attached\n"
            "  • Port is not in use by another program (PuTTY, Arduino IDE, etc.)\n\n"
            "[dim]Run 'replx --port PORT setup' to reconfigure if needed.[/dim]"
        )
    
    OutputHelper.print_panel(
        error_msg,
        title="Connection Error",
        border_style="red"
    )


def _ensure_connected(ctx: typer.Context = None) -> dict:
    """
    Ensure agent is running and connected before executing command.
    
    Session fg/bg policy (excluding scan/session, setup is a separate policy):
    1. Session Available = fg CONN available
    2. No Session Available + CONN omitted → default → fg, fg work
    3. No Session Available + CONN → CONN → fg, fg work
    4. Session Available + CONN omitted → fg work
    5. Session Available + CONN → CONN → bg, bg work
    
    Returns:
        dict with connection status from agent
    """
    # Get global options
    global_opts = _get_global_options()
    explicit_port = global_opts.get('port')
    global_agent_port = global_opts.get('agent_port')
    
    # Get env file and default
    env_path = _find_env_file()
    default_conn = _get_default_connection(env_path) if env_path else None
    
    # Determine agent port
    if global_agent_port:
        agent_port = global_agent_port
    elif env_path and default_conn:
        env_data = _read_env_ini(env_path)
        connections = env_data.get('connections', {})
        if default_conn in connections:
            agent_port = connections[default_conn].get('agent_port', DEFAULT_AGENT_PORT)
        else:
            agent_port = DEFAULT_AGENT_PORT
    else:
        agent_port = DEFAULT_AGENT_PORT
    
    # Case: Server NOT running
    if not AgentClient.is_agent_running(port=agent_port):
        # Determine what to connect
        if not explicit_port:
            if not default_conn:
                OutputHelper.print_panel(
                    "No default connection configured.\n\n"
                    "Run [bright_blue]replx --port PORT setup[/bright_blue] first.\n\n"
                    "Examples:\n"
                    "  [bright_green]replx --port COM3 setup[/bright_green]\n"
                    "  [bright_green]replx --port auto setup[/bright_green]",
                    title="Setup Required",
                    border_style="red"
                )
                raise typer.Exit(1)
            
            conn = _resolve_connection(default_conn)
        else:
            conn = _resolve_connection(explicit_port)
        
        if not conn:
            OutputHelper.print_panel(
                "No connection configuration found.\n\n"
                "Run [bright_blue]replx --port PORT setup[/bright_blue] first.",
                title="Setup Required",
                border_style="red"
            )
            raise typer.Exit(1)
        
        # Start agent
        try:
            AgentClient.start_agent(port=agent_port)
            
            if default_conn:
                with AgentClient(port=agent_port) as client:
                    client.send_command('set_default', port=default_conn, timeout=1.0)
        except Exception as e:
            OutputHelper.print_panel(f"Failed to start agent: {str(e)}", title="Agent Error", title_align="left", border_style="red")
            raise typer.Exit(1)
        
        # Connect fg (Serial only)
        try:
            port_arg = conn['connection']
            
            with AgentClient(port=agent_port) as client:
                result = client.send_command(
                    'session_setup',
                    port=port_arg,
                    core=conn.get('core') or "RP2350",
                    device=conn.get('device'),
                    as_foreground=True,
                    set_default=False,
                    local_default=default_conn,  # Pass workspace default to server
                )
            
            # Notify user if connection was auto-switched
            if result.get('switched_from'):
                OutputHelper.print_panel(
                    f"Auto-disconnected [yellow]{result['switched_from']}[/yellow] (same board)",
                    title="Connection Switched",
                    border_style="dim"
                )
            
            STATE.core = result.get('core', conn.get('core', ''))
            STATE.device = result.get('device', conn.get('device', 'unknown'))
            STATE.version = result.get('version', '?')
            STATE.manufacturer = result.get('manufacturer', '')
            STATE.device_root_fs = result.get('device_root_fs', '/')
            
            # Show auto-connect info when auto-connecting (server was not running)
            _print_auto_connect_info(
                conn['connection'],
                STATE.version,
                STATE.core,
                STATE.device,
                STATE.manufacturer
            )
            
        except Exception as e:
            _handle_connection_error(e, port=port_arg, stop_agent=True)
            raise typer.Exit(1)
        
        # Update .replx config
        if conn['source'] == 'global':
            is_first_connection = env_path is None or default_conn is None
            if not env_path:
                vscode_dir = _find_or_create_vscode_dir()
                env_path = os.path.join(vscode_dir, '.replx')
            _update_connection_config(
                env_path, conn['connection'],
                version=STATE.version, core=STATE.core, device=STATE.device,
                manufacturer=STATE.manufacturer,
                set_default=is_first_connection
            )
        
        # Set global context for helper functions (e.g., InstallHelper)
        set_global_context(STATE.core, STATE.device, STATE.version, STATE.device_root_fs, STATE.device_path)
        
        with AgentClient(port=agent_port, device_port=explicit_port) as client:
            status = client.send_command('status')
        return status
    
    # Case: Server IS running
    try:
        with AgentClient(port=agent_port) as client:
            status = client.send_command('status')
            session_info = client.send_command('session_info', timeout=1.0)

        def _port_norm(p: Optional[str]) -> str:
            if not p:
                return ""
            if sys.platform.startswith("win"):
                return p.upper()
            return p
        
        current_ppid = get_cached_session_id()
        current_fg = None
        current_bgs = []
        
        for session in session_info.get('sessions', []):
            if session.get('ppid') == current_ppid:
                current_fg = session.get('foreground')
                current_bgs = session.get('backgrounds', [])
                break
        
        if not current_fg:
            # No foreground connection for this session (No Session)
            if not explicit_port:
                # Rules 2: No Session + CONN omitted → default → fg
                if not default_conn:
                    OutputHelper.print_panel(
                        "No active foreground connection and no default configured.\n\n"
                        "Run [bright_blue]replx --port PORT setup[/bright_blue] first,\n"
                        "or specify a port with [bright_blue]--port PORT[/bright_blue].",
                        title="No Connection",
                        border_style="red"
                    )
                    raise typer.Exit(1)
                
                # Create fg with default
                fg_conn = _resolve_connection(default_conn)
                if not fg_conn:
                    OutputHelper.print_panel(
                        "No connection available.\n\n"
                        "Run [bright_blue]replx --port PORT setup[/bright_blue] first.",
                        title="Setup Required",
                        border_style="red"
                    )
                    raise typer.Exit(1)
                
                port_arg = fg_conn['connection']
                
                with AgentClient(port=agent_port) as client:
                    result = client.send_command(
                        'session_setup',
                        port=port_arg,
                        core=fg_conn.get('core') or "RP2350",
                        device=fg_conn.get('device'),
                        as_foreground=True,
                        set_default=False,
                        local_default=default_conn,  # Pass workspace default to server
                    )
                
                # Notify user if connection was auto-switched
                if result.get('switched_from'):
                    OutputHelper.print_panel(
                        f"Auto-disconnected [yellow]{result['switched_from']}[/yellow] (same board)",
                        title="Connection Switched",
                        border_style="dim"
                    )
                
                # Show auto-connect info when using default connection
                _print_auto_connect_info(
                    fg_conn['connection'],
                    result.get('version', '?'),
                    result.get('core', fg_conn.get('core', '?')),
                    result.get('device', fg_conn.get('device', '?')),
                    result.get('manufacturer', '')
                )
                
                status = result
                status['connected'] = True
                current_fg = fg_conn['connection']
            else:
                # Rule 3: No session + CONN → CONN → fg
                fg_conn = _resolve_connection(explicit_port)
                if not fg_conn:
                    OutputHelper.print_panel(
                        "No connection configuration found.\n\n"
                        "Run [bright_blue]replx --port PORT setup[/bright_blue] first.",
                        title="Setup Required",
                        border_style="red"
                    )
                    raise typer.Exit(1)
                
                port_arg = fg_conn['connection']
                
                with AgentClient(port=agent_port) as client:
                    result = client.send_command(
                        'session_setup',
                        port=port_arg,
                        core=fg_conn.get('core') or "RP2350",
                        device=fg_conn.get('device'),
                        as_foreground=True,
                        set_default=False,  # General commands do not change defaults
                        local_default=default_conn,  # Pass workspace default to server
                    )
                
                # Notify user if connection was auto-switched
                if result.get('switched_from'):
                    OutputHelper.print_panel(
                        f"Auto-disconnected [yellow]{result['switched_from']}[/yellow] (same board)",
                        title="Connection Switched",
                        border_style="dim"
                    )
                
                if fg_conn.get('source') == 'global':
                    if not env_path:
                        vscode_dir = _find_or_create_vscode_dir()
                        env_path = os.path.join(vscode_dir, '.replx')
                    _update_connection_config(
                        env_path, fg_conn['connection'],
                        version=result.get('version', fg_conn.get('version')),
                        core=result.get('core', fg_conn.get('core')),
                        device=result.get('device', fg_conn.get('device')),
                        manufacturer=result.get('manufacturer', fg_conn.get('manufacturer')),
                        set_default=False
                    )
                
                # Show auto-connect info when session is created with explicit CONN
                _print_auto_connect_info(
                    conn['connection'],
                    result.get('version', '?'),
                    result.get('core', fg_conn.get('core', '?')),
                    result.get('device', fg_conn.get('device', '?')),
                    result.get('manufacturer', '')
                )
                
                status = result
                status['connected'] = True
                current_fg = conn['connection']
                
                # CONN is set as fg - no need to add as bg anymore
                STATE.core = status.get('core', STATE.core)
                STATE.device = status.get('device', STATE.device)
                STATE.version = status.get('version', STATE.version)
                set_global_context(STATE.core, STATE.device, STATE.version, STATE.device_root_fs, STATE.device_path)
                
                return status

        # Rule 4: Session available + CONN omitted → fg work
        # Since current_fg already exists, proceed with fg (status is already obtained based on fg)
        if not explicit_port:
            STATE.core = status.get('core', STATE.core)
            STATE.device = status.get('device', STATE.device)
            STATE.version = status.get('version', STATE.version)
            STATE.device_root_fs = status.get('device_root_fs', STATE.device_root_fs)
            set_global_context(STATE.core, STATE.device, STATE.version, STATE.device_root_fs, STATE.device_path)
            
            return status
        
        # Rule 5: Session available + CONN → CONN → bg, bg work    
        if explicit_port:
            explicit_conn = _resolve_connection(explicit_port)
            if explicit_conn:
                explicit_key = explicit_conn['connection']
                if _port_norm(explicit_key) != _port_norm(current_fg) and _port_norm(explicit_key) not in [_port_norm(bg) for bg in current_bgs]:
                    port_arg = explicit_conn['connection']
                    
                    try:
                        with AgentClient(port=agent_port) as client:
                            bg_result = client.send_command(
                                'session_setup',
                                port=port_arg,
                                core=explicit_conn.get('core') or "RP2350",
                                device=explicit_conn.get('device'),
                                as_foreground=False,  # bg로 추가
                                set_default=False,
                            )

                        # If this command caused a new connection to be established,
                        # show the same Auto-connected panel to avoid confusion.
                        # (Without this, the command output appears with no indication
                        # that a background board was just connected.)
                        if bg_result and not bg_result.get('existing', False):
                            _print_auto_connect_info(
                                port_arg,
                                bg_result.get('version', explicit_conn.get('version', '?')),
                                bg_result.get('core', explicit_conn.get('core', '?')),
                                bg_result.get('device', explicit_conn.get('device', '?')),
                                bg_result.get('manufacturer', explicit_conn.get('manufacturer', '')),
                            )
                        
                        # Notify user if connection was auto-switched
                        if bg_result.get('switched_from'):
                            OutputHelper.print_panel(
                                f"Auto-disconnected [yellow]{bg_result['switched_from']}[/yellow] (same board)",
                                title="Connection Switched",
                                border_style="dim"
                            )
                        
                        if not env_path:
                            vscode_dir = _find_or_create_vscode_dir()
                            env_path = os.path.join(vscode_dir, '.replx')
                        _update_connection_config(
                            env_path, explicit_conn['connection'],
                            version=bg_result.get('version', explicit_conn.get('version')),
                            core=bg_result.get('core', explicit_conn.get('core')),
                            device=bg_result.get('device', explicit_conn.get('device')),
                            manufacturer=bg_result.get('manufacturer', explicit_conn.get('manufacturer')),
                            set_default=False
                        )
                    except Exception as e:
                        _handle_connection_error(e, port=port_arg)
                        raise typer.Exit(1)
        
        # Get status for explicit port if provided
        if explicit_port:
            with AgentClient(port=agent_port, device_port=explicit_port) as client:
                status = client.send_command('status')
        
        STATE.core = status.get('core', STATE.core)
        STATE.device = status.get('device', STATE.device)
        STATE.version = status.get('version', STATE.version)
        STATE.device_root_fs = status.get('device_root_fs', STATE.device_root_fs)
        set_global_context(STATE.core, STATE.device, STATE.version, STATE.device_root_fs, STATE.device_path)
        
        return status
        
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(f"Agent error: {str(e)}", title="Error", border_style="red")
        raise typer.Exit(1)


def _get_current_agent_port() -> int:
    """Get agent port for current connection (from resolved connection)."""
    global_opts = _get_global_options()
    if global_opts.get('agent_port'):
        return global_opts['agent_port']

    explicit_port = global_opts.get('port')
    env_path = _find_env_file()
    default_conn = _get_default_connection(env_path) if env_path else None

    # If an explicit port is provided, use its configured agent_port if possible
    if explicit_port:
        conn = _resolve_connection(explicit_port)
        if conn:
            return conn.get('agent_port', DEFAULT_AGENT_PORT)

    # Otherwise, prefer workspace DEFAULT connection's agent_port
    if env_path and default_conn:
        try:
            env_data = _read_env_ini(env_path)
            connections = env_data.get('connections', {})
            if default_conn in connections:
                return connections[default_conn].get('agent_port', DEFAULT_AGENT_PORT)
        except Exception:
            pass

    return DEFAULT_AGENT_PORT


def _get_device_port() -> Optional[str]:
    """Get explicit device port from --port option."""
    global_opts = _get_global_options()
    if global_opts.get('port'):
        return global_opts.get('port')
    return None


def _create_agent_client() -> AgentClient:
    """
    Create AgentClient with proper agent_port and device_port.
    """
    return AgentClient(port=_get_current_agent_port(), device_port=_get_device_port())


__all__ = [
    '_auto_detect_port',
    '_handle_connection_error',
    '_ensure_connected',
    '_get_current_agent_port',
    '_get_device_port',
    '_create_agent_client',
]
