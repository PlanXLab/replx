from . import get_version

__version__ = get_version()

import os 
import sys
import time
import re
import threading
import posixpath
import shutil
import urllib.request
from urllib.parse import urlparse
from typing import Optional
import typer
from rich.console import Console

from serial.tools import list_ports
from rich.live import Live

from .exceptions import ReplxError
from .terminal import (
    IS_WINDOWS,
    getch,
)
from .helpers import (
    OutputHelper, DeviceScanner, DeviceValidator,
    EnvironmentManager, StoreManager, CompilerHelper, InstallHelper,
    SearchHelper, UpdateChecker, RegistryHelper,
    set_global_context
)
from .repl_protocol import ReplProtocol
from .file_system import DeviceFileSystem

# Patch Typer's rich console creation to force 80-column width
import typer.rich_utils
_original_get_rich_console = getattr(typer.rich_utils, '_get_rich_console', None)

def _get_80col_console(**kwargs):
    """Get a console with width forced to 80 for help output.
    
    Accepts all keyword arguments that Typer passes, including stderr,
    force_terminal, force_interactive, soft_wrap, and theme.
    """
    # Force width=80 while preserving other Typer options
    kwargs['width'] = 80
    kwargs['legacy_windows'] = False
    return Console(**kwargs)

# Replace the get_rich_console at module level if available
try:
    typer.rich_utils._get_rich_console = _get_80col_console
except AttributeError:
    pass

# Also patch the RichCommand to use fixed width
try:
    from typer.core import RichCommand
    _original_rich_command_format_help = RichCommand.format_help
    
    def _format_help_80col(self, ctx, formatter):
        """Format help with 80-column constraint."""
        # Save original console settings
        old_console = getattr(self, '_rich_console', None)
        try:
            # Use 80-column console for formatting
            self._rich_console = Console(width=80, legacy_windows=False)
            return _original_rich_command_format_help(self, ctx, formatter)
        finally:
            if old_console is not None:
                self._rich_console = old_console
    
    RichCommand.format_help = _format_help_80col
except ImportError:
    pass


_repl_protocol = None
_file_system = None
_version = 0.0
_core = ""
_device = ""
_device_root_fs = "/"
_core_path = ""
_device_path = ""
_port = ""

def _tiny_command(cmd:str) -> None:
    """
    Execute a command on the connected device, wrapping it if necessary.
    :param cmd: The command to execute.
    """
    import ast

    try:
        tree = ast.parse(cmd, mode="exec")
        is_expr = (
            len(tree.body) == 1 and
            isinstance(tree.body[0], ast.Expr)
        )
    except SyntaxError:
        is_expr = False

    if is_expr:
        wrapped = (
            f"_r ={cmd}\n"
            "if __r is not None:\n"
            "    print(repr(__r))\n"
        )
    else:
        wrapped = cmd if cmd.endswith("\n") else cmd + "\n"

    out = _repl_protocol.exec(wrapped)
    print(out.decode("utf-8", "replace"), end="", flush=True)

app = typer.Typer(
    help="MicroPython REPL tool for device management",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    context_settings={"max_content_width": 80}
)

def version_callback(val: bool):
    if not val:
        return
    OutputHelper.print_panel(
        f"replx [green]{__version__}[/green].",
        title="version",
        border_style="cyan"
    )
    raise typer.Exit()

@app.callback(invoke_without_command=True)
def cli(
    ctx: typer.Context,
    port: str = typer.Option(
        "",
        "--port",
        "-p",
        envvar="SERIAL_PORT",
        help="The serial port for connected device."
    ),
    command: str = typer.Option(
        "",
        "--command",
        "-c",
        help="Command to execute on the connected device."
    ),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit."
    )
):
    global _repl_protocol, _file_system, _version, _core, _device, _device_root_fs, _core_path, _device_path, _port
    BAUD_RATES = int(os.environ.get("BAUD_RATES", "115200"))
    _port = port

    # Skip device connection check if --help or -h is present (help doesn't need device)
    if "--help" in sys.argv or "-h" in sys.argv:
        return

    if ctx.invoked_subcommand in ("scan", "port", "update", "search", "env"):
        return

    descript =  DeviceScanner.get_board_info(_port)
    if not descript:
        if _port:
            OutputHelper.print_panel(
                f"No device connected to [red]{_port}[/red].",
                title="Connection Failed",
                border_style="red"
            )
        else:
            OutputHelper.print_panel(
                "Serial port name is missing.\nUse [bright_blue]--port[/bright_blue] option or set [bright_blue]SERIAL_PORT[/bright_blue] environment variable.\nOr run [bright_blue]replx scan[/bright_blue] to find available ports.",
                title="Serial Port Required",
                border_style="red"
            )
        raise typer.Exit(1)

    _version, _, _core, _device = descript
    _version = float(_version)
    
    if not DeviceValidator.is_supported_core(_core):
        OutputHelper._console.print(f"The [red]{_device}[/red] is not supported.")
        raise typer.Exit(1)
    
    if _device in ('xnode', 'smartfarm1'):
        _device_root_fs = "/flash/"

    _core_path = os.path.join(StoreManager.pkg_root(), "core", _core)
    if _core != _device:
        _device_path = os.path.join(StoreManager.pkg_root(), "device", _device)

    try:
        _repl_protocol = ReplProtocol(port=_port, baudrate=BAUD_RATES, core=_core, device_root_fs=_device_root_fs)
        _file_system = DeviceFileSystem(_repl_protocol, core=_core, device_root_fs=_device_root_fs)
        
        # Set global context for helper classes
        set_global_context(_core, _device, _version, _device_root_fs, _device_path, _file_system)
    except ReplxError:
        OutputHelper._console.print(f"Device is not connected to [red]{_port}[/red]")
        OutputHelper._console.print(f"Please check the port with the scan command and try again.")
        raise typer.Exit(1)

    if ctx.invoked_subcommand is None and command:
       _tiny_command(command)
       _repl_protocol.close()

@app.command()
def get(
    remote: str = typer.Argument(..., help="Remote file path"),
    local: Optional[str] = typer.Argument(None, help="Local file path")
):
    """
    Download a file from the connected device to the local filesystem.
    If local is omitted, displays the file content in a panel.
    """
    remote = _file_system._normalize_remote_path(remote)
    display_remote = remote.replace(_device_root_fs, "", 1)
    
    try:
        if local:
            # Download to local file with progress panel
            with Live(OutputHelper.create_progress_panel(0, 1, title=f"Downloading {display_remote}", message="Downloading..."), console=OutputHelper._console, refresh_per_second=10) as live:
                _file_system.get(remote, local)
                live.update(OutputHelper.create_progress_panel(1, 1, title=f"Downloading {display_remote}"))
            
            display_local = local if local else os.path.basename(remote)
            OutputHelper.print_panel(
                f"Downloaded [bright_blue]{display_remote}[/bright_blue]\nto [green]{display_local}[/green]",
                title="Download Complete",
                border_style="green"
            )
        else:
            # Display file content in panel
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_name = tmp.name
            
            try:
                with Live(OutputHelper.create_progress_panel(0, 1, title=f"Downloading {display_remote}", message="Reading file..."), console=OutputHelper._console, refresh_per_second=10) as live:
                    _file_system.get(remote, tmp_name)
                    live.update(OutputHelper.create_progress_panel(1, 1, title=f"Downloading {display_remote}"))
                
                with open(tmp_name, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                
                OutputHelper.print_panel(
                    content,
                    title=f"File Content: {display_remote}",
                    border_style="blue"
                )
            finally:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
                    
    except ReplxError:
        OutputHelper.print_panel(
            f"[red]{display_remote}[/red] does not exist or is not a file.",
            title="Download Failed",
            border_style="red"
        )

@app.command()
def mem():
    """
    Show the memory information of the connected device.
    """
    ret = _file_system.mem()
    if ret:
        out_str = f"""Total: {ret[2]//1024:5} KByte ({ret[2]})
Used: {ret[1]//1024:6} KByte ({ret[1]})
Free: {ret[0]//1024:6} KByte ({ret[0]})
Usage: {round(ret[3],2):5} %"""

        OutputHelper.print_panel(out_str, title="Memory Information", border_style="green")

@app.command()
def mkdir(remote: str = typer.Argument(..., help="Directory to create")):
    """
    Create a directory on the connected device.
    """
    path_ = _file_system._normalize_remote_path(remote)
        
    if _file_system.mkdir(path_):
        OutputHelper.print_panel(
            f"Directory [bright_blue]{remote}[/bright_blue] created successfully.",
            title="Create Directory",
            border_style="green"
        )
    else:
        OutputHelper.print_panel(
            f"Directory [bright_blue]{remote}[/bright_blue] already exists.",
            title="Create Directory",
            border_style="yellow"
        )

@app.command()
def rm(remote: str = typer.Argument(..., help="File or directory to remove")):
    """
    Remove a file or directory from the connected device.
    """
    remote = _file_system._normalize_remote_path(remote)
        
    try:
        if _file_system.is_dir(remote):
            _file_system.rmdir(remote)
            item_type = "Directory"
        else:
            _file_system.rm(remote)
            item_type = "File"
        
        display_path = remote.replace(_device_root_fs, "", 1)
        OutputHelper.print_panel(
            f"{item_type} [bright_blue]{display_path}[/bright_blue] removed successfully.",
            title="Remove",
            border_style="green"
        )
    except ReplxError:
        remote = remote.replace(_device_root_fs, "", 1)
        OutputHelper.print_panel(
            f"[red]{remote}[/red] does not exist.",
            title="Remove",
            border_style="red"
        )

@app.command()
def ls(path: str = typer.Argument("/", help="Directory path to list")):
    """
    List the contents of a directory on the connected device.
    """
    path = _file_system._normalize_remote_path(path)

    try:
        items = _file_system.ls_detailed(path)
        
        if not items:
            OutputHelper.print_panel(
                f"[red]{path[1:]}[/red] does not exist or is empty.",
                title=f"Directory Listing: {path}",
                border_style="red"
            )
            return
        
        display_items = []
        for name, size, is_dir in items:
            # Inline icon logic
            if is_dir:
                icon = "📁"
            else:
                ext_icons = {
                    ".py":   "🐍",
                    ".mpy":  "📦",
                    ".txt":  "📜",
                    ".csv":  "📊",
                    ".json": "🗄️",
                }
                _, ext = os.path.splitext(name.lower())
                icon = ext_icons.get(ext, "📄")
            display_items.append((is_dir, name, size, icon))

        if display_items:
            size_width = max(len(str(item[2])) for item in display_items)
            
            lines = []
            for is_dir, f_name, size, icon in display_items:
                name_str = f"[bright_blue]{f_name}[/bright_blue]" if is_dir else f_name
                size_str = "" if is_dir else str(size)
                lines.append(f"{size_str.rjust(size_width)}  {icon}  {name_str}")
            
            OutputHelper.print_panel(
                "\n".join(lines),
                title=f"Directory Listing: {path}",
                border_style="blue"
            )

    except ReplxError:
        OutputHelper.print_panel(
            f"[red]{path[1:]}[/red] does not exist.",
            title=f"Directory Listing: {path}",
            border_style="red"
        )


@app.command()
def put(
    local: str = typer.Argument(..., help="Local file or directory to upload"),
    remote: Optional[str] = typer.Argument(None, help="Remote destination path")
):
    """
    Upload a file or directory to the connected device.
    """
    # Check if local file/directory exists
    if not os.path.exists(local):
        OutputHelper.print_panel(
            f"[red]{local}[/red] does not exist.",
            title="Upload Failed",
            border_style="red"
        )
        raise typer.Exit(1)
    
    if remote is None:
        remote = os.path.basename(os.path.abspath(local))
    else:
        if not remote.startswith(_device_root_fs):
            remote = posixpath.join(_device_root_fs, remote)
        
        try:
            if _file_system.is_dir(remote):
                remote = remote + "/" + os.path.basename(os.path.abspath(local))
        except ReplxError:
            pass
    
    is_dir = os.path.isdir(local)
    item_type = "Directory" if is_dir else "File"
    display_remote = remote.replace(_device_root_fs, "", 1)
    base_name = os.path.basename(local)
    
    # Count files for progress
    if is_dir:
        file_count = sum(1 for _, _, files in os.walk(local) for _ in files)
    else:
        file_count = 1
    
    with Live(OutputHelper.create_progress_panel(0, file_count, title=f"Uploading {base_name}", message=f"Uploading {item_type.lower()}..."), console=OutputHelper._console, refresh_per_second=10) as live:
        if is_dir:
            # Upload directory using batch mode
            def progress_cb(done, total, filename):
                live.update(OutputHelper.create_progress_panel(done, total, title=f"Uploading {base_name}", message=f"Uploading {filename}..."))
            
            _file_system.putdir_batch(local, remote, progress_cb)
        else:
            _file_system.put(local, remote)
            live.update(OutputHelper.create_progress_panel(1, 1, title=f"Uploading {base_name}"))


@app.command()
def run(
    local_file: str = typer.Argument(..., help="Local script file to run"),
    non_interactive: bool = typer.Option(False, "--non-interactive", "-n", help="Non-interactive execution"),
    echo: bool = typer.Option(False, "--echo", "-e", help="Turn on echo for interactive")
):
    """
    Run a local script on the connected device.
    """
    if non_interactive and echo:
        typer.echo("Error: The --non-interactive and --echo options cannot be used together.", err=True)
        raise typer.Exit(1)

    try:
        _repl_protocol.run(local_file, not non_interactive, echo)
    except KeyboardInterrupt:
        try:
            _repl_protocol.request_interrupt()
        except Exception:
            pass
        time.sleep(0.2)
        print()
    except IOError:
        OutputHelper._console.print(f"File not found: [red]{local_file}[/red]", style="red")
    except ReplxError as ex:
        OutputHelper.format_error_output(str(ex).strip().split('\n'), local_file)

@app.command()
def repl():
    """
    Enter the REPL (Read-Eval-Print Loop) mode.
    """
    OutputHelper.print_panel(
        f"Connected to [bright_yellow]{_device}[/bright_yellow] on [bright_green]{_core}[/bright_green]\nPress [magenta]Ctrl+C[/magenta] to exit REPL mode.",
        title="REPL Mode",
        border_style="magenta"
    )

    _repl_protocol.repl()


_is_stop_spinner = None

@app.command()
def format():
    """
    Format the file system of the connected device.
    """
    global _is_stop_spinner

    _is_stop_spinner = False
    frame_idx = [0]
    
    def _spinner_task(live):
        """Spinner runs in thread to show progress"""
        try:
            while not _is_stop_spinner:
                live.update(OutputHelper.create_spinner_panel(
                    f"Formatting file system on {_device}...",
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
            f"Formatting file system on {_device}...",
            title="Format File System",
            frame_idx=0
        ), console=OutputHelper._console, refresh_per_second=10) as live:
            # Start spinner thread
            spinner_thread = threading.Thread(target=_spinner_task, args=(live,), daemon=True)
            spinner_thread.start()
            
            # Execute format in main thread (blocking call)
            try:
                ret = _file_system.format()
            except Exception as e:
                error = e
            finally:
                # Stop spinner
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
        OutputHelper.print_panel(
            f"File system on [bright_yellow]{_device}[/bright_yellow] has been formatted successfully.",
            title="Format Complete",
            border_style="green"
        )
    else:
        OutputHelper.print_panel(
            f"Device [red]{_device}[/red] does not support formatting.",
            title="Format Failed",
            border_style="red"
        )
    return ret

@app.command()
def df():
    """
    Show the file system information of the connected device.
    """
    ret = _file_system.df()
    if ret:
        out_str = f"""Total: {ret[0]//1024:5} KByte ({ret[0]:5})
Used: {ret[1]//1024:6} KByte ({ret[1]:7})
Free: {ret[2]//1024:6} KByte ({ret[2]:6})
Usage: {round(ret[3],2):5} %""" 
        
        OutputHelper.print_panel(out_str, title="File System Information", border_style="cyan")

@app.command()
def shell():
    """
    Enter an interactive shell for device control.
    """
    import shlex

    COMMANDS = "clear, ls, cd, get, put, rm, mkdir, df, repl, pwd, help(?)"
    HELP = f"""Type 'exit' to exit.  
Available: {COMMANDS}"""
    
    current_path = '/' # _device_root_fs
    
    def print_prompt():
        print(f"\n📟 {_device}:{current_path} >", end=" ", flush=True)

    def run_cmd(cmdline):
        nonlocal current_path

        args = shlex.split(cmdline)
        if not args:
            return
        cmd = args[0]

        try:        
            if cmd == "ls":
                if len(args) > 1:
                    print("Usage: ls")
                    return

                ls(path=current_path)
            elif cmd == "cd":
                if len(args) != 2:
                    print("Usage: cd <dir>")
                    return
                
                new_path = posixpath.normpath(posixpath.join(current_path, args[1]))
                try:
                    _file_system.is_dir(new_path)
                    current_path = new_path
                except:
                    dirs = " ".join(args[1:])
                    OutputHelper._console.print(f"The [red]{dirs}[/red] directory does not exist.")
            elif cmd == "get":
                if len(args) < 2 or len(args) > 3:
                    print("Usage: get <remote> [local]")
                    return
                remote = posixpath.join(current_path, args[1])
                local = args[2] if len(args) >= 3 else None
                get(remote=remote, local=local)
            elif cmd == "put":
                if len(args) < 2 or len(args) > 3:
                    print("Usage: put <local> [remote]")
                    return
                
                local = args[1]
                remote = args[2] if len(args) >= 3 else None
                if remote is None:
                    remote = os.path.basename(local)
                remote = posixpath.join(current_path, remote)
                put(local=local, remote=remote)
            elif cmd == "rm":
                if len(args) != 2:
                    print("Usage: rm <remote>")
                    return
                remote = posixpath.join(current_path, args[1])
                rm(remote=remote)
            elif cmd == "mkdir":
                if len(args) != 2:
                    print("Usage: mkdir <remote>")
                    return
                remote = posixpath.join(current_path, args[1])
                mkdir(remote=remote)
            elif cmd == "df":
                if len(args) > 1:
                    print("Usage: df")
                    return

                df()
            elif cmd == "-c":
                if len(args) < 2:
                    print("Usage: -c <scripts>")
                    return
                
                scripts = cmdline[3:]
                _tiny_command(scripts)
            elif cmd == "repl":
                print("Press CTRL+C to exit.")
                if len(args) > 1:
                    print("Usage: repl")
                    return
                
                _repl_protocol.repl()
            elif cmd == "pwd":
                if len(args) > 1:
                    print("Usage: pwd")
                    return

                print(current_path)
            elif cmd == "clear":
                if len(args) > 1:
                    print("Usage: clear")
                    return
                OutputHelper._console.clear()
            elif cmd == "help" or cmd == "?":
                if len(args) > 1:
                    print("Usage: help or ?")
                    return
                
                print(HELP)
            else:
                raise Exception(f"Unknown command: {cmd}")
        except ReplxError:
            raise Exception(f"Unknown command: {cmdline}")
    
    # Display shell header
    header_content = f"Connected to [bright_yellow]{_device}[/bright_yellow] on [bright_green]{_core}[/bright_green]\n\n"
    header_content += f"Available commands:\n{COMMANDS}\n\n"
    header_content += "Type [bright_blue]help[/bright_blue] or [bright_blue]?[/bright_blue] for more info\n"
    header_content += "Type [bright_blue]exit[/bright_blue] to quit"
    
    OutputHelper.print_panel(
        header_content,
        title="Interactive Shell",
        border_style="cyan"
    )
                
    try:
        while True:
            print_prompt()
            line = sys.stdin.buffer.readline().decode(errors='replace').rstrip()
            if line == 'exit':
                break
            try:
                run_cmd(line)
            except Exception as e:
                print(f"{e}")
                continue
            
    except (EOFError, KeyboardInterrupt):
        print()
    finally:
        OutputHelper.print_panel(
            "Shell session ended.",
            title="Exit Shell",
            border_style="cyan"
        )


@app.command()
def reset():
    """
    Reset the connected device.
    """
    _repl_protocol.reset()
    
    OutputHelper.print_panel(
        f"Device [bright_yellow]{_device}[/bright_yellow] has been reset.",
        title="Reset Device",
        border_style="blue"
    )


@app.command()
def env(device: Optional[str] = typer.Argument(None, help="Device name")):
    """
    Set up the VSCode environment for the connected device.
    """
    global _device, _device_path, _core_path, _core, _port

    vscode_dir = ".vscode"
    env_file = os.path.join(vscode_dir, ".env")
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
    settings_file_contents = """{
    "files.exclude": {
      "**/.vscode": true
    },
    "python.languageServer": "Pylance",
    "python.analysis.diagnosticSeverityOverrides": {
        "reportMissingModuleSource": "none"
    },
    "python.analysis.extraPaths": [
        "./.vscode"
    ]
}
"""
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

    # Check if device or port is missing when not provided
    if not device and not _port:
        OutputHelper.print_panel(
            "No device connected and no device specified.\nConnect a device or use: [bright_blue]replx env <device>[/bright_blue]",
            title="Environment Setup",
            border_style="yellow"
        )
        raise typer.Exit(1)
    
    if device:
        # Device name explicitly provided
        if not DeviceValidator.is_supported_device(device):
            OutputHelper.print_panel(
                f"Device [red]{device}[/red] is not supported.",
                title="Environment Setup",
                border_style="red"
            )
            raise typer.Exit(1)

        forced_core = DeviceValidator.find_core_by_device(device)
        if not forced_core:
            OutputHelper.print_panel(
                f"Device [red]{device}[/red] is not supported.",
                title="Environment Setup",
                border_style="red"
            )
            raise typer.Exit(1)

        _device = device
        _core = forced_core
        _core_path = os.path.join(StoreManager.pkg_root(), "core", _core)
        _device_path = os.path.join(StoreManager.pkg_root(), "device", _device)
    else:
        # No device name provided, read from connected device via port
        descript = DeviceScanner.get_board_info(_port)
        if not descript:
            OutputHelper.print_panel(
                f"No device connected to [red]{_port}[/red].",
                title="Environment Setup",
                border_style="red"
            )
            raise typer.Exit(1)
        
        _version, _, _core, _device = descript
        
        if not DeviceValidator.is_supported_core(_core):
            OutputHelper.print_panel(
                f"Device [red]{_device}[/red] is not supported.",
                title="Environment Setup",
                border_style="red"
            )
            raise typer.Exit(1)
        
        _core_path = os.path.join(StoreManager.pkg_root(), "core", _core)
        if _core != _device:
            _device_path = os.path.join(StoreManager.pkg_root(), "device", _device)

    if os.path.exists(vscode_dir):
        OutputHelper.print_panel(
            "Environment already exists.\nDo you want to overwrite it? (y/n)",
            title="Environment Setup",
            border_style="yellow"
        )
        print("Your choice: ", end='', flush=True)
        while True:
            ch = getch().lower()
            if ch == b'n':
                print("n\n")
                OutputHelper.print_panel(
                    "Operation cancelled.",
                    title="Environment Setup",
                    border_style="blue"
                )
                return
            elif ch == b'y':
                print("y")
                break
            else:
                OutputHelper._console.print("\r[red]Please enter 'y' or 'n'.[/red] Your choice: ", end='', highlight=False)
        shutil.rmtree(vscode_dir, onerror=EnvironmentManager.force_remove_readonly)

    os.makedirs(vscode_dir, exist_ok=True)

    port_str = _port.upper() if IS_WINDOWS else _port
    with open(env_file, "w", encoding="utf-8") as f:
        f.write(f"SERIAL_PORT={port_str}\n")
    with open(task_file, "w", encoding="utf-8") as f:
        f.write(task_file_contents)
    with open(settings_file, "w", encoding="utf-8") as f:
        f.write(settings_file_contents)
    with open(launch_file, "w", encoding="utf-8") as f:
        f.write(launch_file_contents)

    linked = 0
    if _core:
        core_typehints = os.path.join(StoreManager.pkg_root(), "core", _core, "typehints")
        linked += EnvironmentManager.link_typehints_into_vscode(core_typehints, vscode_dir)
    if _device:
        device_typehints = os.path.join(StoreManager.pkg_root(), "device", _device, "typehints")
        linked += EnvironmentManager.link_typehints_into_vscode(device_typehints, vscode_dir)

    content = f"Serial port: [bright_green]{port_str}[/bright_green]\n"
    content += f"Device: [bright_yellow]{_device}[/bright_yellow]\n"
    content += f"Core: [bright_green]{_core}[/bright_green]\n"
    content += f"Typehints linked: {linked}"
    
    OutputHelper.print_panel(
        content,
        title="Environment Setup Complete",
        border_style="green"
    )


@app.command(name="update")
def update(
    device: Optional[str] = typer.Argument(None, help="Device name"),
    owner: str = typer.Option("PlanXLab", help="GitHub repository owner"),
    repo: str = typer.Option("replx_libs", help="GitHub repository name"),
    ref: str = typer.Option("main", help="Git reference (branch/tag)")
):
    """
    Update the local library store for the specified device or the connected device.
    """
    StoreManager.ensure_home_store()

    if device:
        resolved_core = DeviceValidator.find_core_by_device(device)
        if not resolved_core:
            raise typer.Exit(f"Unsupported device: {device}")
        core, dev = resolved_core, device
    else:
        port = _port or os.environ.get("SERIAL_PORT", "")
        info = DeviceScanner.get_board_info(port) if port else None
        if not info:
            for p in list_ports.comports():
                if DeviceScanner.is_bluetooth_port(p):
                    continue
                info = DeviceScanner.get_board_info(p.device)
                if info:
                    break
        if not info:
            OutputHelper.print_panel(
                "Serial port name is missing.\nUse [bright_blue]--port[/bright_blue] option or set [bright_blue]SERIAL_PORT[/bright_blue] environment variable.\nOr run [bright_blue]replx scan[/bright_blue] to find available ports.",
                title="Serial Port Required",
                border_style="red"
            )
            raise typer.Exit(1)
        _, _, core, dev = info

    try:
        remote = StoreManager.load_remote_meta(owner, repo, ref)
    except Exception as e:
        raise typer.BadParameter(f"Failed to load remote meta: {e}")

    try:
        local = StoreManager.load_local_meta()
        if not isinstance(local, dict):
            local = {}
    except Exception:
        local = {}

    items_local = local.setdefault("items", {})
    items_local.setdefault("core", {})
    items_local.setdefault("device", {})
    if "targets" in remote:
        local["targets"] = remote.get("targets") or {}

    def _local_touch_file(scope: str, target: str, part: str, relpath: str, ver: float) -> None:
        scope_node = items_local.setdefault(scope, {})
        tgt_node = scope_node.setdefault(target, {})
        part_node = tgt_node.setdefault(part, {})
        files = part_node.setdefault("files", {})
        segs = relpath.split("/")
        cur = files
        for i, seg in enumerate(segs):
            last = (i == len(segs) - 1)
            ent = cur.get(seg)
            if last:
                if not isinstance(ent, dict) or "files" in (ent or {}):
                    ent = {}
                ent["ver"] = float(ver)
                cur[seg] = ent
            else:
                if not isinstance(ent, dict) or "files" not in ent:
                    ent = {"files": {}}
                    cur[seg] = ent
                cur = ent["files"]

    exts = (".py", ".pyi", ".json")
    bar_len = 40

    def _plan(scope: str, target: str, part: str) -> list[tuple[str, float]]:
        node = RegistryHelper.get_node(remote, scope, target)
        part_node = node.get(part) or {}
        if not part_node:
            return []
        todo: list[tuple[str, float]] = []
        for relpath, _leaf_meta in RegistryHelper.walk_files(part_node, ""):
            if not relpath.endswith(exts):
                continue
            rver = RegistryHelper.effective_version(remote, scope, target, part, relpath)
            try:
                lver = RegistryHelper.effective_version(local, scope, target, part, relpath)
            except Exception:
                lver = 0.0
            if float(lver or 0.0) < float(rver or 0.0):
                todo.append((relpath, rver))
        return todo

    plan = (
        [( "core",  core,  "src",       *x) for x in _plan("core",  core,  "src")] +
        [( "core",  core,  "typehints", *x) for x in _plan("core",  core,  "typehints")] +
        [( "device",dev,   "src",       *x) for x in _plan("device",dev,   "src")] +
        [( "device",dev,   "typehints", *x) for x in _plan("device",dev,   "typehints")]
    )
    total = len(plan)

    if total > 0:
        done = 0
        with Live(OutputHelper.create_progress_panel(done, total, title=f"Updating {dev} on {core}", message=f"Downloading {total} file(s)..."), console=OutputHelper._console, refresh_per_second=10) as live:
            for scope, target, part, relpath, rver in plan:
                repo_path = f"{scope}/{target}/{part}/{relpath}"
                out_path = os.path.join(StoreManager.pkg_root(), repo_path.replace("/", os.sep))
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                try:
                    InstallHelper.download_raw_file(owner, repo, ref, repo_path, out_path)
                except OSError:
                    raise typer.BadParameter("Local store is read-only.")
                _local_touch_file(scope, target, part, relpath, rver)

                done += 1
                live.update(OutputHelper.create_progress_panel(done, total, title=f"Updating {dev} on {core}", message=f"Downloading... {relpath}"))

    StoreManager.save_local_meta(local)
    
    # Show completion panel
    if total == 0:
        message = "All files are already up to date."
    else:
        message = f"[green]{total}[/green] file(s) updated successfully."
    
    OutputHelper.print_panel(
        message,
        title="Update Complete",
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

def _install_spec_internal(spec: str):
    """
    Internal helper to install a spec without CLI recursion.
    Used to maintain REPL session when installing multiple specs (core + device).
    
    :param spec: Specification string (e.g., "core/", "device/")
    """
    if spec.startswith("core/") or spec.startswith("device/"):
        scope, rest = InstallHelper.resolve_spec(spec)
        base, local_list = InstallHelper.list_local_py_targets(scope, rest)
        if not local_list:
            raise typer.BadParameter("No local files to install. Run 'replx update' first.")

        total = len(local_list)
        
        # Pre-compile and prepare batch specs
        batch_specs = []
        unique_dirs = set()
        for abs_py, rel in local_list:
            rel_dir = os.path.dirname(rel)
            remote_dir = InstallHelper.remote_dir_for(scope, rel_dir)
            
            CompilerHelper.compile_to_staging(abs_py, base)
            out_mpy = CompilerHelper.staging_out_for(abs_py, base, CompilerHelper.mpy_arch_tag())
            remote_path = (_device_root_fs + remote_dir + os.path.splitext(os.path.basename(rel))[0] + ".mpy").replace("//", "/")
            
            batch_specs.append((out_mpy, remote_path))
            
            # Collect all unique directory paths
            if remote_dir:
                unique_dirs.add(remote_dir)
        
        # Create all directories in a single REPL session
        if unique_dirs:
            _repl_protocol._enter_repl()
            try:
                for remote_dir in sorted(unique_dirs):
                    parts = [p for p in remote_dir.replace("\\", "/").strip("/").split("/") if p]
                    path = _device_root_fs
                    for p in parts:
                        path = path + p + "/"
                        try:
                            _repl_protocol._exec(f"import os; os.mkdir('{path}')")
                        except Exception:
                            pass
            finally:
                _repl_protocol._leave_repl()
        
        # Use batch upload with progress callback
        def progress_cb(done, total, filename):
            live.update(OutputHelper.create_progress_panel(done, total, title=f"Installing {spec} to {_device}", message=f"Uploading {filename}..."))
        
        with Live(OutputHelper.create_progress_panel(0, total, title=f"Installing {spec} to {_device}", message=f"Processing {total} file(s)..."), console=OutputHelper._console, refresh_per_second=10) as live:
            _file_system.repl.put_files_batch(batch_specs, progress_cb)
        
        OutputHelper.print_panel(
            f"[green]{total}[/green] file(s) installed successfully.",
            title="Installation Complete",
            border_style="green"
        )
    else:
        raise typer.BadParameter(f"Invalid spec format: {spec}")

@app.command(
    name="install",
    help="Install libraries/files onto the device.\n\n" + INSTALL_HELP
)
def install(spec: Optional[str] = typer.Argument(None, metavar="SPEC", help="Target specification")):
    if not _repl_protocol or not _file_system:
        raise typer.BadParameter("Device is not connected.")

    StoreManager.ensure_home_store()

    def _install_local_folder(abs_dir: str):
        # Count total files first
        py_files = []
        for dp, _, fns in os.walk(abs_dir):
            for fn in fns:
                if fn.endswith(".py"):
                    py_files.append(os.path.join(dp, fn))
        
        total = len(py_files)
        if total == 0:
            OutputHelper.print_panel(
                f"No Python files found in [yellow]{abs_dir}[/yellow]",
                title="Installation",
                border_style="yellow"
            )
            return 0
        
        installed = 0
        base = abs_dir
        
        # Pre-compile all files to staging
        compiled_files = []
        for ap in py_files:
            CompilerHelper.compile_to_staging(ap, base)
            rel = os.path.relpath(ap, base).replace("\\", "/")
            remote = (_device_root_fs + rel).replace("\\", "/")
            remote = remote[:-3] + ".mpy"
            out_mpy = CompilerHelper.staging_out_for(ap, base, CompilerHelper.mpy_arch_tag())
            compiled_files.append((out_mpy, remote, os.path.dirname(rel)))
        
        # Create all necessary remote directories
        for _, _, rel_dir in compiled_files:
            try:
                InstallHelper.ensure_remote_dir(rel_dir)
            except Exception:
                pass
        
        # Use batch upload with progress callback
        def progress_cb(done, total, filename):
            live.update(OutputHelper.create_progress_panel(done, total, title=f"Installing {os.path.basename(abs_dir)} to {_device}", message=f"Uploading {filename}..."))
        
        with Live(OutputHelper.create_progress_panel(0, total, title=f"Installing {os.path.basename(abs_dir)} to {_device}", message=f"Processing {total} file(s)..."), console=OutputHelper._console, refresh_per_second=10) as live:
            # Prepare batch specs
            batch_specs = [(local_mpy, remote) for local_mpy, remote, _ in compiled_files]
            
            # Upload all files in batch mode
            _file_system.repl.put_files_batch(batch_specs, progress_cb)
            installed = total
        
        OutputHelper.print_panel(
            f"[green]{installed}[/green] file(s) installed successfully.",
            title="Installation Complete",
            border_style="green"
        )
        return installed

    def _install_single_file(abs_py: str):
        base = os.path.dirname(abs_py)
        name = os.path.basename(abs_py)
        
        with Live(OutputHelper.create_progress_panel(0, 1, title=f"Installing {name} to {_device}", message="Processing..."), console=OutputHelper._console, refresh_per_second=10) as live:
            CompilerHelper.compile_to_staging(abs_py, base)
            out_mpy = CompilerHelper.staging_out_for(abs_py, base, CompilerHelper.mpy_arch_tag())
            if name in ("main.py", "boot.py"):
                remote = (_device_root_fs + name[:-3] + ".mpy").replace("//", "/")
            else:
                remote = (_device_root_fs + "lib/" + name[:-3] + ".mpy").replace("//", "/")
                InstallHelper.ensure_remote_dir("lib")
            
            # Upload without showing individual file panel
            _file_system.put(out_mpy, remote)
            live.update(OutputHelper.create_progress_panel(1, 1, title=f"Installing {name} to {_device}"))
        
        OutputHelper.print_panel(
            f"[green]1[/green] file installed successfully.",
            title="Installation Complete",
            border_style="green"
        )
        return 1

    if spec and (spec.startswith("core/") or spec.startswith("device/")):
        _install_spec_internal(spec)
        return

    if spec and InstallHelper.is_url(spec):
        u = urlparse(spec)
        fname = os.path.basename(u.path)
        if not fname.endswith(".py"):
            raise typer.BadParameter("Only single .py file is supported for URL installs.")
        dl_dir = StoreManager.HOME_STAGING / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        dst = str(dl_dir / fname)
        try:
            with urllib.request.urlopen(spec) as r, open(dst, "wb") as f:
                f.write(r.read())
        except Exception as e:
            raise typer.BadParameter(f"Download failed: {e}")
        try:
            _install_single_file(dst)
        finally:
            try:
                os.remove(dst)
            except Exception:
                pass
        return

    if not spec:
        ok, why = InstallHelper.local_store_ready_for_full_install(_core, _device)
        if not ok:
            msg = "Local store is not ready. Please run 'replx update' (or 'replx update <device>') first."
            if why == "meta-missing":
                msg += " (meta missing)"
            elif why == "meta-broken":
                msg += " (meta invalid)"
            elif why == "dirs-missing":
                msg += " (assets missing)"
            raise typer.BadParameter(msg)

        # Prepare install specs for both core and device (maintain single REPL session)
        specs_to_install = []
        
        core_src = os.path.join(StoreManager.pkg_root(), "core", _core, "src")
        if os.path.isdir(core_src):
            specs_to_install.append("core/")

        dev_src = os.path.join(StoreManager.pkg_root(), "device", _device, "src")
        if os.path.isdir(dev_src):
            specs_to_install.append("device/")
        
        # Install all specs in sequence, maintaining REPL session
        for spec_item in specs_to_install:
            # Use _install_spec_internal to avoid re-entering CLI callback
            _install_spec_internal(spec_item)

        return

    target = spec
    ap = os.path.abspath(target)
    if os.path.isdir(ap):
        _install_local_folder(ap)
        return
    if os.path.isfile(ap):
        if not ap.endswith(".py"):
            raise typer.BadParameter("Only .py is supported for single-file install.")
        _install_single_file(ap)
        return

    raise typer.BadParameter("Target not found. For specs use core/... or device/..., otherwise pass a local path or URL.")

@app.command()
def search(
    lib_name: Optional[str] = typer.Argument(None, help="Library name to search"),
    owner: str = typer.Option("PlanXLab", help="GitHub owner"),
    repo: str = typer.Option("replx_libs", help="GitHub repository"),
    ref: str = typer.Option("main", help="Branch/Tag/SHA"),
    show_all: bool = typer.Option(False, "--all", hidden=True)
):
    """
    Search for libraries/files in the remote registry.
    """
    try:
        remote = StoreManager.load_remote_meta(owner, repo, ref)
    except Exception as e:
        raise typer.BadParameter(f"Failed to load remote registry: {e}")

    try:
        local = StoreManager.load_local_meta()
    except Exception:
        local = {}

    cores, devices = RegistryHelper.root_sections(remote)
    targets = remote.get("targets") or {}

    def device_core_of(dev_name: str) -> str | None:
        dnode = devices.get(dev_name) or {}
        return dnode.get("core") or targets.get(dev_name)

    def local_ver(scope: str, target: str, part: str, relpath: str) -> tuple[float, bool]:
        if not local or not isinstance(local, dict):
            return (0.0, True)
        try:
            v = RegistryHelper.effective_version(local, scope, target, part, relpath)
            node = RegistryHelper.get_node(local, scope, target)
            if not node:
                return (v, True)
            return (v, False)
        except Exception:
            return (0.0, True)

    def add_core_rows(core_name: str, rows: list):
        node = RegistryHelper.get_node(remote, "core", core_name)
        part_node = node.get("src") or {}
        for relpath, _leaf_meta in RegistryHelper.walk_files(part_node, ""):
            if not relpath.endswith(".py"):
                continue
            rver = RegistryHelper.effective_version(remote, "core", core_name, "src", relpath)
            lver, missing = local_ver("core", core_name, "src", relpath)
            rows.append(("core", core_name, SearchHelper.fmt_ver_with_star(rver, lver, missing), f"src/{relpath}"))

    def add_device_rows(dev_name: str, rows: list):
        node = RegistryHelper.get_node(remote, "device", dev_name)
        part_node = node.get("src") or {}
        for relpath, _leaf_meta in RegistryHelper.walk_files(part_node, ""):
            if not relpath.endswith(".py"):
                continue
            rver = RegistryHelper.effective_version(remote, "device", dev_name, "src", relpath)
            lver, missing = local_ver("device", dev_name, "src", relpath)
            rows.append(("device", dev_name, SearchHelper.fmt_ver_with_star(rver, lver, missing), f"src/{relpath}"))

    def resolve_current_dev_core() -> tuple[Optional[str], Optional[str]]:
        cur_dev = (_device or "").strip()
        cur_core = (_core or "").strip()
        dk = SearchHelper.key_ci(devices, cur_dev) if cur_dev else None
        ck = SearchHelper.key_ci(cores, cur_core) if cur_core else None
        if dk:
            mapped_core = device_core_of(dk)
            ck = SearchHelper.key_ci(cores, mapped_core) or ck
            return dk, ck

        port = _port or os.environ.get("SERIAL_PORT", "")
        info = DeviceScanner.get_board_info(port) if port else None

        if not info:
            for p in list_ports.comports():
                if DeviceScanner.is_bluetooth_port(p):
                    continue
                info = DeviceScanner.get_board_info(p.device)
                if info:
                    break

        if info:
            _, _, core, dev = info
            dk = SearchHelper.key_ci(devices, dev)
            ck = SearchHelper.key_ci(cores, core)
            return dk, ck

        return None, None

    rows: list[tuple[str, str, str, str]] = []
    cur_dev_key, cur_core_key = resolve_current_dev_core()

    if show_all:
        for c in sorted(cores.keys(), key=str.lower):
            add_core_rows(c, rows)
        for d in sorted(devices.keys(), key=str.lower):
            add_device_rows(d, rows)
    elif lib_name:
        dkey = SearchHelper.key_ci(devices, lib_name)
        ckey = SearchHelper.key_ci(cores, lib_name)

        if dkey:
            core = device_core_of(dkey)
            core_key = SearchHelper.key_ci(cores, core) if core else None
            if core_key:
                add_core_rows(core_key, rows)
            add_device_rows(dkey, rows)
        elif ckey:
            add_core_rows(ckey, rows)
        else:
            scope_candidates: list[tuple[str, str]] = []
            if cur_dev_key:
                scope_candidates.append(("device", cur_dev_key))
                if cur_core_key:
                    scope_candidates.append(("core", cur_core_key))
            else:
                for c in cores.keys():
                    scope_candidates.append(("core", c))
                for d in devices.keys():
                    scope_candidates.append(("device", d))

            q = lib_name.lower()
            for scope, target in scope_candidates:
                node = RegistryHelper.get_node(remote, scope, target)
                part_node = node.get("src") or {}
                for relpath, _leaf_meta in RegistryHelper.walk_files(part_node, ""):
                    if not relpath.endswith(".py"):
                        continue
                    shown = f"src/{relpath}"
                    if (q in shown.lower()) or (q in target.lower()):
                        rver = RegistryHelper.effective_version(remote, scope, target, "src", relpath)
                        lver, missing = local_ver(scope, target, "src", relpath)
                        rows.append((scope, target, SearchHelper.fmt_ver_with_star(rver, lver, missing), shown))
    else:
        if cur_dev_key:
            if cur_core_key:
                add_core_rows(cur_core_key, rows)
            add_device_rows(cur_dev_key, rows)
        else:
            for c in sorted(cores.keys(), key=str.lower):
                add_core_rows(c, rows)
            for d in sorted(devices.keys(), key=str.lower):
                add_device_rows(d, rows)

    if not rows:
        OutputHelper.print_panel(
            "No results found.",
            title=f"Search Results [{owner}/{repo}@{ref}]",
            border_style="yellow"
        )
        return

    def row_key(r):
        scope_order = 0 if r[0] == "core" else 1
        return (scope_order, r[1].lower(), r[3].lower())

    rows.sort(key=row_key)

    w1 = max(5, max(len(r[0]) for r in rows))  # SCOPE
    w2 = max(6, max(len(r[1]) for r in rows))  # TARGET
    w3 = max(5, max(len(r[2]) for r in rows))  # VER

    lines = []
    lines.append(f"{'SCOPE'.ljust(w1)}  {'TARGET'.ljust(w2)}  {'VER'.ljust(w3)}  FILE")
    lines.append("-" * 80)
    for scope, target, ver_str, shown_path in rows:
        lines.append(f"{scope.ljust(w1)}  {target.ljust(w2)}  {ver_str.ljust(w3)}  {shown_path[4:]}")
    
    OutputHelper.print_panel(
        "\n".join(lines),
        title=f"Search Results [{owner}/{repo}@{ref}]",
        border_style="magenta"
    )

@app.command()
def scan(
    raw: bool = typer.Option(False, "--raw", "-r", help="Enable raw REPL mode for scanning")
):
    """
    Scan and list connected MicroPython boards.
    """
    color_map = {
        0: "yellow",
        1: "green", 
        2: "blue"
    }
    color_pos = 0
    lines = []
    max_width = 0
    
    for port in list_ports.comports():
        if DeviceScanner.is_bluetooth_port(port):
            continue
        descript = DeviceScanner.get_board_info(port.device, raw)
            
        if descript:
            color = color_map[color_pos % len(color_map)]
            if not raw:
                version, date, core, device = descript
                line = f"[{color}]{port.device:>6}[/{color}]\t{version:>4} {date:>11}  [{color}]{device}[/{color}]"
            else:
                line = f"[{color}]{port.device:>6}[/{color}]\t{descript}"
            lines.append(line)
            # Calculate actual display width (excluding Rich markup)
            display_line = line.replace(f"[{color}]", "").replace(f"[/{color}]", "")
            max_width = max(max_width, len(display_line))
            color_pos += 1
    
    # Use wider panel for raw mode or calculated width + padding
    panel_width = max(max_width + 10, 80) if raw else None
    
    if lines:
        OutputHelper.print_panel(
            "\n".join(lines),
            title="Connected MicroPython Devices",
            border_style="cyan"

        )
    else:
        OutputHelper.print_panel(
            "No MicroPython devices found.",
            title="Connected MicroPython Devices",
            border_style="red"
        )

@app.command()
def port(port: Optional[str] = typer.Argument(None, help="Serial port device")):
    """Shows the currently configured port or sets a newly connected port."""
    # Find .vscode/.env file
    cur = os.path.abspath(os.getcwd())
    cfg_path = None
    while True:
        candidate = os.path.join(cur, ".vscode", ".env")
        if os.path.isfile(candidate):
            cfg_path = candidate
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    if port is None:
        if cfg_path:
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    content = f.read()
                m = re.search(r"^SERIAL_PORT=(.*)$", content, flags=re.MULTILINE)
                if m:
                    cur_port = m.group(1).strip()
                    OutputHelper.print_panel(
                        f"Current serial port: [bright_green]{cur_port}[/bright_green]",
                        title="Serial Port",
                        border_style="blue"
                    )
                else:
                    OutputHelper.print_panel(
                        "No serial port configured.",
                        title="Serial Port",
                        border_style="yellow"
                    )
            except Exception:
                OutputHelper.print_panel(
                    "Failed to read .env file.",
                    title="Serial Port",
                    border_style="red"
                )
        else:
            OutputHelper.print_panel(
                "No serial port is configured.\nRun the [bright_blue]env[/bright_blue] command in your project root.",
                title="Serial Port",
                border_style="yellow"
            )
        return


    # set/update
    if not DeviceScanner.is_valid_serial_port(port):
        OutputHelper.print_panel(
            f"Invalid serial port: [red]{port}[/red]",
            title="Serial Port",
            border_style="red"
        )
        return
    if not DeviceScanner.get_board_info(port):
        OutputHelper.print_panel(
            f"Device is not connected to [red]{port}[/red]",
            title="Serial Port",
            border_style="red"
        )
        return

    if not cfg_path:
        OutputHelper.print_panel(
            "Environment configuration is required.\nRun the [bright_blue]env[/bright_blue] command.",
            title="Serial Port",
            border_style="red"
        )
        return

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        content = ""

    port_str = port.upper() if IS_WINDOWS else port
    if re.search(r"^SERIAL_PORT=.*$", content, flags=re.MULTILINE):
        content = re.sub(r"^SERIAL_PORT=.*$", f"SERIAL_PORT={port_str}", content, flags=re.MULTILINE)
    else:
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"SERIAL_PORT={port_str}\n"

    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(content)
        shown = port.upper() if IS_WINDOWS else port
        OutputHelper.print_panel(
            f"Serial port set to: [bright_green]{shown}[/bright_green]",
            title="Serial Port Updated",
            border_style="green"
        )
    except Exception:
        OutputHelper.print_panel(
            "Failed to update .env file.",
            title="Serial Port",
            border_style="red"
        )

def main():
    if len(sys.argv) == 1:
        OutputHelper.print_panel(
            f"Use [bright_blue]replx --help[/bright_blue] to see available commands.",
            title="Replx",
            border_style="green"
        )
        raise SystemExit()

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
        "install","put","get","rm","run","format","search",
        "repl","df","shell","mkdir","ls","reset","env","scan","port","update"
    }

    first_nonopt_idx = next((i for i, a in enumerate(sys.argv[1:], 1) if not a.startswith('-')), None)
    first_nonopt = sys.argv[first_nonopt_idx] if first_nonopt_idx is not None else None

    py_arg_idx = next((i for i, a in enumerate(sys.argv[1:], 1) if a.endswith('.py')), None)

    run_opts = {'-n', '--non-interactive', '-e', '--echo'}

    should_inject_run = (
        ('run' not in args) and
        (py_arg_idx is not None) and
        (first_nonopt is None or first_nonopt not in known)
    )

    if should_inject_run:
        opt_idx = next((i for i, a in enumerate(sys.argv[1:], 1) if a in run_opts), None)
        insert_at = opt_idx if opt_idx is not None else py_arg_idx
        sys.argv.insert(insert_at, 'run')

        first_nonopt_idx = next((i for i, a in enumerate(sys.argv[1:], 1) if not a.startswith('-')), None)
        first_nonopt = sys.argv[first_nonopt_idx] if first_nonopt_idx is not None else None

    suppressed = {'search', 'update', 'scan', 'port'}
    if not any(x in sys.argv for x in ('--help','-h','--version','-v')):
        if (first_nonopt is None) or (first_nonopt not in suppressed):
            UpdateChecker.check_for_updates(__version__)
        
    try:
        EnvironmentManager.load_env_from_rep()
        app()
        exit_code = 0
    except KeyboardInterrupt:
        try:
            if _repl_protocol:
                _repl_protocol.request_interrupt()
        except Exception:
            pass
        print()
        exit_code = 130
    sys.exit(exit_code)


if __name__ == '__main__':
    main()