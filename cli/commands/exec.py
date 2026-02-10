import os
import sys
import time
import re
import threading
import posixpath
import signal
import shlex

import typer
from rich.console import Console
from rich.panel import Panel

from replx.terminal import IS_WINDOWS, getch
from replx.utils.constants import CTRL_C, CTRL_D
from ..helpers import (
    OutputHelper, StoreManager,
    get_panel_box, CONSOLE_WIDTH
)
from ..config import STATE
from ..connection import (
    _ensure_connected, _create_agent_client
)

from ..app import app

from .file import ls, cat, cp, mv, rm, mkdir, touch
from .device import usage


@app.command(name="exec", rich_help_panel="Execution")
def exec_cmd(
    command: str = typer.Argument("", help="MicroPython command to execute"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Run a single MicroPython command on the connected board.

Quick way to execute code without creating a file.

[bold cyan]Usage:[/bold cyan]
  replx exec [yellow]"CODE"[/yellow]
  replx -c [yellow]"CODE"[/yellow]          [dim]# Short form (like python -c)[/dim]

[bold cyan]Arguments:[/bold cyan]
  [yellow]CODE[/yellow]     MicroPython code to execute [red][required][/red]

[bold cyan]Examples:[/bold cyan]
  [dim]# Simple expressions[/dim]
  replx -c "print('hello')"              [dim]# Print message[/dim]
  replx -c "1 + 2 * 3"                   [dim]# Math (prints result)[/dim]

  [dim]# Import and use modules[/dim]
  replx -c "import os; print(os.listdir())"      [dim]# List files[/dim]
  replx -c "import machine; print(machine.freq())"  [dim]# CPU frequency[/dim]

  [dim]# Multi-line with semicolons[/dim]
  replx -c "import time; time.sleep(1); print('done')"

  [dim]# Hardware control[/dim]
  replx -c "from machine import Pin; Pin(25, Pin.OUT).on()"  [dim]# LED on[/dim]

[bold cyan]Tips:[/bold cyan]
  • Use quotes around the code: "..." or '...'
  • Separate multiple statements with semicolons
  • For longer code, use [yellow]replx run script.py[/yellow] instead
  • For interactive work, use [yellow]replx repl[/yellow]

[bold cyan]Related:[/bold cyan]
  replx run script.py     [dim]# Run script file instead[/dim]
  replx repl              [dim]# Interactive MicroPython prompt[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    if not command:
        OutputHelper.print_panel(
            "Missing required argument.\n\n"
            "[bold cyan]Usage:[/bold cyan] replx exec [yellow]COMMAND[/yellow]\n"
            "       replx -c [yellow]COMMAND[/yellow]",
            title="Exec Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    _ensure_connected()
    with _create_agent_client() as client:
        result = client.send_command('exec', code=command)
        if result.get('output'):
            print(result['output'], end='')


@app.command(rich_help_panel="Execution")
def run(
    script_file: str = typer.Argument("", help="Script file to run"),
    non_interactive: bool = typer.Option(False, "--non-interactive", "-n", help="Non-interactive execution"),
    echo: bool = typer.Option(False, "--echo", "-e", help="Turn on echo for interactive"),
    device: bool = typer.Option(False, "--device", "-d", help="Run from device storage"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Run a MicroPython script on the connected board.

By default, runs a file from your computer. Use -d to run from device.

[bold cyan]Usage:[/bold cyan]
  replx run [yellow]SCRIPT[/yellow]           [dim]# Run local file[/dim]
  replx run -d [yellow]SCRIPT[/yellow]        [dim]# Run file from device[/dim]
  replx [yellow]SCRIPT[/yellow]               [dim]# Shortcut (if .py file)[/dim]

[bold cyan]Options:[/bold cyan]
  [yellow]-d, --device[/yellow]          Run from device storage (not local)
  [yellow]-n, --non-interactive[/yellow] Detached mode (don't wait for output)
  [yellow]-e, --echo[/yellow]            Show what's being sent

[bold cyan]Arguments:[/bold cyan]
  [yellow]SCRIPT[/yellow]      Script file to run [red][required][/red]

[bold cyan]Examples:[/bold cyan]
  [dim]# Run local files[/dim]
  replx run main.py               [dim]# Run ./main.py on device[/dim]
  replx main.py                   [dim]# Same (shortcut)[/dim]
  replx run ./tests/test_led.py   [dim]# Run from subdirectory[/dim]

  [dim]# Run files stored on device[/dim]
  replx run -d main.py            [dim]# Run /main.py from device[/dim]
  replx run -d /lib/test.py       [dim]# Run specific path[/dim]

  [dim]# Background execution[/dim]
  replx run -n server.py          [dim]# Start and detach[/dim]
  replx run -dn main.py           [dim]# Run from device, detached[/dim]

[bold cyan]How it works:[/bold cyan]
  • Local files: uploaded to device RAM and executed
  • Device files (-d): executed directly from flash storage
  • Interactive: Ctrl+C to interrupt, output shown in real-time

[bold cyan]Related:[/bold cyan]
  replx -c "code"         [dim]# Run single command instead[/dim]
  replx repl              [dim]# Interactive mode[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    if not script_file:
        typer.echo("Error: Missing required argument 'SCRIPT_FILE'.", err=True)
        raise typer.Exit(1)
    
    if non_interactive and echo:
        typer.echo("Error: The --non-interactive and --echo options cannot be used together.", err=True)
        raise typer.Exit(1)
    
    _ensure_connected()
    
    client = None
    
    local_file = None
    remote_path = None
    device_exec_code = None
    
    if device:
        if script_file.startswith('/'):
            remote_path = script_file
        else:
            remote_path = '/' + script_file
        
        device_root_fs = STATE.device_root_fs or "/"
        if device_root_fs != "/" and not remote_path.startswith(device_root_fs):
            real_path = device_root_fs.rstrip('/') + remote_path
        else:
            real_path = remote_path
        
        try:
            with _create_agent_client() as check_client:
                check_code = f"f=open('{real_path}','rb');f.close();print('OK')"
                result = check_client.send_command('exec', code=check_code)
                output = result.get('output', '').strip()
                if output != 'OK' or result.get('error'):
                    raise FileNotFoundError()
        except Exception:
            OutputHelper.print_panel(
                f"File not found on device: [red]{remote_path}[/red]",
                title="File Not Found",
                border_style="red"
            )
            raise typer.Exit(1)
        
        if remote_path.endswith('.mpy'):
            mod_path = remote_path[1:-4]
            mod_name = mod_path.replace('/', '.')
            if mod_name.startswith('lib.'):
                mod_name = mod_name[4:]
            device_exec_code = f"import sys; sys.modules.pop('{mod_name}', None); import {mod_name}"
        else:
            device_exec_code = f"exec(open('{real_path}').read())"
    else:
        if not os.path.exists(script_file):
            OutputHelper.print_panel(
                f"File not found: [red]{script_file}[/red]\n\n"
                "Check the file path on your PC.\n"
                "To run from device storage: [bright_blue]replx run -d /path/file.py[/bright_blue]",
                title="File Not Found",
                border_style="red"
            )
            raise typer.Exit(1)
        local_file = script_file
    
    client = _create_agent_client()
    
    try:
        if not non_interactive:
            stop_requested = False
            ctrl_c_count = 0  # Track consecutive Ctrl+C presses
            pending_input = []
            stderr_buffer = bytearray()
            stdout_ended_with_newline = True  # Default to True (no extra newline unless output exists without newline)
            
            old_settings = None
            fd = None
            if not IS_WINDOWS:
                import tty
                import termios
                fd = sys.stdin.fileno()
                try:
                    old_settings = termios.tcgetattr(fd)
                except Exception:
                    pass
            
            def output_callback(data: bytes, stream_type: str = "stdout"):
                """Handle streaming output from device."""
                nonlocal ctrl_c_count, stdout_ended_with_newline
                ctrl_c_count = 0
                try:
                    if stream_type == "stderr":
                        stderr_buffer.extend(data)
                    else:
                        # Normalize line endings for all platforms
                        if not IS_WINDOWS:
                            # Unix: CR+LF -> LF, then remove standalone CR
                            data = data.replace(b'\r\n', b'\n')
                            data = data.replace(b'\r', b'')
                        else:
                            # Windows: just remove CR (terminal handles LF)
                            data = data.replace(b'\r', b'')
                        
                        if data:
                            stdout_ended_with_newline = data.endswith(b'\n')
                            sys.stdout.buffer.write(data)
                            sys.stdout.buffer.flush()
                except Exception:
                    pass
            
            def input_provider() -> bytes:
                """Provide keyboard input to device."""
                nonlocal ctrl_c_count, stop_requested
                
                if pending_input:
                    return pending_input.pop(0)
                
                try:
                    if IS_WINDOWS:
                        import msvcrt
                        if msvcrt.kbhit():
                            ch = msvcrt.getwch()
                            if ch == '\x03':
                                ctrl_c_count += 1
                                if ctrl_c_count >= 2:
                                    stop_requested = True
                                    return None
                                return CTRL_C
                            elif ch == '\x04':
                                return CTRL_D
                            elif ch == '\r':  # Enter key
                                if echo:
                                    sys.stdout.write('\r\n')
                                    sys.stdout.flush()
                                return b'\r'
                            elif ch == '\n':  # Also map newline to CR
                                if echo:
                                    sys.stdout.write('\r\n')
                                    sys.stdout.flush()
                                return b'\r'
                            elif ch == '\x08':  # Backspace
                                if echo:
                                    # Erase character: move back, overwrite with space, move back
                                    sys.stdout.write('\b \b')
                                    sys.stdout.flush()
                                return b'\x08'
                            elif ch in ('\x00', '\xe0'):  # Extended key
                                ext = msvcrt.getwch()
                                # Map arrow keys etc.
                                ext_map = {
                                    'H': b'\x1b[A',  # Up
                                    'P': b'\x1b[B',  # Down
                                    'M': b'\x1b[C',  # Right
                                    'K': b'\x1b[D',  # Left
                                }
                                return ext_map.get(ext, b'')
                            else:
                                ctrl_c_count = 0
                                if echo:
                                    sys.stdout.write(ch)
                                    sys.stdout.flush()
                                return ch.encode('utf-8')
                    else:
                        import select
                        r, _, _ = select.select([sys.stdin], [], [], 0)
                        if r:
                            ch = os.read(sys.stdin.fileno(), 1)
                            if ch == CTRL_C:
                                ctrl_c_count += 1
                                if ctrl_c_count >= 2:
                                    stop_requested = True
                                    return None
                                return CTRL_C  # Send to device
                            elif ch == b'\n':  # Enter key on Unix
                                ctrl_c_count = 0
                                if echo:
                                    sys.stdout.buffer.write(b'\r\n')
                                    sys.stdout.buffer.flush()
                                return b'\r'
                            else:
                                ctrl_c_count = 0
                                if echo:
                                    sys.stdout.buffer.write(ch)
                                    sys.stdout.buffer.flush()
                                return ch
                except Exception:
                    pass
                return None
            
            def stop_check() -> bool:
                """Check if stop was requested."""
                return stop_requested
            
            original_sigint = signal.getsignal(signal.SIGINT)
            
            def sigint_handler(signum, frame):
                """Handle SIGINT by queuing Ctrl+C to send to device."""
                nonlocal ctrl_c_count, stop_requested
                ctrl_c_count += 1
                if ctrl_c_count >= 2:
                    stop_requested = True
                else:
                    pending_input.append(CTRL_C)
            
            try:
                signal.signal(signal.SIGINT, sigint_handler)
                
                if not IS_WINDOWS:
                    if old_settings is not None:
                        try:
                            tty.setraw(fd)
                        except Exception:
                            pass
                
                try:
                    if device_exec_code:
                        result = client.run_interactive(
                            script_content=device_exec_code,
                            echo=echo,
                            output_callback=output_callback,
                            input_provider=input_provider,
                            stop_check=stop_check
                        )
                    else:
                        result = client.run_interactive(
                            script_path=local_file,
                            echo=echo,
                            output_callback=output_callback,
                            input_provider=input_provider,
                            stop_check=stop_check
                        )
                except KeyboardInterrupt:
                    stop_requested = True
                    try:
                        client.send_command('run_stop', timeout=0.3)
                    except Exception:
                        pass
                    # Add newline only if output didn't end with one
                    if not stdout_ended_with_newline:
                        print("\n[Interrupted]")
                    return
                
                # Add newline only if output didn't end with one
                if not stdout_ended_with_newline:
                    print()
                
                # Display stderr as error panel if present
                if stderr_buffer:
                    stderr_text = stderr_buffer.decode('utf-8', errors='replace').strip()
                    if stderr_text:
                        script_abs_path = os.path.abspath(local_file) if local_file else None
                        
                        def make_file_link(match):
                            """Convert File "<stdin>", line X to clickable path:line format."""
                            file_ref = match.group(1)
                            line_num = match.group(2)
                            
                            if file_ref == "<stdin>":
                                if script_abs_path:
                                    return f'File "{script_abs_path}", line {line_num}'
                                else:
                                    return match.group(0)
                            else:
                                replx_home = StoreManager.pkg_root()
                                possible_paths = [
                                    os.path.join(replx_home, "core", STATE.core or "", "src", file_ref.lstrip('/')),
                                    os.path.join(replx_home, "device", STATE.device or "", "src", file_ref.lstrip('/')),
                                ]
                                for path in possible_paths:
                                    if os.path.exists(path):
                                        return f'File "{path}", line {line_num}'
                                return match.group(0)
                        
                        linked_text = re.sub(
                            r'File "([^"]+)", line (\d+)',
                            make_file_link,
                            stderr_text
                        )
                        
                        OutputHelper.print_panel(
                            linked_text,
                            title="Execution Error",
                            border_style="red"
                        )
                
            except KeyboardInterrupt:
                stop_requested = True
                try:
                    client.send_command('run_stop', timeout=0.1)
                except Exception:
                    pass
                print("\n[Interrupted by user]")
            except Exception as e:
                error_msg = str(e)
                if 'Not connected' in error_msg:
                    OutputHelper.print_panel(
                        "Not connected to any device.\n\nRun [bright_green]replx --port COM3 setup[/bright_green] first.",
                        title="Connection Required",
                        border_style="red"
                    )
                elif 'busy' in error_msg.lower():
                    OutputHelper.print_panel(
                        f"{error_msg}",
                        title="Device Busy",
                        border_style="yellow"
                    )
                else:
                    OutputHelper.print_panel(
                        f"Error: {str(e)}",
                        title="Execution Failed",
                        border_style="red"
                    )
                raise typer.Exit(1)
            finally:
                signal.signal(signal.SIGINT, original_sigint)
                
                if not IS_WINDOWS:
                    try:
                        import termios
                        if old_settings is not None and fd is not None:
                            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    except Exception:
                        pass
            
            return
        
        if device_exec_code:
            result = client.send_command('run', script_content=device_exec_code, detach=True)
        else:
            result = client.send_command('run', script_path=local_file, detach=True)
        
        display_name = remote_path if remote_path else local_file
        OutputHelper.print_panel(
            f"Script [bright_blue]{display_name}[/bright_blue] sent to device.\n\n"
            "[yellow]⚠ Detached mode:[/yellow] Device may still be executing.\n"
            "Other commands may fail until script completes.",
            title="Script Sent",
            border_style="green"
        )
    except Exception as e:
        error_msg = str(e)
        if 'Not connected' in error_msg:
            OutputHelper.print_panel(
                "Not connected to any device.\n\nRun [bright_green]replx --port COM3 setup[/bright_green] first.",
                title="Connection Required",
                border_style="red"
            )
        else:
            OutputHelper.format_error_output(error_msg.strip().split('\n'), local_file if local_file else script_file)
        raise typer.Exit(1)
    finally:
        if client:
            try:
                client.__exit__(None, None, None)
            except Exception:
                pass


@app.command(rich_help_panel="Interactive")
def repl(
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Enter the REPL (Read-Eval-Print Loop) mode.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Enter interactive MicroPython mode on the connected board.

Start a live MicroPython session where you can type code line by line.

[bold cyan]Usage:[/bold cyan]
  replx repl

[bold cyan]In REPL mode:[/bold cyan]
  • Type MicroPython code and press Enter to execute
  • [yellow]>>>>[/yellow] = ready for input
  • [yellow]....[/yellow] = waiting for more (multi-line)
  • Results are printed immediately

[bold cyan]Exit REPL:[/bold cyan]
  Type [cyan]exit()[/cyan] and press Enter

[bold cyan]Example session:[/bold cyan]
  >>> print("hello")
  hello
  >>> 1 + 2
  3
  >>> import os
  >>> os.listdir()
  ['boot.py', 'main.py', 'lib']
  >>> exit()

[bold cyan]Keyboard shortcuts:[/bold cyan]
  [yellow]Ctrl+C[/yellow]    Interrupt running code
  [yellow]Ctrl+D[/yellow]    Soft reset (restart MicroPython)

[bold cyan]Tips:[/bold cyan]
  • REPL = Read-Eval-Print Loop
  • Great for testing and exploring
  • Changes to variables persist until reset
  • For file editing, use [yellow]replx shell[/yellow] instead

[bold cyan]Related:[/bold cyan]
  replx shell             [dim]# Interactive file management[/dim]
  replx -c "code"         [dim]# Single command instead[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    status = _ensure_connected()
    
    port = status.get('port')
    if not port:
        OutputHelper.print_panel(
            "Could not determine port from agent status.",
            title="REPL Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    initial_output = ""
    try:
        with _create_agent_client() as client:
            result = client.send_command('repl_enter')
            if result.get('error'):
                error_msg = result.get('error', 'Unknown error')
                OutputHelper.print_panel(
                    f"Failed to enter Friendly REPL.\n{error_msg}",
                    title="REPL Error",
                    border_style="red"
                )
                raise typer.Exit(1)
            if not result.get('entered'):
                OutputHelper.print_panel(
                    "Failed to enter Friendly REPL.\nNo prompt received from device.",
                    title="REPL Error",
                    border_style="red"
                )
                raise typer.Exit(1)
            initial_output = result.get('output', '')
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(
            f"Failed to enter Friendly REPL.\nError: {e}",
            title="REPL Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    OutputHelper.print_panel(
        f"Connected to [bright_yellow]{STATE.device}[/bright_yellow] on [bright_green]{STATE.core}[/bright_green]\n"
        f"Type [cyan]exit()[/cyan] and press Enter to exit REPL mode.",
        title="REPL Mode",
        border_style="magenta"
    )
    
    YELLOW = "\033[33m"
    RESET = "\033[0m"
    
    def colorize_prompt(text: str) -> str:
        """Colorize >>> and ... prompts to yellow."""
        text = re.sub(r'^(>>>|\.\.\.)', f'{YELLOW}\\1{RESET}', text, flags=re.MULTILINE)
        return text
    
    if initial_output:
        print(colorize_prompt(initial_output), end="", flush=True)
    
    repl_running = [True]
    reader_client = [None]
    
    def reader_thread_func():
        """Background thread that reads agent output and prints to stdout."""
        try:
            reader_client[0] = _create_agent_client()
            while repl_running[0]:
                try:
                    result = reader_client[0].send_command('repl_read')
                    output = result.get('output', '')
                    if output:
                        output = colorize_prompt(output)
                        if IS_WINDOWS:
                            sys.stdout.buffer.write(output.encode('utf-8').replace(b'\r', b''))
                        else:
                            sys.stdout.buffer.write(output.encode('utf-8'))
                        sys.stdout.buffer.flush()
                except Exception:
                    if not repl_running[0]:
                        break
                    time.sleep(0.05)
                time.sleep(0.005)
        finally:
            if reader_client[0]:
                try:
                    reader_client[0].__exit__(None, None, None)
                except Exception:
                    pass
    
    reader = threading.Thread(target=reader_thread_func, daemon=True, name='REPL-Output')
    reader.start()
    
    writer_client = _create_agent_client()
    
    input_buffer = ""
    
    try:
        while True:
            char = getch()
            
            if char == b'\x00' or not char:
                continue
            
            if char == CTRL_D:
                break
            
            if char in (b'\r', b'\n'):
                if input_buffer.strip() == 'exit()':
                    repl_running[0] = False
                    reader.join(timeout=0.3)
                    try:
                        writer_client.send_command('repl_write', data='\x03')
                    except Exception:
                        pass
                    break
                input_buffer = ""
                try:
                    writer_client.send_command('repl_write', data='\r')
                except Exception:
                    break
            elif char == b'\x7f' or char == b'\x08':
                if input_buffer:
                    input_buffer = input_buffer[:-1]
                try:
                    writer_client.send_command('repl_write', data=char.decode('utf-8', errors='replace'))
                except Exception:
                    break
            else:
                if char >= b' ':
                    try:
                        input_buffer += char.decode('utf-8', errors='ignore')
                    except UnicodeDecodeError:
                        pass
                try:
                    writer_client.send_command('repl_write', data=char.decode('utf-8', errors='replace'))
                except Exception:
                    break
                
    except KeyboardInterrupt:
        pass
    finally:
        repl_running[0] = False
        reader.join(timeout=0.5)
        
        try:
            writer_client.__exit__(None, None, None)
        except Exception:
            pass
        
        try:
            with _create_agent_client() as client:
                client.send_command('repl_exit')
        except Exception:
            pass
    
    print()


@app.command(rich_help_panel="Interactive")
def shell(
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Enter an interactive shell for device control.
    Provides a shell-like environment where you can run replx commands without the 'replx' prefix.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Enter interactive shell for managing files on the device.

Use familiar commands (ls, cd, cat, etc.) without typing "replx" each time.

[bold cyan]Usage:[/bold cyan]
  replx shell

[bold cyan]File commands (same as replx commands):[/bold cyan]
  [yellow]ls[/yellow] [path]              List directory contents
  [yellow]cat[/yellow] file               Show file contents
  [yellow]cp[/yellow] src dest            Copy file/directory
  [yellow]mv[/yellow] src dest            Move/rename
  [yellow]rm[/yellow] [-rf] file          Remove file/directory
  [yellow]mkdir[/yellow] dir              Create directory
  [yellow]touch[/yellow] file             Create empty file

[bold cyan]Navigation commands (shell-only):[/bold cyan]
  [yellow]cd[/yellow] path                Change directory
  [yellow]pwd[/yellow]                    Print current directory
  [yellow]clear[/yellow]                  Clear screen

[bold cyan]Other commands:[/bold cyan]
  [yellow]usage[/yellow]                  Show memory/storage usage
  [yellow]exec[/yellow] "code"            Run MicroPython code
  [yellow]run[/yellow] script.py          Run script from device
  [yellow]repl[/yellow]                   Enter Python REPL
  [yellow]edit[/yellow] file              Open file in VSCode
  [yellow]exit[/yellow] or [yellow]quit[/yellow]          Exit shell
  [yellow]help[/yellow] [command]         Show help

[bold cyan]Example session:[/bold cyan]
  [ticle]:/ > ls
  [dim]  boot.py  main.py  lib/[/dim]
  [ticle]:/ > cd lib
  [ticle]:/lib > cat audio.py
  [dim]  ... file contents ...[/dim]
  [ticle]:/lib > exit

[bold cyan]Tips:[/bold cyan]
  • Shell stays connected - faster than running replx repeatedly
  • Tab key not supported (use full filenames)
  • Type 'help cmd' for help on any command
  • Current directory is remembered until you exit

[bold cyan]Related:[/bold cyan]
  replx repl              [dim]# Interactive MicroPython instead[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    _ensure_connected()
    
    SHELL_COMMANDS = {
        'ls', 'cat', 'cp', 'mv', 'rm', 'mkdir', 'touch', 'usage', 'exec', 'repl', 'run',
        'cd', 'pwd', 'clear', 'edit', 'exit', 'help', '?', 'wifi'
    }
    
    EXCLUDED_COMMANDS = {
        'version', 'setup', 'scan', 'shell', 'reset', 'get', 'put', 'format', 
        'init', 'install', 'update', 'search'
    }
    
    current_path = '/'
    
    def print_prompt():
        print(f"\n[{STATE.device}]:{current_path} > ", end="", flush=True)

    def print_shell_help(cmd: str):
        """Print help for a shell command by calling the corresponding replx command with --help."""
        shell_console = Console(width=CONSOLE_WIDTH)
        
        shell_only_help = {
            "cd": """\
[bold cyan]Usage:[/bold cyan]
  cd [yellow]DIRECTORY[/yellow]

[bold cyan]Description:[/bold cyan]
  Change current working directory

[bold cyan]Examples:[/bold cyan]
  cd /lib      [dim]# Change to /lib[/dim]
  cd ..        [dim]# Go to parent directory[/dim]
  cd subdir    [dim]# Enter subdirectory[/dim]""",
            
            "pwd": """\
[bold cyan]Usage:[/bold cyan]
  pwd

[bold cyan]Description:[/bold cyan]
  Print current working directory""",
            
            "clear": """\
[bold cyan]Usage:[/bold cyan]
  clear

[bold cyan]Description:[/bold cyan]
  Clear the terminal screen""",
            
            "exit": """\
[bold cyan]Usage:[/bold cyan]
  exit

[bold cyan]Description:[/bold cyan]
  Exit the shell and return to normal terminal""",
            
            "edit": """\
[bold cyan]Usage:[/bold cyan]
  edit [yellow]FILE[/yellow]

[bold cyan]Description:[/bold cyan]
  Edit a file from device in VSCode
  - Downloads file to .temp folder
  - Opens in VSCode and waits for close
  - Prompts to upload if file was modified

[bold cyan]Examples:[/bold cyan]
  edit main.py        [dim]# Edit main.py[/dim]
  edit /lib/utils.py  [dim]# Edit with absolute path[/dim]""",
            
            "help": """\
[bold cyan]Usage:[/bold cyan]
  help [yellow][COMMAND][/yellow]
  ? [yellow][COMMAND][/yellow]

[bold cyan]Description:[/bold cyan]
  Show help information

[bold cyan]Examples:[/bold cyan]
  help       [dim]# Show all commands[/dim]
  help ls    [dim]# Show help for ls command[/dim]""",
        }
        shell_only_help["?"] = shell_only_help["help"]
        
        if cmd in shell_only_help:
            shell_console.print(Panel(shell_only_help[cmd], border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
            return
        
        try:
            if cmd == "ls":
                ls(path="/", recursive=False, show_help=True)
            elif cmd == "cat":
                cat(remote="", encoding="utf-8", show_help=True)
            elif cmd == "cp":
                cp(args=None, recursive=False, show_help=True)
            elif cmd == "mv":
                mv(args=None, recursive=False, show_help=True)
            elif cmd == "rm":
                rm(args=None, recursive=False, show_help=True)
            elif cmd == "mkdir":
                mkdir(remotes=None, show_help=True)
            elif cmd == "touch":
                touch(remotes=None, show_help=True)
            elif cmd == "usage":
                usage(show_help=True)
            elif cmd == "exec":
                exec_cmd(command="", show_help=True)
            elif cmd == "repl":
                repl(show_help=True)
            elif cmd == "run":
                shell_console.print(Panel("""\
Run a script from device storage.
In shell mode, 'run' always runs from device (equivalent to 'replx run -d').

[bold cyan]Usage:[/bold cyan]
  run [yellow]SCRIPT_FILE[/yellow]

[bold cyan]Arguments:[/bold cyan]
  [yellow]SCRIPT_FILE[/yellow]  Script file path on device [red][required][/red]

[bold cyan]Examples:[/bold cyan]
  run main.py           [dim]# Run /main.py from device[/dim]
  run lib/test.py       [dim]# Run /lib/test.py from device[/dim]
  run t1.mpy            [dim]# Run .mpy file from device[/dim]

[bold yellow]Note:[/bold yellow]
  In shell mode, -e and -n options are not available.
  Use 'replx run' directly for those options.""", border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
            elif cmd == "wifi":
                shell_console.print(Panel("""\
Manage WiFi connection.

[bold cyan]Usage:[/bold cyan]
  wifi                     [dim]# Show WiFi status[/dim]
  wifi [yellow]SSID PW[/yellow]             [dim]# Connect and save config[/dim]
  wifi off                 [dim]# Disable WiFi[/dim]
  wifi scan                [dim]# Scan for networks[/dim]

[bold cyan]Examples:[/bold cyan]
  wifi                          [dim]# Check status[/dim]
  wifi MyNetwork secret123      [dim]# Connect[/dim]
  wifi scan                     [dim]# Find networks[/dim]""", border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
            else:
                shell_console.print(f"No help available for '{cmd}'")
        except typer.Exit:
            pass 

    def run_shell_cmd(cmdline):
        nonlocal current_path

        args = shlex.split(cmdline)
        if not args:
            return
        cmd = args[0]
        
        if cmd in EXCLUDED_COMMANDS:
            OutputHelper.print_panel(
                f"[yellow]'{cmd}'[/yellow] is not available in shell mode.",
                title="Command Not Available",
                border_style="yellow"
            )
            return
        
        if cmd not in SHELL_COMMANDS:
            OutputHelper.print_panel(
                f"[red]'{cmd}'[/red] is not a valid command.\n\nType [bright_blue]help[/bright_blue] or [bright_blue]?[/bright_blue] to see available commands.",
                title="Unknown Command",
                border_style="red"
            )
            return

        try:
            if cmd == "help" or cmd == "?":
                if len(args) > 1:
                    print_shell_help(args[1])
                else:
                    shell_console = Console(width=CONSOLE_WIDTH)
                    help_text = """\
[bold cyan]Commands:[/bold cyan]
  [yellow]ls[/yellow] [path] [-r]      List files/directories
  [yellow]cat[/yellow] <file>          Display file contents
  [yellow]cp[/yellow] <src...> <dst>   Copy files (use -r for directories)
  [yellow]mv[/yellow] <src...> <dst>   Move/rename files (use -r for directories)
  [yellow]rm[/yellow] <files...>       Remove files (use -r for directories)
  [yellow]mkdir[/yellow] <dirs...>     Create directories
  [yellow]touch[/yellow] <files...>    Create empty files
  [yellow]usage[/yellow]               Show memory/storage usage
  [yellow]exec[/yellow] <code>         Execute MicroPython code
  [yellow]repl[/yellow]                Enter Python REPL
  [yellow]run[/yellow] <script>        Run script from device
  [yellow]wifi[/yellow] [args]         Manage WiFi
  [yellow]cd[/yellow] <dir>            Change directory
  [yellow]pwd[/yellow]                 Print current directory
  [yellow]clear[/yellow]               Clear screen
  [yellow]edit[/yellow] <file>          Edit file in VSCode
  [yellow]exit[/yellow]                Exit shell

[dim]Type 'help <command>' for detailed help on a specific command.[/dim]"""
                    shell_console.print(Panel(help_text, title="Available Commands", title_align="left", border_style="cyan", box=get_panel_box(), width=CONSOLE_WIDTH))
                return
                
            elif cmd == "exit":
                raise SystemExit()
                
            elif cmd == "pwd":
                if "--help" in args or "-h" in args:
                    print_shell_help("pwd")
                    return
                print(current_path)
                return
                
            elif cmd == "clear":
                if "--help" in args or "-h" in args:
                    print_shell_help("clear")
                    return
                OutputHelper._console.clear()
                return
                
            elif cmd == "cd":
                if "--help" in args or "-h" in args:
                    print_shell_help("cd")
                    return
                    
                if len(args) != 2:
                    print("Usage: cd <directory>")
                    return
                
                new_path = posixpath.normpath(posixpath.join(current_path, args[1]))
                try:
                    client = _create_agent_client()
                    result = client.send_command('is_dir', path=new_path)
                    is_dir = result if isinstance(result, bool) else result.get('is_dir', False)
                    if is_dir:
                        current_path = new_path
                    else:
                        print(f"cd: {args[1]}: Not a directory")
                except Exception:
                    print(f"cd: {args[1]}: No such directory")
                return
                
            elif cmd == "ls":
                if "--help" in args or "-h" in args:
                    print_shell_help("ls")
                    return
                    
                path_arg = current_path
                recursive = False
                
                for arg in args[1:]:
                    if arg in ("-r", "--recursive"):
                        recursive = True
                    elif not arg.startswith('-'):
                        path_arg = posixpath.normpath(posixpath.join(current_path, arg))
                
                ls(path=path_arg, recursive=recursive, show_help=False)
                return
                
            elif cmd == "cat":
                if "--help" in args or "-h" in args:
                    print_shell_help("cat")
                    return
                    
                number = False
                lines_opt = None
                file_arg = None
                encoding = "utf-8"
                
                i = 1
                while i < len(args):
                    arg = args[i]
                    if arg in ("-n", "--number"):
                        number = True
                    elif arg in ("-L", "--lines"):
                        if i + 1 < len(args):
                            lines_opt = args[i + 1]
                            i += 1
                    elif arg in ("-e", "--encoding"):
                        if i + 1 < len(args):
                            encoding = args[i + 1]
                            i += 1
                    elif not arg.startswith('-'):
                        file_arg = arg
                    i += 1
                
                if not file_arg:
                    print("Usage: cat <file>")
                    return
                    
                remote = posixpath.normpath(posixpath.join(current_path, file_arg))
                cat(remote=remote, encoding=encoding, number=number, lines=lines_opt, show_help=False)
                return
                
            elif cmd == "cp":
                if "--help" in args or "-h" in args:
                    print_shell_help("cp")
                    return
                    
                recursive = False
                file_args = []
                
                for arg in args[1:]:
                    if arg in ("-r", "--recursive"):
                        recursive = True
                    else:
                        file_args.append(arg)
                
                if len(file_args) < 2:
                    print("Usage: cp [-r] <source...> <dest>")
                    return
                
                abs_args = [posixpath.normpath(posixpath.join(current_path, arg)) for arg in file_args]
                cp(args=abs_args, recursive=recursive, show_help=False)
                return
                
            elif cmd == "mv":
                if "--help" in args or "-h" in args:
                    print_shell_help("mv")
                    return
                    
                recursive = False
                file_args = []
                
                for arg in args[1:]:
                    if arg in ("-r", "--recursive"):
                        recursive = True
                    else:
                        file_args.append(arg)
                
                if len(file_args) < 2:
                    print("Usage: mv [-r] <source...> <dest>")
                    return
                
                abs_args = [posixpath.normpath(posixpath.join(current_path, arg)) for arg in file_args]
                mv(args=abs_args, recursive=recursive, show_help=False)
                return
                
            elif cmd == "rm":
                if "--help" in args or "-h" in args:
                    print_shell_help("rm")
                    return
                    
                recursive = False
                file_args = []
                
                for arg in args[1:]:
                    if arg in ("-r", "--recursive"):
                        recursive = True
                    else:
                        file_args.append(arg)
                
                if not file_args:
                    print("Usage: rm [-r] <files...>")
                    return
                
                abs_args = [posixpath.normpath(posixpath.join(current_path, arg)) for arg in file_args]
                rm(args=abs_args, recursive=recursive, show_help=False)
                return
                
            elif cmd == "mkdir":
                if "--help" in args or "-h" in args:
                    print_shell_help("mkdir")
                    return
                    
                if len(args) < 2:
                    print("Usage: mkdir <directories...>")
                    return
                
                abs_args = [posixpath.normpath(posixpath.join(current_path, arg)) for arg in args[1:]]
                mkdir(remotes=abs_args, show_help=False)
                return
                
            elif cmd == "touch":
                if "--help" in args or "-h" in args:
                    print_shell_help("touch")
                    return
                    
                if len(args) < 2:
                    print("Usage: touch <files...>")
                    return
                
                abs_args = [posixpath.normpath(posixpath.join(current_path, arg)) for arg in args[1:]]
                touch(remotes=abs_args, show_help=False)
                return
                
            elif cmd == "usage":
                if "--help" in args or "-h" in args:
                    print_shell_help("usage")
                    return
                usage(show_help=False)
                return
                
            elif cmd == "exec":
                if "--help" in args or "-h" in args:
                    print_shell_help("exec")
                    return
                    
                if len(args) < 2:
                    print("Usage: exec <python_code>")
                    return
                
                code = ' '.join(args[1:])
                exec_cmd(command=code, show_help=False)
                return
                
            elif cmd == "repl":
                if "--help" in args or "-h" in args:
                    print_shell_help("repl")
                    return
                repl(show_help=False)
                return
                
            elif cmd == "run":
                if "--help" in args or "-h" in args:
                    print_shell_help("run")
                    return
                    
                if "-e" in args or "--echo" in args:
                    print("Error: -e/--echo option is not available in shell mode.")
                    return
                if "-n" in args or "--non-interactive" in args:
                    print("Error: -n/--non-interactive option is not available in shell mode.")
                    return
                    
                if len(args) != 2:
                    print("Usage: run <script_file>")
                    return
                
                script_file = args[1]
                if script_file.startswith('/'):
                    remote_path = script_file
                else:
                    remote_path = posixpath.normpath(posixpath.join(current_path, script_file))
                
                run(script_file=remote_path, non_interactive=False, echo=False, device=True, show_help=False)
                return
                
            elif cmd == "edit":
                if "--help" in args or "-h" in args:
                    print_shell_help("edit")
                    return
                    
                if len(args) != 2:
                    print("Usage: edit <file>")
                    return
                
                file_arg = args[1]
                if file_arg.startswith('/'):
                    remote_path = file_arg
                else:
                    remote_path = posixpath.normpath(posixpath.join(current_path, file_arg))
                
                temp_dir = os.path.join(os.getcwd(), '.temp')
                os.makedirs(temp_dir, exist_ok=True)
                
                filename = posixpath.basename(remote_path)
                local_path = os.path.join(temp_dir, filename)
                
                try:
                    client = _create_agent_client()
                    
                    try:
                        result = client.send_command('is_dir', path=remote_path)
                        is_dir = result if isinstance(result, bool) else result.get('is_dir', False)
                        if is_dir:
                            print(f"Error: '{remote_path}' is a directory, not a file.")
                            return
                    except Exception:
                        pass
                    
                    original_hash = None
                    try:
                        result = client.send_command('get_to_local', remote_path=remote_path, local_path=local_path)
                        if isinstance(result, dict) and result.get('error'):
                            with open(local_path, 'w', encoding='utf-8') as f:
                                pass
                            print(f"Creating new file: {remote_path}")
                        else:
                            print(f"Downloaded: {remote_path}")
                    except Exception:
                        with open(local_path, 'w', encoding='utf-8') as f:
                            pass
                        print(f"Creating new file: {remote_path}")
                    
                    with open(local_path, 'rb') as f:
                        original_hash = hashlib.md5(f.read()).hexdigest()
                    
                    print("Opening in VSCode... (close the file tab to continue)")
                    try:
                        subprocess.run(['code', '--wait', local_path], shell=True)
                    except FileNotFoundError:
                        print("Error: 'code' command not found. Make sure VSCode is in PATH.")
                        return
                    
                    with open(local_path, 'rb') as f:
                        new_hash = hashlib.md5(f.read()).hexdigest()
                    
                    if new_hash == original_hash:
                        print("No changes detected.")
                    else:
                        print("File was modified. Apply the changes? [y/N]: ", end="", flush=True)
                        response = sys.stdin.buffer.readline().decode(errors='replace').strip().lower()
                        
                        if response == 'y':
                            result = client.send_command('put_from_local', local_path=local_path, remote_path=remote_path)
                            if isinstance(result, dict) and result.get('error'):
                                print(f"Upload failed: {result.get('error')}")
                            else:
                                file_size = os.path.getsize(local_path)
                                print(f"Uploaded: {remote_path} ({file_size} bytes)")
                        else:
                            print("Changes discarded.")
                
                finally:
                    try:
                        if os.path.exists(temp_dir):
                            shutil.rmtree(temp_dir)
                    except Exception:
                        pass
                return
            
            elif cmd == "wifi":
                if "--help" in args or "-h" in args:
                    print_shell_help("wifi")
                    return
                
                from replx.cli.commands.device import wifi
                
                if len(args) == 1:
                    wifi(args=None, show_help=False)
                elif len(args) == 2:
                    subarg = args[1]
                    if subarg == "scan":
                        wifi(args=["scan"], show_help=False)
                    elif subarg == "off":
                        wifi(args=["off"], show_help=False)
                    else:
                        print("Usage: wifi [SSID PASSWORD | scan | off]")
                elif len(args) == 3:
                    subarg1 = args[1]
                    subarg2 = args[2]
                    wifi(args=[subarg1, subarg2], show_help=False)
                else:
                    print("Usage: wifi [SSID PASSWORD | scan | off]")
                return
                
        except typer.Exit:
            pass
        except SystemExit:
            raise
        except Exception as e:
            print(f"Error: {e}")
    
    header_content = f"Connected to [bright_yellow]{STATE.device}[/bright_yellow] on [bright_green]{STATE.core}[/bright_green]\n\n"
    header_content += "Type [bright_blue]help[/bright_blue] or [bright_blue]?[/bright_blue] to see available commands\n"
    header_content += "Type [bright_blue]exit[/bright_blue] to quit shell"
    
    OutputHelper.print_panel(
        header_content,
        title="Interactive Shell",
        border_style="cyan"
    )

    shell_running = True
    
    def signal_handler(sig, frame):
        print("\nType 'exit' to quit shell.")
    
    old_handler = signal.signal(signal.SIGINT, signal_handler)
    
    try:
        while shell_running:
            try:
                print_prompt()
                line = sys.stdin.buffer.readline().decode(errors='replace').rstrip()
                if not line:
                    continue
                try:
                    run_shell_cmd(line)
                except SystemExit:
                    break
                except Exception as e:
                    print(f"Error: {e}")
            except EOFError:
                break
            
    finally:
        signal.signal(signal.SIGINT, old_handler)
        OutputHelper.print_panel(
            "Shell session ended.",
            title="Exit Shell",
            border_style="cyan"
        )


@app.command(rich_help_panel="Device Management")
def reset(
    hard: bool = typer.Option(False, "--hard", help="Hard reset (full hardware reset)"),
    soft: bool = typer.Option(False, "--soft", help="Soft reset (default, restarts interpreter)"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Reset the connected device.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Reset the connected MicroPython device.

[bold cyan]Usage:[/bold cyan] replx reset [yellow][OPTIONS][/yellow]

[bold cyan]Options:[/bold cyan]
  [yellow]--soft[/yellow]      Soft reset (default) - restarts Python interpreter
  [yellow]--hard[/yellow]      Hard reset - full hardware reset like RESET button

[bold cyan]Examples:[/bold cyan]
  replx reset               [dim]Soft reset (default)[/dim]
  replx reset --soft        [dim]Soft reset explicitly[/dim]
  replx reset --hard        [dim]Hard reset with auto-reconnect[/dim]

[bold cyan]When to use:[/bold cyan]
  • [yellow]--soft[/yellow]: After uploading code, to restart cleanly
    • Clears all Python variables, objects, imported modules
    • Frees all Python memory and restarts interpreter
    • Resets most peripherals (Pin.irq, Bluetooth, sockets, etc.)
    • [dim]Preserves:[/dim] Wi-Fi/network connections, RTC, CPU clock, Pin modes
    • [dim]Wi-Fi:[/dim] Stays connected - no reconnect needed
  • [yellow]--hard[/yellow]: When soft reset doesn't respond
    • Complete reset like pushing physical RESET button
    • Clears all hardware state (except RTC)
    • USB disconnects briefly, then auto-reconnects
    • [dim]Wi-Fi:[/dim] Disconnects - must call wlan.connect() again
  
[bold cyan]Note:[/bold cyan]
  Both resets run boot.py → main.py sequence after restart."""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    if hard and soft:
        OutputHelper.print_panel(
            "Cannot use both [yellow]--hard[/yellow] and [yellow]--soft[/yellow] at the same time.",
            title="Reset Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    reset_type = "hard" if hard else "soft"
    
    status = _ensure_connected()
    device = status.get('device', 'unknown')
    
    if reset_type == "hard":
        _reset_hard(device, status)
    else:
        _reset_soft(device, status)


def _reset_soft(device: str, status: dict):
    """Perform soft reset (Ctrl+C → Ctrl+D)."""
    try:
        with _create_agent_client() as client:
            client.send_command('reset')
        
        if status.get('core') == 'EFR32MG':
            try:
                client = _create_agent_client()
                client.send_command('free')
            except Exception:
                pass
        
        OutputHelper.print_panel(
            f"Device [bright_yellow]{device}[/bright_yellow] has been soft reset.",
            title="Soft Reset",
            border_style="blue"
        )
    except Exception as e:
        error_msg = str(e)
        if "soft reset failed" in error_msg.lower():
            OutputHelper.print_panel(
                f"Soft reset failed: {error_msg}\n\n"
                "[yellow]Tip:[/yellow] If code catches KeyboardInterrupt, soft reset cannot work.\n"
                "Try [bright_blue]replx reset hard[/bright_blue] instead.",
                title="Reset Error",
                border_style="red"
            )
        else:
            OutputHelper.print_panel(
                f"Soft reset failed: {error_msg}",
                title="Reset Error",
                border_style="red"
            )
        raise typer.Exit(1)


def _reset_hard(device: str, status: dict):
    """Perform hard reset (machine.reset()) with auto-reconnect."""
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.text import Text
    
    port = status.get('port', '')
    console = Console()
    
    spinner = Spinner("dots", text=Text(" Executing hard reset...", style="bright_cyan"))
    
    try:
        with Live(spinner, console=console, refresh_per_second=10, transient=True):
            with _create_agent_client() as client:
                try:
                    client.send_command('exec', code='import machine; machine.reset()', timeout=2.0)
                except Exception:
                    pass
        
        spinner = Spinner("dots", text=Text(" Waiting for device to restart...", style="yellow"))
        with Live(spinner, console=console, refresh_per_second=10, transient=True):
            time.sleep(2.0)
        
        spinner = Spinner("dots", text=Text(" Reconnecting...", style="bright_cyan"))
        with Live(spinner, console=console, refresh_per_second=10, transient=True):
            try:
                with _create_agent_client() as client:
                    client.send_command('disconnect_port', port=port, timeout=2.0)
            except Exception:
                pass
            
            time.sleep(1.0)
            
            reconnect_attempts = 5
            reconnected = False
            
            for attempt in range(reconnect_attempts):
                try:
                    time.sleep(1.0)
                    with _create_agent_client() as client:
                        result = client.send_command(
                            'session_setup',
                            port=port,
                            as_foreground=True,
                            timeout=5.0
                        )
                        if result.get('connected'):
                            reconnected = True
                            break
                except Exception:
                    continue
            
            if not reconnected:
                try:
                    _ensure_connected()
                    reconnected = True
                except Exception:
                    pass
        
        if reconnected:
            OutputHelper.print_panel(
                f"Device [bright_yellow]{device}[/bright_yellow] has been hard reset and reconnected.",
                title="Hard Reset",
                border_style="green"
            )
        else:
            OutputHelper.print_panel(
                f"Device [bright_yellow]{device}[/bright_yellow] was reset but auto-reconnect failed.\n\n"
                "Please reconnect manually with [bright_blue]replx --port {port} setup[/bright_blue]",
                title="Hard Reset",
                border_style="yellow"
            )
    except Exception as e:
        OutputHelper.print_panel(
            f"Hard reset failed: {str(e)}",
            title="Reset Error",
            border_style="red"
        )
        raise typer.Exit(1)
