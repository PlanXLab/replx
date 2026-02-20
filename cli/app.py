import sys
from typing import Optional
import typer
from rich.console import Console
from rich.panel import Panel

from replx import __version__
from .helpers.output import OutputHelper, get_panel_box, CONSOLE_WIDTH
from .helpers.updater import UpdateChecker
from .helpers.environment import EnvironmentManager
from .connection import _ensure_connected, _create_agent_client
from .config import STATE, _set_global_options


import re

def _preprocess_connection_shortcut():
    if len(sys.argv) < 2:
        return
    
    # Commands that accept --port global option
    commands_with_connection = {
        'setup', 'fg', 'disconnect',
        'repl', 'shell', 'exec', 'run',
        'ls', 'cat', 'get', 'put', 'cp', 'mv', 'rm', 'mkdir', 'touch',
        'usage', 'reset', 'format', 'init',
        'install', 'update', 'search',
    }
    
    # Commands that don't use connection (no shortcut conversion)
    commands_without_connection = {
        'scan', 'status', 'whoami', 'shutdown',
        'version', 'help',
    }
    
    # All known commands (for recognition)
    known_commands = commands_with_connection | commands_without_connection | {'connect'}
    
    opts_with_value = {'--port', '-p', '--agent-port'}
    
    first_arg_idx = None
    skip_next = False
    for i, arg in enumerate(sys.argv[1:], 1):
        if skip_next:
            skip_next = False
            continue
        if arg in opts_with_value:
            skip_next = True
            continue
        if not arg.startswith('-'):
            first_arg_idx = i
            break
    
    if first_arg_idx is None:
        return
    
    first_arg = sys.argv[first_arg_idx]
    
    # If first arg is a known command, no conversion needed
    if first_arg.lower() in known_commands:
        return
    
    # Check if there's a command after the potential port/target
    # If the next arg is a command that doesn't use connection, don't convert
    if first_arg_idx + 1 < len(sys.argv):
        next_arg = sys.argv[first_arg_idx + 1]
        if next_arg.lower() in commands_without_connection:
            return
    
    # Detect serial port patterns for all platforms
    is_port = False
    
    # Windows: COM1, COM10, etc.
    if re.match(r'^com\d+$', first_arg, re.IGNORECASE):
        is_port = True
    # Linux: /dev/ttyUSB0, /dev/ttyACM0, /dev/ttyAMA0, /dev/serial/by-id/*
    elif re.match(r'^/dev/(tty(USB|ACM|AMA)\d+|serial/by-id/.+)$', first_arg, re.IGNORECASE):
        is_port = True
    # macOS: /dev/cu.usbmodem*, /dev/cu.usbserial*, /dev/tty.usbmodem*, /dev/tty.usbserial*
    elif re.match(r'^/dev/(cu|tty)\.(usbmodem|usbserial|wchusbserial).+$', first_arg, re.IGNORECASE):
        is_port = True
    
    if is_port:
        sys.argv.insert(first_arg_idx, '--port')
        return


def _preprocess_cli_aliases():
    if len(sys.argv) < 2:
        return
    
    # Handle -c alias for exec command
    # -c must be followed by a command string
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == '-c':
            # Replace -c with exec
            sys.argv[i] = 'exec'
            return
    
    # Regular command aliases
    aliases = {
        'connect': 'setup',
    }
    
    for i, arg in enumerate(sys.argv[1:], 1):
        if not arg.startswith('-'):
            if arg in aliases:
                sys.argv[i] = aliases[arg]
            break

_preprocess_connection_shortcut()
_preprocess_cli_aliases()


def _tiny_command():
    if len(sys.argv) == 2 and sys.argv[1] == 'version':
        from .helpers.output import OutputHelper
        OutputHelper.print_panel(
            f"[bright_blue]replx[/bright_blue] version [bright_green]{__version__}[/bright_green]",
            title="Version",
            border_style="green"
        )
        sys.exit(0)


def _get_console():
    return Console(width=CONSOLE_WIDTH, legacy_windows=False)


try:
    import typer.rich_utils
    
    def _patched_get_rich_console():
        return _get_console()
    
    typer.rich_utils._get_rich_console = _patched_get_rich_console
except ImportError:
    pass

try:
    from typer.core import RichCommand
    _original_rich_command_format_help = RichCommand.format_help
    
    def _format_help_width(self, ctx, formatter):
        old_console = getattr(self, '_rich_console', None)
        try:
            self._rich_console = Console(width=CONSOLE_WIDTH, legacy_windows=False)
            return _original_rich_command_format_help(self, ctx, formatter)
        finally:
            if old_console is not None:
                self._rich_console = old_console
    
    RichCommand.format_help = _format_help_width
except ImportError:
    pass


import click
from click.exceptions import UsageError

_original_usage_error_format_message = UsageError.format_message
_original_usage_error_show = UsageError.show

_original_context_get_usage = click.Context.get_usage


def _custom_context_get_usage(self):
    return ""

click.Context.get_usage = _custom_context_get_usage


def _build_command_help(ctx) -> str:
    if not ctx or not ctx.command:
        return None
    
    cmd = ctx.command
    cmd_name = ctx.info_name
    
    lines = []
    
    if cmd.help:
        lines.append(cmd.help.split('\n')[0])  # First line only
        lines.append("")
    
    params_str = ""
    options = []
    arguments = []
    
    for param in cmd.params:
        if isinstance(param, click.Option):
            if not param.hidden:
                options.append(param)
        elif isinstance(param, click.Argument):
            arguments.append(param)
    
    if options:
        params_str += "[[cyan]OPTIONS[/cyan]] "
    for arg in arguments:
        arg_name = arg.name.upper()
        if arg.required:
            params_str += f"[yellow]{arg_name}[/yellow] "
        else:
            params_str += f"[yellow][{arg_name}][/yellow] "
    
    lines.append("[bold cyan]Usage:[/bold cyan]")
    lines.append(f"  replx {cmd_name} {params_str.strip()}")
    lines.append("")
    
    if options:
        lines.append("[bold cyan]Options:[/bold cyan]")
        for opt in options:
            opt_str = ", ".join(opt.opts)
            if opt.metavar:
                opt_str += f" [green]{opt.metavar}[/green]"
            elif opt.type and opt.type.name.upper() not in ('BOOL', 'BOOLEAN'):
                opt_str += f" [green]{opt.type.name.upper()}[/green]"
            
            help_text = opt.help or ""
            if len(help_text) > 40:
                help_text = help_text[:37] + "..."
            
            lines.append(f"  {opt_str:<25} {help_text}")
        lines.append("")
    
    if arguments:
        lines.append("[bold cyan]Arguments:[/bold cyan]")
        for arg in arguments:
            req = "[red][required][/red]" if arg.required else "[dim][optional][/dim]"
            lines.append(f"  [yellow]{arg.name}[/yellow]  {req}")
    
    return "\n".join(lines)


def _custom_usage_error_show(self, output_file=None):
    console = Console(width=CONSOLE_WIDTH, file=sys.stderr)
    
    error_msg = _original_usage_error_format_message(self)
    error_lines = []
    
    # Check if this is a command-specific error (has context with command info)
    cmd_name = None
    if self.ctx and self.ctx.info_name and self.ctx.info_name != 'replx':
        cmd_name = self.ctx.info_name
    
    is_option_error = any(x in error_msg.lower() for x in ['no such option', 'missing option', 'missing argument', 'invalid value', 'requires an argument', 'got unexpected'])
    
    if cmd_name and is_option_error:
        help_text = _build_command_help(self.ctx)
        if help_text:
            error_lines.append(help_text)
            error_lines.append("")
            error_lines.append(f"[red]{error_msg}[/red]")
        else:
            # Fallback: show basic usage
            error_lines.append(f"[bold cyan]Usage:[/bold cyan] replx {cmd_name} [OPTIONS] [ARGS]...")
            error_lines.append("")
            error_lines.append(f"[red]{error_msg}[/red]")
    else:
        # Unknown command or general error - add Usage line
        error_lines.append("[bold cyan]Usage:[/bold cyan] replx [OPTIONS] COMMAND [ARGS]...")
        error_lines.append("")
        error_lines.append(f"[red]{error_msg}[/red]")
    
    console.print(Panel(
        "\n".join(error_lines),
        title="Error",
        border_style="red",
        box=get_panel_box(),
        width=CONSOLE_WIDTH
    ))


def _handle_usage_error(e):
    console = Console(width=CONSOLE_WIDTH, file=sys.stderr)
    
    error_msg = str(e.format_message()) if hasattr(e, 'format_message') else str(e)
    error_lines = []
    
    cmd_name = None
    if e.ctx and e.ctx.info_name and e.ctx.info_name != 'replx':
        cmd_name = e.ctx.info_name
    
    is_option_error = any(x in error_msg.lower() for x in ['no such option', 'missing option', 'missing argument', 'invalid value', 'requires an argument', 'got unexpected'])
    
    if cmd_name and is_option_error:
        help_text = _build_command_help(e.ctx)
        if help_text:
            error_lines.append(help_text)
            error_lines.append("")
            error_lines.append(f"[red]{error_msg}[/red]")
        else:
            # Fallback: show basic usage
            error_lines.append(f"[bold cyan]Usage:[/bold cyan] replx {cmd_name} [OPTIONS] [ARGS]...")
            error_lines.append("")
            error_lines.append(f"[red]{error_msg}[/red]")
    else:
        # Unknown command or general error - add Usage line
        error_lines.append("[bold cyan]Usage:[/bold cyan] replx [OPTIONS] COMMAND [ARGS]...")
        error_lines.append("")
        error_lines.append(f"[red]{error_msg}[/red]")
    
    console.print(Panel(
        "\n".join(error_lines),
        title="Error",
        border_style="red",
        box=get_panel_box(),
        width=CONSOLE_WIDTH
    ))

UsageError.show = _custom_usage_error_show


app = typer.Typer(
    rich_markup_mode="rich",
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_show_locals=False,
    pretty_exceptions_enable=False,
    help="MicroPython REPL tool for device management."
)


def _print_main_help():
    lines = []
    lines.append("[bold]MicroPython development tool for VSCode[/bold]")
    lines.append("[dim]Connect, manage files, run code, and develop on MicroPython boards[/dim]")
    lines.append("")
    lines.append("[bold cyan]Usage:[/bold cyan]")
    lines.append("  replx [yellow][OPTIONS][/yellow] [green]COMMAND[/green] [[dim]ARGS[/dim]]...")
    lines.append("")
    lines.append("[bold cyan]Global Options:[/bold cyan]")
    lines.append("  [yellow]-p, --port[/yellow] [cyan]PORT[/cyan]       Serial port [dim](COM3, /dev/ttyUSB0)[/dim]")
    lines.append("  [dim] 󱞩 [cyan]PORT[/cyan] can be used alone without the -p flag[/dim]")
    lines.append("  [dim] 󱞩 setup requires an explicit port[/dim]")

    # Command groups - descriptions match first line of each command's help_text
    command_groups = [
        ("Connection & Session", [
            ("setup", "Initialize MicroPython development environment for VSCode"),
            ("scan", "Find and list all connected MicroPython boards"),
            ("status", "Show all active sessions and their board connections"),
            ("fg", "Switch foreground connection to another board"),
            ("whoami", "Show which board your commands are currently talking to"),
            ("disconnect", "Close a board connection"),
            ("shutdown", "Completely stop the replx agent and release all connections"),
        ]),
        ("Interactive", [
            ("repl", "Enter interactive MicroPython mode on the connected board"),
            ("shell", "Enter interactive shell for managing files on the device"),
        ]),
        ("Execution", [
            ("exec, -c", "Run a single MicroPython command on the connected board"),
            ("run", "Run a MicroPython script on the connected board"),
        ]),
        ("File Operations", [
            ("ls", "List files and directories on the connected device"),
            ("cat", "Display the contents of a file on the connected device"),
            ("get", "Download files or directories from the device to your computer"),
            ("put", "Upload files or directories from your computer to the device"),
            ("cp", "Copy files or directories on the connected device"),
            ("mv", "Move or rename files and directories on the connected device"),
            ("rm", "Delete files or directories from the connected device"),
            ("mkdir", "Create directories on the connected device"),
            ("touch", "Create empty files on the connected device"),
        ]),
        ("Device Management", [
            ("usage", "Show memory and storage usage of the connected device"),
            ("reset", "Reset the device (soft or hard with auto-reconnect)"),
            ("format", "Format (erase) the filesystem on the connected device"),
            ("init", "Completely reset device: format filesystem and install all libraries"),
            ("wifi", "Configure and manage WiFi settings on the device"),
            ("firmware", "Download and update device firmware (UF2)"),
        ]),
        ("Package Management", [
            ("pkg", "Manage libraries: search, download, update"),
            ("mpy", "Compile Python files to .mpy bytecode"),
        ]),
    ]
    
    for group_name, commands in command_groups:
        lines.append("")
        lines.append(f"[bold cyan]{group_name}:[/bold cyan]")
        for cmd, desc in commands:
            lines.append(f"  [green]{cmd:<12}[/green] {desc}")
    
    lines.append("")
    lines.append("[dim]Use 'replx COMMAND --help' for detailed help on each command.[/dim]")
    
    OutputHelper.print_panel(
        "\n".join(lines),
        title="replx",
        border_style="bright_blue"
    )


# =============================================================================
# App Callback
# =============================================================================

@app.callback(invoke_without_command=True)
def cli(
    ctx: typer.Context,
    global_port: Optional[str] = typer.Option(
        None,
        "--port", "-p",
        help="Serial port to use (e.g., COM3)",
        is_eager=True
    ),
    global_agent_port: Optional[int] = typer.Option(
        None,
        "--agent-port",
        help="Agent UDP port (auto-assigned if not specified)",
        is_eager=True
    ),
    show_help: bool = typer.Option(
        False,
        "--help",
        is_eager=True,
        expose_value=True,
        help="Show this message and exit."
    )
):
    """
    MicroPython REPL tool for device management.
    
    Use 'replx --port PORT setup' to connect, then run commands.
    """
    
    # Store global options in module-level storage for access by all commands
    _set_global_options(global_port, global_agent_port)
    
    # Also store in context for subcommands that receive ctx
    ctx.ensure_object(dict)
    ctx.obj['global_port'] = global_port
    ctx.obj['global_agent_port'] = global_agent_port
    
    # Handle help flag first
    if show_help:
        _print_main_help()
        raise typer.Exit()
    
    # If no command provided, show help
    if ctx.invoked_subcommand is None:
        _print_main_help()
        raise typer.Exit()


# =============================================================================
# Import all commands to register them with the app
# =============================================================================
from .commands import file, device, exec, package, utility

# These imports are for side-effect (command registration)
_command_modules = (file, device, exec, package, utility)


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    # Check if replx command is run without arguments
    if len(sys.argv) == 1:
        OutputHelper.print_panel(
            "Use [bright_blue]replx --help[/bright_blue] to see available commands.",
            title="Replx",
            border_style="green"
        )
        raise SystemExit()
    
    # Handle -v / --version
    if len(sys.argv) == 2 and sys.argv[1] in ('--version', '-v'):
        OutputHelper.print_panel(
            f"[bright_blue]replx[/bright_blue] version [bright_green]{__version__}[/bright_green]",
            title="Version",
            border_style="green"
        )
        sys.exit(0)
    
    # Handle -c / --command
    if sys.argv[1] in ('-c', '--command'):
        if len(sys.argv) < 3:
            OutputHelper.print_panel(
                "Missing required argument: [yellow]COMMAND[/yellow]\n\n"
                "[bold cyan]Usage:[/bold cyan]\n"
                "  replx -c [yellow]COMMAND[/yellow]\n\n"
                "[bold cyan]Examples:[/bold cyan]\n"
                "  replx -c \"print('hello')\"",
                title="Command Required",
                border_style="red"
            )
            sys.exit(1)
        
        command_str = sys.argv[2]
        
        try:
            _ensure_connected()
            client = _create_agent_client()
            result = client.send_command('exec', code=command_str)
            output = result.get('output', '')
            if output:
                print(output, end='')
                if not output.endswith('\n'):
                    print()
        except Exception as e:
            error_msg = str(e)
            if 'Not connected' in error_msg:
                OutputHelper.print_panel(
                    "Not connected to any device.\n\nRun [bright_green]replx connect --port COM3[/bright_green] first.",
                    title="Connection Required",
                    border_style="red"
                )
            else:
                typer.echo(f"Error: {error_msg}", err=True)
            sys.exit(1)
        sys.exit(0)
    
    # Check if replx --help is run (with only --help or -h flag)
    if len(sys.argv) == 2 and sys.argv[1] in ('--help', '-h'):
        # Show custom main help (single box)
        _print_main_help()
        sys.exit(0)

    # Normalize -ne and -en options
    try:
        out = [sys.argv[0]]
        for tok in sys.argv[1:]:
            if not tok.startswith('-') or tok.startswith('--') or len(tok) <= 2:
                out.append(tok)
                continue

            if tok in ('-ne', '-en'):
                out.extend(['-n', '-e'])
                continue

            if tok.startswith('-') and set(tok[1:]).issubset({'n', 'e'}) and len(tok) > 2:
                typer.echo("Error: Option chaining error: -n and -e can only be used once, not multiple times.", err=True)
                sys.exit(2)
            
            out.append(tok)
        sys.argv = out
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        sys.exit(2)

    args = sys.argv[1:]

    known = {
        "install","put","get","cat","rm","mv","cp","touch","run","format","search",
        "repl","df","shell","mkdir","ls","reset","env","scan","port","update","target","stat","mem",
        "pkg","setup","init","firmware"
    }

    # Options that take a value (skip the value when finding first non-option)
    opts_with_value = {'--port', '-p', '--agent-port'}
    
    # Find first non-option argument (the command), skipping option values
    first_nonopt_idx = None
    skip_next = False
    for i, a in enumerate(sys.argv[1:], 1):
        if skip_next:
            skip_next = False
            continue
        if a in opts_with_value:
            skip_next = True
            continue
        if not a.startswith('-'):
            first_nonopt_idx = i
            break
    first_nonopt = sys.argv[first_nonopt_idx] if first_nonopt_idx is not None else None

    run_opts = {'-n', '--non-interactive', '-e', '--echo', '-d', '--device'}
    has_device_opt = bool(run_opts & {'-d', '--device'} & set(args))
    
    # Find script files: .py always, .mpy only with -d option
    # Count how many .py/.mpy files exist (excluding option values)
    script_files = []
    skip_next = False
    for i, a in enumerate(sys.argv[1:], 1):
        if skip_next:
            skip_next = False
            continue
        if a in opts_with_value:
            skip_next = True
            continue
        if a.endswith('.py') or (has_device_opt and a.endswith('.mpy')):
            script_files.append((i, a))
    
    script_arg_idx = script_files[0][0] if script_files else None

    # Only inject 'run' if:
    # - no command found (first_nonopt not in known)
    # - exactly one .py file exists
    should_inject_run = (
        ('run' not in args) and
        (len(script_files) == 1) and
        (first_nonopt is None or first_nonopt not in known)
    )

    if should_inject_run:
        opt_idx = next((i for i, a in enumerate(sys.argv[1:], 1) if a in run_opts), None)
        insert_at = opt_idx if opt_idx is not None else script_arg_idx
        sys.argv.insert(insert_at, 'run')

        first_nonopt_idx = next((i for i, a in enumerate(sys.argv[1:], 1) if not a.startswith('-')), None)
        first_nonopt = sys.argv[first_nonopt_idx] if first_nonopt_idx is not None else None

    suppressed = {'scan'}
    if not any(x in sys.argv for x in ('--help','-h','--version','-v')):
        if (first_nonopt is None) or (first_nonopt not in suppressed):
            UpdateChecker.check_for_updates(__version__)
        
    try:
        EnvironmentManager.load_env_from_rep()
        app(standalone_mode=False)
        exit_code = 0
    except click.exceptions.UsageError as e:
        # Handle UsageError with our custom formatter
        _handle_usage_error(e)
        exit_code = 2
    except click.exceptions.Abort:
        print()
        exit_code = 1
    except KeyboardInterrupt:
        try:
            if STATE.repl_protocol:
                STATE.repl_protocol.request_interrupt()
        except Exception:
            pass
        print()
        exit_code = 130
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
