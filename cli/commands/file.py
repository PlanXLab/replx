import os
import sys
import time
import posixpath
import glob
import fnmatch
import threading
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.live import Live

from replx.utils.exceptions import ProtocolError
from ..helpers import OutputHelper, get_panel_box, CONSOLE_WIDTH
from ..connection import _ensure_connected, _create_agent_client
from ..app import app


@app.command(rich_help_panel="File Operations")
def get(
    args: Optional[list[str]] = typer.Argument(None, help="Remote file(s) and local destination"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Download file(s) or directory from the connected device to the local filesystem.
    Last argument is the local destination path.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Download files or directories from the device to your computer.

[bold cyan]Usage:[/bold cyan]
  replx get [yellow]REMOTE[/yellow] [yellow]LOCAL[/yellow]
  replx get [yellow]REMOTE...[/yellow] [yellow]LOCAL[/yellow]   [dim]# Multiple files[/dim]

[bold cyan]Arguments:[/bold cyan]
  [yellow]REMOTE[/yellow]    File(s) or directory on device [red][required][/red]
  [yellow]LOCAL[/yellow]     Destination on your computer [red][required][/red]

[bold cyan]Examples:[/bold cyan]
  [dim]# Single file[/dim]
  replx get main.py ./               [dim]# Download to current dir[/dim]
  replx get /lib/audio.py ./backup   [dim]# Download to backup folder[/dim]

  [dim]# Directory (recursive)[/dim]
  replx get /lib ./mylib             [dim]# Download entire /lib[/dim]

  [dim]# Multiple files[/dim]
  replx get /a.py /b.py ./           [dim]# Download multiple files[/dim]

  [dim]# Wildcards[/dim]
  replx get /*.py ./backup           [dim]# All .py files from root[/dim]
  replx get /lib/*.mpy ./            [dim]# All .mpy from /lib[/dim]
  replx get test*.py ./              [dim]# Files starting with test[/dim]

[bold cyan]Note:[/bold cyan]
  • Last argument is always the local destination
  • Directories are downloaded recursively
  • Wildcards (*,?) are expanded on the device

[bold cyan]Related:[/bold cyan]
  replx put local /remote   [dim]# Upload files instead[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    status = _ensure_connected()
    device_root_fs = status.get('device_root_fs', '/')
    
    if not args or len(args) < 2:
        OutputHelper.print_panel(
            "Missing required arguments.\n\n"
            "[bold cyan]Usage:[/bold cyan] replx get [yellow]REMOTE... LOCAL[/yellow]\n\n"
            "At least 2 arguments required: remote file(s) and local destination.",
            title="Download Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    # Get raw arguments from sys.argv to avoid Typer's shell expansion
    cmd_idx = next((i for i, arg in enumerate(sys.argv) if arg == 'get'), None)
    if cmd_idx is not None and cmd_idx + 1 < len(sys.argv):
        raw_args = []
        for arg in sys.argv[cmd_idx + 1:]:
            if arg.startswith('-'):
                continue
            raw_args.append(arg)
        if len(raw_args) >= 2:
            args = raw_args
    
    # Last argument is local destination
    local = args[-1]
    remotes = args[:-1]
    
    # Helper function to normalize remote path
    def normalize_remote(path: str) -> str:
        if not path.startswith('/'):
            path = '/' + path
        return path
    
    # Use AgentClient for all operations
    client = _create_agent_client()
    
    # Expand wildcards and collect all files to download
    files_to_download = []
    
    for remote_pattern in remotes:
        remote = normalize_remote(remote_pattern)
        
        # Check for wildcards
        if '*' in remote_pattern or '?' in remote_pattern:
            # Extract directory and pattern
            dir_path = posixpath.dirname(remote) or '/'
            basename_pattern = posixpath.basename(remote)
            
            try:
                result = client.send_command('ls', path=dir_path, detailed=True)
                items = [(item['name'], item['size'], item['is_dir']) for item in result.get('items', [])]
                
                for name, size, is_dir in items:
                    if fnmatch.fnmatch(name, basename_pattern):
                        full_path = posixpath.join(dir_path, name)
                        files_to_download.append((full_path, name, is_dir))
            except Exception:
                OutputHelper.print_panel(
                    f"[red]{remote_pattern}[/red] - pattern did not match any files.",
                    title="Download Failed",
                    border_style="red"
                )
                continue
        else:
            # Single file/directory
            try:
                result = client.send_command('is_dir', path=remote)
                is_dir = result.get('is_dir', False)
                basename = posixpath.basename(remote.rstrip('/'))
                files_to_download.append((remote, basename, is_dir))
            except Exception:
                display_remote = remote.replace(device_root_fs, "", 1)
                OutputHelper.print_panel(
                    f"[red]{display_remote}[/red] does not exist.",
                    title="Download Failed",
                    border_style="red"
                )
                continue
    
    if not files_to_download:
        OutputHelper.print_panel(
            "No files to download.",
            title="Download",
            border_style="yellow"
        )
        raise typer.Exit(1)
    
    # Download files
    total_files = len(files_to_download)
    success_count = 0
    
    if total_files == 1:
        # Single file/directory - use original behavior
        remote, basename, is_dir = files_to_download[0]
        display_remote = remote.replace(device_root_fs, "", 1)
        
        # Determine local destination
        if os.path.exists(local) and os.path.isdir(local):
            local_path = os.path.join(local, basename)
        else:
            local_path = local
        
        try:
            if is_dir:
                # Use streaming directory download from server
                progress_state = {"current": 0, "total": 0, "file": "", "status": "starting"}
                
                def progress_callback(progress_data):
                    progress_state.update(progress_data)
                
                # Start streaming download
                with Live(OutputHelper.create_progress_panel(0, 1, title=f"Downloading {basename}", message="Scanning directory..."), console=OutputHelper._console, refresh_per_second=10) as live:
                    result_holder = {"result": None, "error": None, "done": False}
                    
                    def stream_callback(data):
                        progress_state.update(data)
                    
                    def download_task():
                        try:
                            result = client.send_command_streaming(
                                'getdir_to_local',
                                remote_path=remote,
                                local_path=local_path,
                                progress_callback=stream_callback,
                                timeout=300
                            )
                            result_holder["result"] = result
                        except Exception as e:
                            result_holder["error"] = str(e)
                        finally:
                            result_holder["done"] = True
                    
                    # Run download in background thread
                    download_thread = threading.Thread(target=download_task, daemon=True)
                    download_thread.start()
                    
                    # Update progress display
                    while not result_holder["done"]:
                        current = progress_state.get("current", 0)
                        total = progress_state.get("total", 1) or 1
                        file = progress_state.get("file", "")
                        status = progress_state.get("status", "")
                        
                        if status == "starting":
                            message = "Scanning directory..."
                        elif status == "downloading" and file:
                            message = f"Downloading {file}..."
                        else:
                            message = ""
                        
                        live.update(OutputHelper.create_progress_panel(current, total, title=f"Downloading {basename}", message=message))
                        time.sleep(0.05)
                    
                    # Final update
                    live.update(OutputHelper.create_progress_panel(
                        progress_state.get("current", 0), 
                        progress_state.get("total", 0) or 1, 
                        title=f"Downloading {basename}"
                    ))
                    
                    if result_holder["error"]:
                        raise Exception(result_holder["error"])
            else:
                # Single file - simple download with progress
                file_count = 1
                with Live(OutputHelper.create_progress_panel(0, file_count, title=f"Downloading {basename}", message="Downloading file..."), console=OutputHelper._console, refresh_per_second=10) as live:
                    result = client.send_command('get_to_local', remote_path=remote, local_path=local_path, timeout=60)
                    live.update(OutputHelper.create_progress_panel(1, 1, title=f"Downloading {basename}"))
            
            OutputHelper.print_panel(
                f"Downloaded [bright_blue]{display_remote}[/bright_blue]\nto [green]{local_path}[/green]",
                title="Download Complete",
                border_style="green"
            )
        except Exception as e:
            OutputHelper.print_panel(
                f"Download failed: [red]{str(e)}[/red]",
                title="Download Failed",
                border_style="red"
            )
            raise typer.Exit(1)
    else:
        # Multiple files - ensure destination is a directory
        if not os.path.exists(local):
            os.makedirs(local)
        elif not os.path.isdir(local):
            OutputHelper.print_panel(
                f"Destination [red]{local}[/red] must be a directory when downloading multiple files.",
                title="Download Failed",
                border_style="red"
            )
            raise typer.Exit(1)
        
        with Live(OutputHelper.create_progress_panel(0, total_files, title=f"Downloading {total_files} item(s)", message="Starting..."), console=OutputHelper._console, refresh_per_second=10) as live:
            for idx, (remote, basename, is_dir) in enumerate(files_to_download):
                display_remote = remote.replace(device_root_fs, "", 1)
                live.update(OutputHelper.create_progress_panel(idx, total_files, title=f"Downloading {total_files} item(s)", message=f"Downloading {display_remote}..."))
                
                local_path = os.path.join(local, basename)
                
                try:
                    if is_dir:
                        # Use streaming for directory download
                        result = client.send_command_streaming(
                            'getdir_to_local', 
                            remote_path=remote, 
                            local_path=local_path, 
                            timeout=300
                        )
                    else:
                        result = client.send_command('get_to_local', remote_path=remote, local_path=local_path, timeout=60)
                    success_count += 1
                except Exception as e:
                    OutputHelper._console.print(f"[red]Failed to download {display_remote}: {str(e)}[/red]")
            
            live.update(OutputHelper.create_progress_panel(total_files, total_files, title=f"Downloading {total_files} item(s)"))
        
        OutputHelper.print_panel(
            f"Downloaded [green]{success_count}[/green] out of {total_files} file(s)\nto [green]{local}[/green]",
            title="Download Complete",
            border_style="green" if success_count == total_files else "yellow"
        )




@app.command(name="cat", rich_help_panel="File Operations")
def cat(
    remote: str = typer.Argument("", help="Remote file path"),
    number: bool = typer.Option(False, "-n", "--number", help="Show line numbers"),
    lines: Optional[str] = typer.Option(None, "-L", "--lines", help="Range: lines (text) or bytes (binary)"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Display the content of a file from the connected device.
    Text files are displayed as-is, binary files are shown in hex format.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Display the contents of a file on the connected device.

Text files show as-is; binary files show in hex format.

[bold cyan]Usage:[/bold cyan]
  replx cat [yellow]FILE[/yellow]
  replx cat -n [yellow]FILE[/yellow]        [dim]# With line numbers[/dim]
  replx cat -L N:M [yellow]FILE[/yellow]   [dim]# Show lines N to M[/dim]

[bold cyan]Options:[/bold cyan]
  [yellow]-n, --number[/yellow]          Show line numbers (text only)
  [yellow]-L, --lines N:M[/yellow]       Line range (text) or byte range (binary)

[bold cyan]Arguments:[/bold cyan]
  [yellow]FILE[/yellow]      Remote file path [red][required][/red]

[bold cyan]Examples:[/bold cyan]
  [dim]# Text files[/dim]
  replx cat main.py                   [dim]# Show entire file[/dim]
  replx cat -n /lib/audio.py          [dim]# With line numbers[/dim]
  replx cat -L 1:20 main.py           [dim]# Lines 1-20[/dim]
  replx cat -L 50: main.py            [dim]# From line 50 to end[/dim]
  replx cat -L :10 boot.py            [dim]# First 10 lines[/dim]

  [dim]# Binary files (.mpy, etc.)[/dim]
  replx cat audio.mpy                 [dim]# Show hex dump[/dim]
  replx cat -L 0:256 file.mpy         [dim]# First 256 bytes[/dim]
  replx cat -L 100:+64 file.mpy       [dim]# 64 bytes from offset 100[/dim]

[bold cyan]Related:[/bold cyan]
  replx get file ./       [dim]# Download to local machine[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    if not remote:
        typer.echo("Error: Missing required argument 'REMOTE'.", err=True)
        raise typer.Exit(1)
    
    _ensure_connected()  # Ensure connection is established
    
    # Normalize path
    if not remote.startswith('/'):
        remote = '/' + remote
    
    display_remote = remote.replace('/', '', 1) if remote.startswith('/') else remote
    
    # Use agent to get file content
    client = _create_agent_client()
    
    try:
        result = client.send_command('cat', path=remote)
        content = result.get('content', '')
        is_binary = result.get('is_binary', False)
    except Exception as e:
        error_msg = str(e)
        if OutputHelper.handle_error(e, f"Read: {display_remote}"):
            raise typer.Exit(1)
        elif 'is a directory' in error_msg.lower():
            OutputHelper.print_panel(
                f"[red]{display_remote}[/red] is a directory, not a file.",
                title="Read Failed",
                border_style="red"
            )
        else:
            OutputHelper.print_panel(
                f"[red]{display_remote}[/red] does not exist.",
                title="Read Failed",
                border_style="red"
            )
        raise typer.Exit(1)
    
    if is_binary:
        # Binary file - content is hex string from server, convert back to bytes
        raw_bytes = bytes.fromhex(content)
        total_bytes = len(raw_bytes)
        start_byte = 0
        end_byte = total_bytes
        
        # Parse byte range for binary files
        if lines:
            try:
                if ':' not in lines:
                    raise ValueError("Invalid format")
                parts = lines.split(':')
                if len(parts) != 2:
                    raise ValueError("Invalid format")
                
                # Parse start byte
                if parts[0]:
                    start_byte = int(parts[0])
                    if start_byte < 0:
                        start_byte = 0
                
                # Parse end byte
                if parts[1]:
                    if parts[1].startswith('+'):
                        # Relative: N:+M means M bytes from N
                        count = int(parts[1][1:])
                        end_byte = start_byte + count
                    else:
                        # Absolute: N:M
                        end_byte = int(parts[1])
                
                # Clamp to valid range
                start_byte = max(0, min(start_byte, total_bytes))
                end_byte = max(start_byte, min(end_byte, total_bytes))
            except (ValueError, IndexError):
                OutputHelper.print_panel(
                    f"Invalid byte range format: [red]{lines}[/red]\nFor binary files, use N:M (byte range)",
                    title="Invalid Option",
                    border_style="red"
                )
                raise typer.Exit(1)
        
        # Extract byte range
        data = raw_bytes[start_byte:end_byte]
        
        # Format hex dump: 16 bytes per line with ASCII representation
        # Align to 16-byte boundaries and show -- for bytes outside the range
        from rich.text import Text
        hex_output = Text()
        
        # Calculate the first and last line boundaries (aligned to 16 bytes)
        first_line_start = (start_byte // 16) * 16
        last_line_end = ((end_byte + 15) // 16) * 16
        
        for line_offset in range(first_line_start, last_line_end, 16):
            # Add offset
            hex_output.append(f"{line_offset:08x}", style="cyan")
            hex_output.append("  ")
            
            # Hex part
            hex_chars = []
            ascii_chars = []
            
            for byte_offset in range(line_offset, line_offset + 16):
                if start_byte <= byte_offset < end_byte:
                    # Byte is within range - show actual data
                    data_index = byte_offset - start_byte
                    b = data[data_index]
                    hex_chars.append((f'{b:02x}', "bright_green"))
                    # For ASCII part: printable chars or dot
                    if 32 <= b < 127:
                        ascii_chars.append((chr(b), None))
                    else:
                        ascii_chars.append((".", "dim"))
                else:
                    # Byte is outside range - show placeholder
                    hex_chars.append(("--", "dim"))
                    ascii_chars.append((" ", None))
            
            # Add hex bytes with spaces
            for i, (h, style) in enumerate(hex_chars):
                if i > 0:
                    hex_output.append(" ")
                hex_output.append(h, style=style)
            
            hex_output.append("   ")
            
            # Add ASCII chars
            for c, style in ascii_chars:
                hex_output.append(c, style=style)
            
            hex_output.append("\n")
        
        range_info = f" (bytes {start_byte}-{end_byte})" if lines else f" ({total_bytes} bytes)"
        title = f"Binary File (Hex): {display_remote}{range_info}"
        
        console = Console(width=CONSOLE_WIDTH)
        console.print(Panel(
            hex_output,
            title=title,
            border_style="blue",
            box=get_panel_box(),
            width=CONSOLE_WIDTH
        ))
        return  # Binary file handled, exit early
    else:
        # Text file - content is string from server
        text_content = content
        content_lines = text_content.split('\n')
        total_lines = len(content_lines)
        
        # Apply line range if specified
        start_line = 1
        end_line = total_lines
        if lines:
            try:
                if ':' in lines:
                    parts = lines.split(':')
                    if parts[0]:
                        start_line = max(1, int(parts[0]))
                    if parts[1]:
                        if parts[1].startswith('+'):
                            end_line = start_line + int(parts[1][1:]) - 1
                        else:
                            end_line = int(parts[1])
                    end_line = min(end_line, total_lines)
            except (ValueError, IndexError):
                pass
        
        display_lines = content_lines[start_line-1:end_line]
        
        # Add line numbers if requested
        if number:
            width = len(str(start_line + len(display_lines) - 1))
            formatted = []
            for idx, line in enumerate(display_lines):
                line_num = start_line + idx
                formatted.append(f"{line_num:>{width}}: {line}")
            display_content = '\n'.join(formatted)
        else:
            display_content = '\n'.join(display_lines)
        
        range_info = f" (lines {start_line}-{end_line})" if lines else f" ({total_lines} lines)"
        title = f"File Content: {display_remote}{range_info}"
    
    syntax_extensions = {
        '.py': 'python',
        '.json': 'json',
        '.yaml': 'yaml',
        '.yml': 'yaml',
        '.md': 'markdown',
        '.toml': 'toml',
        '.ini': 'ini',
        '.cfg': 'ini',
    }
    
    # Get file extension
    _, ext = os.path.splitext(remote.lower())
    language = syntax_extensions.get(ext)
    
    # Use syntax highlighting for supported file types (text files only)
    if language and not is_binary:
        from rich.syntax import Syntax
        console = Console(width=CONSOLE_WIDTH)
        
        # Create syntax-highlighted content
        syntax = Syntax(
            display_content if not number else '\n'.join(display_lines),
            language,
            theme="dracula", #monokai, dracula, one-dark
            line_numbers=number,
            start_line=start_line if number else 1,
            word_wrap=False
        )
        
        # Print in a panel
        console.print(Panel(
            syntax,
            title=title,
            border_style="blue",
            box=get_panel_box(),
            width=CONSOLE_WIDTH
        ))
    else:
        # Plain text or binary - use existing panel
        OutputHelper.print_panel(
            display_content,
            title=title,
            border_style="blue"
        )




@app.command(rich_help_panel="File Operations")
def mkdir(
    remotes: Optional[list[str]] = typer.Argument(None, help="Directories to create"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Create one or more directories on the connected device.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Create directories on the connected device.

[bold cyan]Usage:[/bold cyan]
  replx mkdir [yellow]DIR[/yellow]
  replx mkdir [yellow]DIR...[/yellow]        [dim]# Multiple directories[/dim]

[bold cyan]Arguments:[/bold cyan]
  [yellow]DIR[/yellow]       Directory path(s) to create [red][required][/red]

[bold cyan]Examples:[/bold cyan]
  replx mkdir /lib                    [dim]# Create /lib[/dim]
  replx mkdir /tests                  [dim]# Create /tests[/dim]
  replx mkdir /lib/audio /lib/net     [dim]# Create multiple[/dim]
  replx mkdir /a/b/c                  [dim]# Creates /a/b/c (nested)[/dim]

[bold cyan]Note:[/bold cyan]
  • Creates parent directories automatically if needed
  • No error if directory already exists

[bold cyan]Related:[/bold cyan]
  replx rm -r /dir          [dim]# Remove directory[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    _ensure_connected()
    
    if not remotes:
        typer.echo("Error: Missing required arguments.", err=True)
        raise typer.Exit(1)
    
    success_count = 0
    already_exist = []
    
    with _create_agent_client() as client:
        for remote in remotes:
            # Normalize path
            if not remote.startswith('/'):
                remote = '/' + remote
            
            try:
                client.send_command('mkdir', path=remote)
                success_count += 1
                if len(remotes) == 1:  # Only show panel for single directory
                    OutputHelper.print_panel(
                        f"Directory [bright_blue]{remote}[/bright_blue] created successfully.",
                        title="Create Directory",
                        border_style="green"
                    )
            except Exception as e:
                error_msg = str(e)
                if 'EEXIST' in error_msg or 'exists' in error_msg.lower():
                    already_exist.append(remote)
                    if len(remotes) == 1:
                        OutputHelper.print_panel(
                            f"Directory [bright_blue]{remote}[/bright_blue] already exists.",
                            title="Create Directory",
                            border_style="yellow"
                        )
                else:
                    already_exist.append(remote)
                    if len(remotes) == 1:
                        OutputHelper.print_panel(
                            f"Failed to create [bright_blue]{remote}[/bright_blue]: {error_msg}",
                            title="Create Directory",
                            border_style="red"
                        )
    
    # Summary for multiple directories
    if len(remotes) > 1:
        if already_exist:
            OutputHelper.print_panel(
                f"Created [green]{success_count}[/green] director{'y' if success_count == 1 else 'ies'}.\nAlready exist: {', '.join(already_exist)}",
                title="Create Directories",
                border_style="yellow" if success_count > 0 else "green"
            )
        else:
            OutputHelper.print_panel(
                f"Created [green]{success_count}[/green] director{'y' if success_count == 1 else 'ies'} successfully.",
                title="Create Directories",
                border_style="green"
            )



@app.command(rich_help_panel="File Operations")
def rm(
    args: Optional[list[str]] = typer.Argument(None, help="Files or directories to remove"),
    recursive: bool = typer.Option(False, "-r", "--recursive", help="Remove directories recursively"),
    force: bool = typer.Option(False, "-f", "--force", help="Force removal without confirmation"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Remove files or directories from the connected device.
    Use -r option to remove directories recursively.
    Use -f option to skip confirmation prompt.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Delete files or directories from the connected device.

[bold cyan]Usage:[/bold cyan]
  replx rm [yellow]FILE[/yellow]
  replx rm -r [yellow]DIR[/yellow]          [dim]# Remove directory[/dim]
  replx rm -f [yellow]FILE[/yellow]         [dim]# Skip confirmation[/dim]

[bold cyan]Options:[/bold cyan]
  [yellow]-r, --recursive[/yellow]       Remove directories and contents
  [yellow]-f, --force[/yellow]           Don't ask for confirmation

[bold cyan]Arguments:[/bold cyan]
  [yellow]FILE/DIR[/yellow]    File(s) or directory to remove [red][required][/red]

[bold cyan]Examples:[/bold cyan]
  [dim]# Single file[/dim]
  replx rm /main.py                   [dim]# Delete file (asks confirm)[/dim]
  replx rm -f /main.py                [dim]# Delete without asking[/dim]

  [dim]# Multiple files[/dim]
  replx rm /a.py /b.py /c.py          [dim]# Delete multiple files[/dim]

  [dim]# Wildcards[/dim]
  replx rm /*.pyc                     [dim]# Delete all .pyc files[/dim]
  replx rm /lib/*.mpy                 [dim]# Delete .mpy in /lib[/dim]
  replx rm -f test*.py                [dim]# Delete test*.py silently[/dim]

  [dim]# Directories[/dim]
  replx rm -r /backup                 [dim]# Remove directory[/dim]
  replx rm -rf /temp                  [dim]# Remove without confirmation[/dim]

[bold yellow]Warning:[/bold yellow]
  Deleted files cannot be recovered!

[bold cyan]Related:[/bold cyan]
  replx get file ./         [dim]# Backup before deleting[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    status = _ensure_connected()
    device_root_fs = status.get('device_root_fs', '/')
    
    # Get raw arguments from sys.argv to avoid Typer's shell expansion
    # Find 'rm' command in sys.argv
    cmd_idx = next((i for i, arg in enumerate(sys.argv) if arg == 'rm'), None)
    if cmd_idx is not None and cmd_idx + 1 < len(sys.argv):
        # Use raw arguments after 'rm' command, skipping options
        raw_args = []
        for arg in sys.argv[cmd_idx + 1:]:
            if arg.startswith('-'):
                # Check for combined options like -rf, -r, -f
                if 'f' in arg:
                    force = True
                if 'r' in arg:
                    recursive = True
                continue
            raw_args.append(arg)
        if raw_args:
            args = raw_args
    
    if not args:
        typer.echo("Error: Missing required arguments.", err=True)
        raise typer.Exit(1)
    
    client = _create_agent_client()
    
    # Helper function to normalize path
    def normalize_path(path: str) -> str:
        if not path.startswith('/'):
            path = '/' + path
        return path
    
    # Resolve wildcards to actual file list for confirmation
    def resolve_patterns(patterns: list[str]) -> list[str]:
        """Resolve wildcard patterns to actual file paths."""
        resolved = []
        for pattern in patterns:
            remote = normalize_path(pattern)
            if '*' in pattern or '?' in pattern:
                # Extract directory and pattern
                dir_path = posixpath.dirname(remote) or '/'
                basename_pattern = posixpath.basename(remote)
                try:
                    result = client.send_command('ls', path=dir_path, detailed=True)
                    items = result.get('items', [])
                    for item in items:
                        if fnmatch.fnmatch(item['name'], basename_pattern):
                            resolved.append(posixpath.join(dir_path, item['name']))
                except RuntimeError as e:
                    if OutputHelper.handle_error(e, "rm"):
                        raise typer.Exit(1)
                    resolved.append(pattern)
                except Exception:
                    resolved.append(pattern)
            else:
                resolved.append(remote)
        return resolved
    
    def check_exists(path: str) -> bool:
        """Check if a file or directory exists on the device."""
        try:
            result = client.send_command('is_dir', path=path)
            return result.get('exists', True)
        except Exception:
            return True
    
    if not force:
        has_wildcard = any('*' in p or '?' in p for p in args)
        
        if has_wildcard:
            resolved_files = resolve_patterns(args)
            if not resolved_files:
                typer.echo("No matching files found.")
                raise typer.Exit(0)
            
            MAX_SHOW = 5
            if len(resolved_files) <= MAX_SHOW:
                files_display = '\n  '.join(resolved_files)
                confirm_msg = f"Remove {len(resolved_files)} item(s)?\n  {files_display}\n[y/N]"
            else:
                shown = '\n  '.join(resolved_files[:MAX_SHOW])
                remaining = len(resolved_files) - MAX_SHOW
                confirm_msg = f"Remove {len(resolved_files)} item(s)?\n  {shown}\n  ... and {remaining} more\n[y/N]"
        else:
            existing_files = []
            not_found_files = []
            for pattern in args:
                remote = normalize_path(pattern)
                if check_exists(remote):
                    existing_files.append(pattern)
                else:
                    not_found_files.append(pattern)
            
            if not_found_files:
                if len(not_found_files) == 1:
                    OutputHelper.print_panel(
                        f"rm: [red]{not_found_files[0]}[/red] does not exist.",
                        title="Remove Failed",
                        border_style="red"
                    )
                else:
                    files_str = ', '.join(not_found_files)
                    OutputHelper.print_panel(
                        f"rm: Files not found: [red]{files_str}[/red]",
                        title="Remove Failed",
                        border_style="red"
                    )
                raise typer.Exit(1)
            
            files_str = ', '.join(args)
            confirm_msg = f"Remove {files_str}? [y/N]"
        
        confirm = typer.prompt(confirm_msg, default="n", show_default=False)
        if confirm.lower() != 'y':
            typer.echo("Cancelled.")
            raise typer.Exit(0)
    
    success_count = 0
    failed_items = []
    dir_without_r = []  # Track directories attempted without -r
    
    for pattern in args:
        remote = normalize_path(pattern)
        
        # Check for wildcards
        if '*' in pattern or '?' in pattern:
            # Extract directory and pattern
            dir_path = posixpath.dirname(remote) or '/'
            basename_pattern = posixpath.basename(remote)
            
            try:
                result = client.send_command('ls', path=dir_path, detailed=True)
                items = [(item['name'], item['size'], item['is_dir']) for item in result.get('items', [])]
                matched = False
                
                for name, size, is_dir in items:
                    if fnmatch.fnmatch(name, basename_pattern):
                        matched = True
                        full_path = posixpath.join(dir_path, name)
                        try:
                            if is_dir:
                                if not recursive:
                                    dir_without_r.append(name)
                                    continue
                                client.send_command('rmdir', path=full_path)
                            else:
                                client.send_command('rm', path=full_path)
                            success_count += 1
                        except RuntimeError as e:
                            if OutputHelper.handle_error(e, "rm"):
                                raise typer.Exit(1)
                            failed_items.append(name)
                        except Exception:
                            failed_items.append(name)
                
                if not matched:
                    failed_items.append(pattern)
            except RuntimeError as e:
                if OutputHelper.handle_error(e, "rm"):
                    raise typer.Exit(1)
                failed_items.append(pattern)
            except Exception:
                failed_items.append(pattern)
        else:
            # Single file/directory
            try:
                is_dir_result = client.send_command('is_dir', path=remote)
                is_dir = is_dir_result.get('is_dir', False)
                
                if is_dir:
                    if not recursive:
                        display_remote = remote.replace(device_root_fs, "", 1)
                        dir_without_r.append(display_remote)
                        continue
                    client.send_command('rmdir', path=remote)
                    item_type = "Directory"
                else:
                    client.send_command('rm', path=remote)
                    item_type = "File"
                success_count += 1
                
                display_path = remote.replace(device_root_fs, "", 1)
                if len(args) == 1:  # Only show panel for single item
                    OutputHelper.print_panel(
                        f"{item_type} [bright_blue]{display_path}[/bright_blue] removed successfully.",
                        title="Remove",
                        border_style="green"
                    )
            except RuntimeError as e:
                if OutputHelper.handle_error(e, "rm"):
                    raise typer.Exit(1)
                failed_items.append(pattern)
            except Exception:
                failed_items.append(pattern)
    
    # Show error for directories attempted without -r
    if dir_without_r:
        if len(dir_without_r) == 1:
            OutputHelper.print_panel(
                f"rm: [red]{dir_without_r[0]}[/red] is a directory.\n\n"
                "Use [cyan]-r[/cyan] option to remove directories recursively:\n"
                f"  replx rm -r {dir_without_r[0]}",
                title="Remove Failed",
                border_style="red"
            )
        else:
            dirs_str = "\n".join(f"  - {d}" for d in dir_without_r)
            OutputHelper.print_panel(
                f"rm: cannot remove directories (use [cyan]-r[/cyan] option):\n{dirs_str}\n\n"
                "Example: replx rm -r <directory>",
                title="Remove Failed",
                border_style="red"
            )
        if not success_count and not failed_items:
            raise typer.Exit(1)
    
    # Summary for multiple items
    if len(args) > 1 or success_count > 1:
        if failed_items:
            OutputHelper.print_panel(
                f"Removed [green]{success_count}[/green] item(s).\nFailed: {', '.join(failed_items)}",
                title="Remove Complete",
                border_style="yellow"
            )
        else:
            OutputHelper.print_panel(
                f"Removed [green]{success_count}[/green] item(s) successfully.",
                title="Remove Complete",
                border_style="green"
            )
    elif failed_items and not dir_without_r:
        remote_display = args[0].replace(device_root_fs, "", 1) if args[0].startswith(device_root_fs) else args[0]
        OutputHelper.print_panel(
            f"rm: [red]{remote_display}[/red] does not exist.",
            title="Remove Failed",
            border_style="red"
        )
        raise typer.Exit(1)
    elif failed_items:
        remote_display = args[0].replace(device_root_fs, "", 1) if args[0].startswith(device_root_fs) else args[0]
        OutputHelper.print_panel(
            f"[red]{remote_display}[/red] does not exist.",
            title="Remove",
            border_style="red"
        )



@app.command(rich_help_panel="File Operations")
def cp(
    args: Optional[list[str]] = typer.Argument(None, help="Source file(s) and destination"),
    recursive: bool = typer.Option(False, "-r", "--recursive", help="Copy directories recursively"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Copy file(s) or directory on the connected device.
    Last argument is the destination. Supports wildcards for source files.
    Use -r to copy directories.
    """
    # Check for custom help
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Copy files or directories on the connected device.

[bold cyan]Usage:[/bold cyan]
  replx cp [yellow]SRC[/yellow] [yellow]DEST[/yellow]
  replx cp -r [yellow]DIR[/yellow] [yellow]DEST[/yellow]      [dim]# Copy directory[/dim]
  replx cp [yellow]SRC...[/yellow] [yellow]DEST[/yellow]      [dim]# Multiple files[/dim]

[bold cyan]Options:[/bold cyan]
  [yellow]-r, --recursive[/yellow]       Copy directories and contents

[bold cyan]Arguments:[/bold cyan]
  [yellow]SRC[/yellow]       Source file(s) or directory [red][required][/red]
  [yellow]DEST[/yellow]      Destination path [red][required][/red]

[bold cyan]Examples:[/bold cyan]
  [dim]# Single file[/dim]
  replx cp /main.py /backup.py        [dim]# Copy with new name[/dim]
  replx cp /lib/a.py /backup          [dim]# Copy to directory[/dim]

  [dim]# Multiple files[/dim]
  replx cp x.py y.py z.py /backup     [dim]# Copy multiple[/dim]

  [dim]# Wildcards[/dim]
  replx cp *.py /backup               [dim]# Copy all .py files[/dim]
  replx cp /lib/*.mpy /backup         [dim]# Copy all .mpy files[/dim]

  [dim]# Directories[/dim]
  replx cp -r /lib /lib_backup        [dim]# Copy entire directory[/dim]
  replx cp -r a.py dir1 /backup       [dim]# Mix files and directories[/dim]

[bold cyan]Note:[/bold cyan]
  • Last argument is always the destination
  • Use -r for directories (required)
  • Destination directory is created if needed

[bold cyan]Related:[/bold cyan]
  replx mv src dest       [dim]# Move instead of copy[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    _ensure_connected()
    
    if not args or len(args) < 2:
        OutputHelper.print_panel(
            "Missing required arguments.\n\n"
            "[bold cyan]Usage:[/bold cyan] replx cp [yellow]SOURCE... DEST[/yellow]\n\n"
            "At least 2 arguments required: source file(s) and destination.",
            title="Copy Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    # Get raw arguments from sys.argv (skip options like -r, -h)
    cmd_idx = next((i for i, arg in enumerate(sys.argv) if arg == 'cp'), None)
    if cmd_idx is not None and cmd_idx + 1 < len(sys.argv):
        raw_args = []
        for arg in sys.argv[cmd_idx + 1:]:
            # Skip options (but not file paths starting with -)
            if arg in ('-r', '--recursive', '-h', '--help'):
                continue
            raw_args.append(arg)
        if len(raw_args) >= 2:
            args = raw_args
    
    # Last argument is destination
    dest = args[-1]
    sources = args[:-1]
    
    # Use agent for cp operation
    client = _create_agent_client()
    
    # For single source without wildcards, use agent directly
    if len(sources) == 1 and '*' not in sources[0] and '?' not in sources[0]:
        source = sources[0]
        try:
            resp = client.send_command('cp', source=source, dest=dest, recursive=recursive)
            
            success_count = resp.get('files_count', 1) if resp.get('copied') else 0
            total_files = resp.get('files_count', 1) if 'files_count' in resp else 1
            is_empty_dir = resp.get('is_empty_dir', False)
            display_source = resp.get('source', source)
            display_dest = resp.get('dest', dest)
            
            if resp.get('copied'):
                if is_empty_dir:
                    OutputHelper.print_panel(
                        f"Copied empty directory [bright_blue]{display_source}[/bright_blue]\nto [green]{display_dest}[/green]",
                        title="Copy Complete",
                        border_style="green"
                    )
                elif total_files == 1:
                    OutputHelper.print_panel(
                        f"Copied [bright_blue]{display_source}[/bright_blue]\nto [green]{display_dest}[/green]",
                        title="Copy Complete",
                        border_style="green"
                    )
                else:
                    OutputHelper.print_panel(
                        f"Copied [green]{success_count}[/green] file(s)\nfrom [bright_blue]{display_source}[/bright_blue]\nto [green]{display_dest}[/green]",
                        title="Copy Complete",
                        border_style="green"
                    )
            else:
                OutputHelper.print_panel(
                    "Source directory is empty or no files to copy.",
                    title="Copy",
                    border_style="yellow"
                )
        except RuntimeError as e:
            error = str(e)
            
            if 'not found' in error.lower() or 'does not exist' in error.lower():
                OutputHelper.print_panel(
                    f"[red]{source}[/red] does not exist.",
                    title="Copy Failed",
                    border_style="red"
                )
            elif 'is a directory' in error.lower() or 'EISDIR' in error:
                OutputHelper.print_panel(
                    f"cp: [red]{source}[/red] is a directory (not copied).\n\n"
                    "Use [cyan]-r[/cyan] option to copy directories recursively:\n"
                    f"  replx cp -r {source} <destination>",
                    title="Copy Failed",
                    border_style="red"
                )
            else:
                OutputHelper.print_panel(
                    f"Copy failed: [red]{error}[/red]",
                    title="Copy Failed",
                    border_style="red"
                )
            raise typer.Exit(1)
        return
    
    # For multiple sources or wildcards, expand on client side using agent calls
    files_to_copy = []
    
    for source_pattern in sources:
        # Check for wildcards
        if '*' in source_pattern or '?' in source_pattern:
            # Extract directory and pattern
            dir_path = posixpath.dirname(source_pattern) or '/'
            pattern = posixpath.basename(source_pattern)
            
            try:
                result = client.send_command('ls', path=dir_path, detailed=True)
                items = result.get('items', []) if isinstance(result, dict) else []
                
                matched = False
                for item in items:
                    if isinstance(item, dict):
                        name = item.get('name', '')
                        is_dir = item.get('is_dir', False)
                    else:
                        name = str(item)
                        is_dir = False
                    if fnmatch.fnmatch(name, pattern):
                        full_path = posixpath.join(dir_path, name)
                        files_to_copy.append((full_path, name, is_dir))
                        matched = True
                if not matched:
                    OutputHelper.print_panel(
                        f"No files matching: [red]{source_pattern}[/red]",
                        title="Copy Failed",
                        border_style="red"
                    )
                    raise typer.Exit(1)
            except typer.Exit:
                raise
            except Exception as e:
                OutputHelper.print_panel(
                    f"Error processing pattern '{source_pattern}': {e}",
                    title="Copy Failed",
                    border_style="red"
                )
                raise typer.Exit(1)
        else:
            # Check if source exists and get info
            try:
                result = client.send_command('is_dir', path=source_pattern)
                is_dir = result if isinstance(result, bool) else result.get('is_dir', False)
            except Exception:
                OutputHelper.print_panel(
                    f"cp: [red]{source_pattern}[/red] does not exist.",
                    title="Copy Failed",
                    border_style="red"
                )
                raise typer.Exit(1)
            
            basename = posixpath.basename(source_pattern)
            
            # Check if trying to copy directory without -r
            if is_dir and not recursive:
                OutputHelper.print_panel(
                    f"cp: [red]{source_pattern}[/red] is a directory.\n"
                    f"Use [yellow]-r[/yellow] option to copy directories.",
                    title="Copy Failed",
                    border_style="red"
                )
                raise typer.Exit(1)
            files_to_copy.append((source_pattern, basename, is_dir))
    
    if not files_to_copy:
        OutputHelper.print_panel(
            "No files to copy.",
            title="Copy",
            border_style="yellow"
        )
        raise typer.Exit(1)
    
    # Check if dest is a directory (needed for multiple files)
    try:
        result = client.send_command('is_dir', path=dest)
        dest_is_dir = result if isinstance(result, bool) else result.get('is_dir', False)
    except Exception:
        dest_is_dir = False
    
    # If copying multiple files, dest must be a directory
    if len(files_to_copy) > 1 and not dest_is_dir:
        OutputHelper.print_panel(
            f"When copying multiple files, destination must be a directory.\n"
            f"Destination [red]{dest}[/red] is not a directory.",
            title="Copy Failed",
            border_style="red"
        )
        raise typer.Exit(1)
    
    # Copy each file/directory
    success_count = 0
    for source_path, basename, is_dir in files_to_copy:
        if dest_is_dir:
            target = posixpath.join(dest, basename)
        else:
            target = dest
        
        try:
            client.send_command('cp', source=source_path, dest=target, recursive=recursive)
            success_count += 1
        except Exception as e:
            OutputHelper.print_panel(
                f"Failed to copy [red]{source_path}[/red]: {e}",
                title="Copy Failed",
                border_style="red"
            )
            if success_count > 0:
                OutputHelper.print_panel(
                    f"Copied {success_count} of {len(files_to_copy)} file(s) before error.",
                    title="Partial Copy",
                    border_style="yellow"
                )
            raise typer.Exit(1)
    
    # Success message
    if success_count == 1:
        source_path = files_to_copy[0][0]
        OutputHelper.print_panel(
            f"Copied [bright_blue]{source_path}[/bright_blue]\nto [green]{dest}[/green]",
            title="Copy Complete",
            border_style="green"
        )
    else:
        OutputHelper.print_panel(
            f"Copied [green]{success_count}[/green] file(s) to [green]{dest}[/green]",
            title="Copy Complete",
            border_style="green"
        )



@app.command(rich_help_panel="File Operations")
def mv(
    args: Optional[list[str]] = typer.Argument(None, help="Source file(s) and destination"),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Move directories recursively"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Move/rename file(s) or directory on the connected device.
    Last argument is the destination. Supports wildcards for source files.
    Use -r to move directories.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Move or rename files and directories on the connected device.

[bold cyan]Usage:[/bold cyan]
  replx mv [yellow]SRC[/yellow] [yellow]DEST[/yellow]           [dim]# Rename or move file[/dim]
  replx mv -r [yellow]DIR[/yellow] [yellow]DEST[/yellow]        [dim]# Move directory[/dim]
  replx mv [yellow]SRC...[/yellow] [yellow]DEST[/yellow]        [dim]# Multiple files[/dim]

[bold cyan]Options:[/bold cyan]
  [yellow]-r, --recursive[/yellow]       Move directories and contents

[bold cyan]Arguments:[/bold cyan]
  [yellow]SRC[/yellow]       Source file(s) or directory [red][required][/red]
  [yellow]DEST[/yellow]      Destination path [red][required][/red]

[bold cyan]Examples:[/bold cyan]
  [dim]# Rename[/dim]
  replx mv /old.py /new.py            [dim]# Rename file[/dim]
  replx mv /tests /test               [dim]# Rename directory[/dim]

  [dim]# Move to directory[/dim]
  replx mv /main.py /backup           [dim]# Move file into /backup[/dim]
  replx mv x.py y.py z.py /backup     [dim]# Move multiple files[/dim]

  [dim]# Wildcards[/dim]
  replx mv *.py /backup               [dim]# Move all .py files[/dim]
  replx mv /lib/*.mpy /compiled       [dim]# Move all .mpy files[/dim]

  [dim]# Directories[/dim]
  replx mv -r /lib/audio /lib/sound   [dim]# Move directory[/dim]
  replx mv -r a.py dir1 /backup       [dim]# Mix files and dirs[/dim]

[bold cyan]Note:[/bold cyan]
  • Last argument is the destination
  • Use -r for directories (required)
  • Unlike copy, the source is removed after move

[bold cyan]Related:[/bold cyan]
  replx cp src dest       [dim]# Copy instead of move[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    _ensure_connected()
    
    if not args or len(args) < 2:
        OutputHelper.print_panel(
            "Missing required arguments.\n\n"
            "[bold cyan]Usage:[/bold cyan] replx mv [yellow]SOURCE... DEST[/yellow]\n\n"
            "At least 2 arguments required: source file(s) and destination.",
            title="Move Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    # Get raw arguments from sys.argv (skip options like -r, -h)
    cmd_idx = next((i for i, arg in enumerate(sys.argv) if arg == 'mv'), None)
    if cmd_idx is not None and cmd_idx + 1 < len(sys.argv):
        raw_args = []
        for arg in sys.argv[cmd_idx + 1:]:
            # Skip options (but not file paths starting with -)
            if arg in ('-r', '--recursive', '-h', '--help'):
                continue
            raw_args.append(arg)
        if len(raw_args) >= 2:
            args = raw_args
    
    # Last argument is destination
    dest = args[-1]
    sources = args[:-1]
    
    # Use agent for mv operation
    client = _create_agent_client()
    
    # For single source without wildcards, use agent directly
    if len(sources) == 1 and '*' not in sources[0] and '?' not in sources[0]:
        source = sources[0]
        try:
            result = client.send_command('mv', source=source, dest=dest)
            display_source = result.get('source', source)
            display_dest = result.get('dest', dest)
            
            OutputHelper.print_panel(
                f"Moved [bright_blue]{display_source}[/bright_blue]\nto [green]{display_dest}[/green]",
                title="Move Complete",
                border_style="green"
            )
        except Exception as e:
            error = str(e)
            
            if 'ENOENT' in error or 'not found' in error.lower() or 'does not exist' in error.lower():
                OutputHelper.print_panel(
                    f"mv: [red]{source}[/red] does not exist.",
                    title="Move Failed",
                    border_style="red"
                )
            elif 'is a directory' in error.lower():
                OutputHelper.print_panel(
                    f"mv: [red]{source}[/red] is a directory.\n"
                    f"Use [yellow]-r[/yellow] option to move directories.",
                    title="Move Failed",
                    border_style="red"
                )
            else:
                OutputHelper.print_panel(
                    f"Move failed: {error}",
                    title="Move Failed",
                    border_style="red"
                )
            raise typer.Exit(1)
        return
    
    # For multiple sources or wildcards, expand on client side using agent calls
    files_to_move = []
    
    for source_pattern in sources:
        # Check for wildcards
        if '*' in source_pattern or '?' in source_pattern:
            # Extract directory and pattern
            dir_path = posixpath.dirname(source_pattern) or '/'
            pattern = posixpath.basename(source_pattern)
            
            try:
                result = client.send_command('ls', path=dir_path, detailed=True)
                # result is {"items": [{"name": ..., "size": ..., "is_dir": ...}, ...]}
                items = result.get('items', []) if isinstance(result, dict) else []
                
                matched = False
                for item in items:
                    if isinstance(item, dict):
                        name = item.get('name', '')
                        is_dir = item.get('is_dir', False)
                    else:
                        name = str(item)
                        is_dir = False
                    if fnmatch.fnmatch(name, pattern):
                        full_path = posixpath.join(dir_path, name)
                        files_to_move.append((full_path, name, is_dir))
                        matched = True
                if not matched:
                    OutputHelper.print_panel(
                        f"No files matching: [red]{source_pattern}[/red]",
                        title="Move Failed",
                        border_style="red"
                    )
                    raise typer.Exit(1)
            except typer.Exit:
                raise
            except Exception as e:
                OutputHelper.print_panel(
                    f"Error processing pattern '{source_pattern}': {e}",
                    title="Move Failed",
                    border_style="red"
                )
                raise typer.Exit(1)
        else:
            # Check if source exists
            try:
                result = client.send_command('is_dir', path=source_pattern)
                is_dir = result if isinstance(result, bool) else result.get('is_dir', False)
            except Exception:
                OutputHelper.print_panel(
                    f"mv: [red]{source_pattern}[/red] does not exist.",
                    title="Move Failed",
                    border_style="red"
                )
                raise typer.Exit(1)
            
            basename = posixpath.basename(source_pattern)
            
            # Check if trying to move directory without -r
            if is_dir and not recursive:
                OutputHelper.print_panel(
                    f"mv: [red]{source_pattern}[/red] is a directory.\n"
                    f"Use [yellow]-r[/yellow] option to move directories.",
                    title="Move Failed",
                    border_style="red"
                )
                raise typer.Exit(1)
            files_to_move.append((source_pattern, basename, is_dir))
    
    if not files_to_move:
        OutputHelper.print_panel(
            "No files to move.",
            title="Move",
            border_style="yellow"
        )
        raise typer.Exit(1)
    
    # Check if dest is a directory (needed for multiple files)
    try:
        result = client.send_command('is_dir', path=dest)
        dest_is_dir = result if isinstance(result, bool) else result.get('is_dir', False)
    except Exception:
        dest_is_dir = False
    
    # If moving multiple files, dest must be a directory
    if len(files_to_move) > 1 and not dest_is_dir:
        OutputHelper.print_panel(
            f"When moving multiple files, destination must be a directory.\n"
            f"Destination [red]{dest}[/red] is not a directory.",
            title="Move Failed",
            border_style="red"
        )
        raise typer.Exit(1)
    
    # Move files using agent
    success_count = 0
    failed_items = []
    
    for source, basename, is_dir in files_to_move:
        # Determine final destination
        if dest_is_dir:
            final_dest = posixpath.join(dest, basename)
        else:
            final_dest = dest
        
        try:
            client.send_command('mv', source=source, dest=final_dest)
            success_count += 1
        except Exception as e:
            failed_items.append((source, str(e)))
    
    # Summary
    if len(files_to_move) == 1 and success_count == 1:
        source, basename, is_dir = files_to_move[0]
        display_dest = posixpath.join(dest, basename) if dest_is_dir else dest
        item_type = "Directory" if is_dir else "File"
        OutputHelper.print_panel(
            f"Moved [bright_blue]{source}[/bright_blue]\nto [green]{display_dest}[/green]",
            title=f"Move Complete ({item_type})",
            border_style="green"
        )
    elif success_count > 0:
        if failed_items:
            fail_list = "\n".join([f"  • {src}" for src, _ in failed_items])
            OutputHelper.print_panel(
                f"Moved [green]{success_count}[/green] out of {len(files_to_move)} file(s)\n"
                f"to [green]{dest}[/green]\n\n"
                f"Failed:\n{fail_list}",
                title="Move Partially Complete",
                border_style="yellow"
            )
        else:
            OutputHelper.print_panel(
                f"Moved [green]{success_count}[/green] file(s)\nto [green]{dest}[/green]",
                title="Move Complete",
                border_style="green"
            )
    else:
        fail_list = "\n".join([f"  • {src}: {err}" for src, err in failed_items])
        OutputHelper.print_panel(
            f"Failed to move files:\n{fail_list}",
            title="Move Failed",
            border_style="red"
        )
        raise typer.Exit(1)



@app.command(rich_help_panel="File Operations")
def touch(
    remotes: Optional[list[str]] = typer.Argument(None, help="Files to create"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Create one or more empty files on the connected device.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Create empty files on the connected device.

[bold cyan]Usage:[/bold cyan]
  replx touch [yellow]FILE[/yellow]
  replx touch [yellow]FILE...[/yellow]       [dim]# Multiple files[/dim]

[bold cyan]Arguments:[/bold cyan]
  [yellow]FILE[/yellow]      File path(s) to create [red][required][/red]

[bold cyan]Examples:[/bold cyan]
  replx touch /config.py              [dim]# Create single file[/dim]
  replx touch /a.py /b.py /c.py       [dim]# Create multiple files[/dim]
  replx touch /lib/placeholder.txt    [dim]# Create in subdirectory[/dim]

[bold cyan]Note:[/bold cyan]
  • Creates empty files (0 bytes)
  • Parent directory must exist
  • Does not overwrite existing files

[bold cyan]Use cases:[/bold cyan]
  • Create placeholder files
  • Create empty __init__.py for packages
  • Create config files to edit later

[bold cyan]Related:[/bold cyan]
  replx put file /        [dim]# Upload file with content[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    _ensure_connected()
    
    if not remotes:
        typer.echo("Error: Missing required arguments.", err=True)
        raise typer.Exit(1)
    
    # Use agent for touch operation
    client = _create_agent_client()
    success_count = 0
    failed_items = []
    
    for remote in remotes:
        try:
            result = client.send_command('touch', path=remote)
            success_count += 1
            
            if len(remotes) == 1:  # Only show panel for single file
                display_path = result.get('created', remote)
                OutputHelper.print_panel(
                    f"File [bright_blue]{display_path}[/bright_blue] created successfully.",
                    title="Touch File",
                    border_style="green"
                )
        except Exception as e:
            failed_items.append(remote)
            if len(remotes) == 1:  # Only show panel for single file
                OutputHelper.print_panel(
                    f"Touch failed: {e}",
                    title="Touch Failed",
                    border_style="red"
                )
    
    # Summary for multiple files
    if len(remotes) > 1:
        if failed_items:
            OutputHelper.print_panel(
                f"Created [green]{success_count}[/green] file(s).\nFailed: {', '.join(failed_items)}",
                title="Touch Files",
                border_style="yellow"
            )
        else:
            OutputHelper.print_panel(
                f"Created [green]{success_count}[/green] file(s) successfully.",
                title="Touch Files",
                border_style="green"
            )



@app.command(rich_help_panel="File Operations")
def ls(
    path: str = typer.Argument("/", help="Directory path to list"),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="List subdirectories recursively"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    List the contents of a directory on the connected device.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
List files and directories on the connected device.

[bold cyan]Usage:[/bold cyan]
  replx ls [[yellow]PATH[/yellow]]          [dim]# Default: / (root)[/dim]
  replx ls -r [[yellow]PATH[/yellow]]       [dim]# Recursive listing[/dim]

[bold cyan]Options:[/bold cyan]
  [yellow]-r, --recursive[/yellow]       Show subdirectories as tree

[bold cyan]Arguments:[/bold cyan]
  [yellow]PATH[/yellow]      Directory to list [dim][default: /][/dim]

[bold cyan]Examples:[/bold cyan]
  replx ls                    [dim]# List root directory[/dim]
  replx ls /lib               [dim]# List /lib directory[/dim]
  replx ls -r                 [dim]# Full tree from root[/dim]
  replx ls -r /lib            [dim]# Tree of /lib[/dim]

[bold cyan]Output shows:[/bold cyan]
  • [yellow]󴋋[/yellow]  Folders (yellow icon)
  • [cyan]󰄠[/cyan]  Python files (.py)
  • [orange]󰆧[/orange]  Compiled files (.mpy)
  • File sizes in bytes

[bold cyan]Related:[/bold cyan]
  replx cat file.py     [dim]# View file contents[/dim]
  replx get file ./     [dim]# Download file[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    _ensure_connected()  # Ensure connection is established
    
    # Normalize path
    if not path.startswith('/'):
        path = '/' + path

    try:
        with _create_agent_client() as client:
            result = client.send_command('ls', path=path, detailed=True, recursive=recursive)
        
        items = [(item['name'], item['size'], item['is_dir']) for item in result.get('items', [])]
        
        if not items:
            OutputHelper.print_panel(
                "Directory is empty.",
                title=f"Directory Listing: {path}",
                border_style="dim"
            )
            return
        
        def get_icon(name: str, is_dir: bool) -> str:
            """Get file/folder icon with color"""
            if is_dir:
                return "[#E6B450]󰉋[/#E6B450]"  # folder - gold/yellow
            ext_icons = {
                ".py":   "[#5CB8C2]󰌠[/#5CB8C2]",  # Python - cyan
                ".mpy":  "[#D98C53]󰆧[/#D98C53]",  # compiled - orange
                ".log":  "[#7A7A7A]󰌱[/#7A7A7A]",  # log - gray
                ".ini":  "[#7A7A7A]󰘦[/#7A7A7A]",  # config - gray
            }
            _, ext = os.path.splitext(str(name).lower())
            return ext_icons.get(ext, "[#8C8C8C]󰈙[/#8C8C8C]")  # default - gray

        if recursive:
            # Build tree structure for recursive listing
            from collections import defaultdict
            
            # Build tree dict: path -> list of (basename, size, is_dir, full_path)
            tree = defaultdict(list)
            
            for name, size, is_dir in items:
                # name is full path like /lib/ticle/ext/__init__.mpy
                parent = '/'.join(name.rsplit('/', 1)[:-1]) or '/'
                basename = name.rsplit('/', 1)[-1]
                tree[parent].append((basename, size, is_dir, name))
            
            # Sort items in each directory: folders first, then files, alphabetically
            for parent in tree:
                tree[parent].sort(key=lambda x: (not x[2], x[0].lower()))
            
            # Calculate max size width from original items (3-tuple)
            max_size = max((size for _, size, is_dir in items if not is_dir), default=0)
            size_width = len(str(max_size)) if max_size > 0 else 0
            
            lines = []
            
            def render_tree(dir_path: str, prefix: str = ""):
                """Recursively render tree structure"""
                children = tree.get(dir_path, [])
                for i, (basename, size, is_dir, full_path) in enumerate(children):
                    is_last = (i == len(children) - 1)
                    
                    # Tree branch characters
                    branch = "└── " if is_last else "├── "
                    child_prefix = prefix + ("    " if is_last else "│   ")
                    
                    icon = get_icon(basename, is_dir)
                    
                    if is_dir:
                        name_str = f"[#73B8F1]{basename}[/#73B8F1]"
                        size_str = "".rjust(size_width)
                    else:
                        name_str = basename
                        size_str = str(size).rjust(size_width)
                    
                    lines.append(f"{size_str}  {prefix}{branch}{icon}  {name_str}")
                    
                    # Recurse into directories
                    if is_dir and full_path in tree:
                        render_tree(full_path, child_prefix)
            
            # Start rendering from the listing path
            # First show root folder
            root_icon = get_icon(path, True)
            root_name = f"[#73B8F1]{path}[/#73B8F1]"
            lines.append(f"{''.rjust(size_width)}  {root_icon}  {root_name}")
            
            # Check if the requested path has any children in the tree
            if path not in tree and items:
                # Items exist but none are direct children of the requested path
                # This can happen if recursive traversal failed or returned unexpected paths
                OutputHelper.print_panel(
                    f"No accessible items found in [yellow]{path}[/yellow]\n\n"
                    f"Received {len(items)} item(s) from device, but none are children of the requested path.\n"
                    "This may indicate:\n"
                    "  • Permission issues accessing subdirectories\n"
                    "  • File system errors during recursive traversal\n"
                    "  • Path mismatch between client and device\n\n"
                    "[dim]Try using 'replx ls' (non-recursive) to see direct children.[/dim]",
                    title=f"Directory Tree: {path}",
                    border_style="yellow"
                )
                return
            
            render_tree(path, "")
            
            OutputHelper.print_panel(
                "\n".join(lines),
                title=f"Directory Tree: {path}",
                border_style="blue"
            )
        else:
            # Non-recursive: simple flat listing
            display_items = []
            for name, size, is_dir in items:
                icon = get_icon(name, is_dir)
                display_items.append((is_dir, name, size, icon))

            if display_items:
                size_width = max(len(str(item[2])) for item in display_items)
                
                lines = []
                for is_dir, f_name, size, icon in display_items:
                    name_str = f"[#73B8F1]{f_name}[/#73B8F1]" if is_dir else f_name
                    size_str = "" if is_dir else str(size)
                    lines.append(f"{size_str.rjust(size_width)}  {icon}  {name_str}")
                
                title = f"Directory Listing: {path}"
                if len(display_items) == 1 and display_items[0][0] is False:
                    title = f"File: {path}"
                
                OutputHelper.print_panel(
                    "\n".join(lines),
                    title=title,
                    border_style="blue"
                )

    except ProtocolError:
        OutputHelper.print_panel(
            f"[red]{path[1:]}[/red] does not exist.",
            title=f"Directory Listing: {path}",
            border_style="red"
        )
    except RuntimeError as e:
        if not OutputHelper.handle_error(e, f"Directory Listing: {path}"):
            OutputHelper.print_panel(
                f"[red]{str(e)}[/red]",
                title="Error",
                border_style="red"
            )




@app.command(rich_help_panel="File Operations")
def put(
    args: Optional[list[str]] = typer.Argument(None, help="Local file(s) and remote destination"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    """
    Upload file(s) or directory to the connected device.
    Last argument is the remote destination path.
    """
    if show_help:
        console = Console(width=CONSOLE_WIDTH)
        help_text = """\
Upload files or directories from your computer to the device.

[bold cyan]Usage:[/bold cyan]
  replx put [yellow]LOCAL[/yellow] [yellow]REMOTE[/yellow]
  replx put [yellow]LOCAL...[/yellow] [yellow]REMOTE[/yellow]   [dim]# Multiple files[/dim]

[bold cyan]Arguments:[/bold cyan]
  [yellow]LOCAL[/yellow]     File(s) or directory on your computer [red][required][/red]
  [yellow]REMOTE[/yellow]    Destination on device [red][required][/red]

[bold cyan]Examples:[/bold cyan]
  [dim]# Single file[/dim]
  replx put main.py /                 [dim]# Upload to root[/dim]
  replx put ./lib/audio.py /lib       [dim]# Upload to /lib[/dim]

  [dim]# Directory (recursive)[/dim]
  replx put ./mylib /lib              [dim]# Upload directory[/dim]

  [dim]# Multiple files[/dim]
  replx put a.py b.py /lib            [dim]# Upload multiple files[/dim]

  [dim]# Wildcards[/dim]
  replx put *.py /lib                 [dim]# Upload all .py files[/dim]
  replx put test*.py /                [dim]# Files starting with test[/dim]

[bold cyan]Note:[/bold cyan]
  • Last argument is always the remote destination
  • Directories are uploaded recursively
  • Wildcards (*,?) are expanded locally
  • Remote directories are created if needed

[bold cyan]Tip:[/bold cyan]
  For installing libraries with compilation, use [yellow]replx pkg update[/yellow] instead.

[bold cyan]Related:[/bold cyan]
  replx get remote local    [dim]# Download files instead[/dim]
  replx pkg update ./file.py   [dim]# Install with .mpy compilation[/dim]"""
        console.print(Panel(help_text, border_style="dim", box=get_panel_box(), width=CONSOLE_WIDTH))
        console.print()
        raise typer.Exit()
    
    status = _ensure_connected()
    device_root_fs = status.get('device_root_fs', '/')
    
    if not args or len(args) < 2:
        OutputHelper.print_panel(
            "Missing required arguments.\n\n"
            "[bold cyan]Usage:[/bold cyan] replx put [yellow]LOCAL... REMOTE[/yellow]\n\n"
            "At least 2 arguments required: local file(s) and remote destination.",
            title="Upload Error",
            border_style="red"
        )
        raise typer.Exit(1)
    
    # Get raw arguments from sys.argv to avoid Typer's shell expansion
    cmd_idx = next((i for i, arg in enumerate(sys.argv) if arg == 'put'), None)
    if cmd_idx is not None and cmd_idx + 1 < len(sys.argv):
        raw_args = []
        for arg in sys.argv[cmd_idx + 1:]:
            if arg.startswith('-'):
                continue
            raw_args.append(arg)
        if len(raw_args) >= 2:
            args = raw_args
    
    # Last argument is remote destination
    remote = args[-1]
    locals = args[:-1]
    
    # Normalize remote path
    if not remote.startswith('/'):
        remote = '/' + remote
    
    # Expand wildcards and collect all files to upload
    files_to_upload = []
    
    for local_pattern in locals:
        # Expand wildcards
        if '*' in local_pattern or '?' in local_pattern:
            matched_files = glob.glob(local_pattern)
            if not matched_files:
                OutputHelper.print_panel(
                    f"[red]{local_pattern}[/red] - pattern did not match any files.",
                    title="Upload Failed",
                    border_style="red"
                )
                continue
            for matched in matched_files:
                if os.path.exists(matched):
                    files_to_upload.append(matched)
        else:
            if not os.path.exists(local_pattern):
                OutputHelper.print_panel(
                    f"[red]{local_pattern}[/red] does not exist.",
                    title="Upload Failed",
                    border_style="red"
                )
                continue
            files_to_upload.append(local_pattern)
    
    if not files_to_upload:
        OutputHelper.print_panel(
            "No files to upload.",
            title="Upload",
            border_style="yellow"
        )
        raise typer.Exit(1)
    
    client = _create_agent_client()
    
    # Check if remote destination is a directory
    try:
        result = client.send_command('is_dir', path=remote)
        is_remote_dir = result.get('is_dir', False)
    except Exception:
        # Remote doesn't exist - will be created if needed
        is_remote_dir = remote.endswith('/')
    
    # Upload files
    total_files = len(files_to_upload)
    success_count = 0
    
    if total_files == 1:
        # Single file/directory - use original behavior
        local = files_to_upload[0]
        is_dir = os.path.isdir(local)
        base_name = os.path.basename(local)
        
        # Determine remote destination
        if is_remote_dir:
            remote_path = posixpath.join(remote, base_name)
        else:
            remote_path = remote
        
        display_remote = remote_path.replace(device_root_fs, "", 1)
        item_type = "Directory" if is_dir else "File"
        
        # Count files for progress (for directories)
        file_count = sum(1 for _, _, files in os.walk(local) for _ in files) if is_dir else 1
        
        # Progress state for streaming updates
        progress_state = {"current": 0, "total": file_count, "file": base_name}
        
        def progress_callback(data):
            """Handle streaming progress updates."""
            progress_state["current"] = data.get("current", 0)
            progress_state["total"] = data.get("total", file_count)
            progress_state["file"] = data.get("file", base_name)
        
        try:
            with Live(OutputHelper.create_progress_panel(0, file_count, title=f"Uploading {base_name}", message=f"Uploading {item_type.lower()}..."), console=OutputHelper._console, refresh_per_second=10) as live:
                # Start upload in background with streaming
                upload_error = [None]
                upload_result = [None]
                
                def do_upload():
                    try:
                        if is_dir:
                            upload_result[0] = client.send_command_streaming(
                                'putdir_from_local_streaming',
                                local_path=local,
                                remote_path=remote_path,
                                timeout=300,
                                progress_callback=progress_callback
                            )
                        else:
                            upload_result[0] = client.send_command_streaming(
                                'put_from_local_streaming',
                                local_path=local,
                                remote_path=remote_path,
                                timeout=60,
                                progress_callback=progress_callback
                            )
                    except Exception as e:
                        upload_error[0] = e
                
                upload_thread = threading.Thread(target=do_upload, daemon=True)
                upload_thread.start()
                
                # Update progress bar while upload is running
                while upload_thread.is_alive():
                    if is_dir:
                        # Directory: show file count progress
                        live.update(OutputHelper.create_progress_panel(
                            progress_state["current"],
                            progress_state["total"],
                            title=f"Uploading {base_name}",
                            message=f"Uploading {progress_state['file']}..."
                        ))
                    else:
                        # Single file: show byte progress
                        live.update(OutputHelper.create_progress_panel(
                            progress_state["current"],
                            progress_state["total"],
                            title=f"Uploading {base_name}",
                            message="Uploading file..."
                        ))
                    time.sleep(0.1)
                
                upload_thread.join()
                
                if upload_error[0]:
                    raise upload_error[0]
                
                # Final update
                live.update(OutputHelper.create_progress_panel(
                    progress_state["total"],
                    progress_state["total"],
                    title=f"Uploading {base_name}"
                ))
            
            OutputHelper.print_panel(
                f"Uploaded [green]{local}[/green]\nto [bright_blue]{display_remote}[/bright_blue]",
                title="Upload Complete",
                border_style="green"
            )
        except Exception as e:
            OutputHelper.print_panel(
                f"Upload failed: [red]{str(e)}[/red]",
                title="Upload Failed",
                border_style="red"
            )
            raise typer.Exit(1)
    else:
        # Multiple files - remote must be a directory
        if not is_remote_dir:
            OutputHelper.print_panel(
                f"Destination [red]{remote}[/red] must be a directory when uploading multiple files.",
                title="Upload Failed",
                border_style="red"
            )
            raise typer.Exit(1)
        
        # Calculate total bytes for all files (including in directories)
        def count_bytes(path):
            """Count total bytes in file or directory."""
            if os.path.isfile(path):
                return os.path.getsize(path)
            elif os.path.isdir(path):
                total = 0
                for root, dirs, files in os.walk(path):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(root, f))
                        except Exception:
                            pass
                return total
            return 0
        
        total_bytes = sum(count_bytes(local) for local in files_to_upload)
        uploaded_bytes = [0]  # Track total uploaded bytes
        
        # Progress state for streaming updates
        current_file_progress = {"file": "", "current": 0, "total": 0, "bytes": 0}
        
        def progress_callback(data):
            """Handle streaming progress updates for current file."""
            current_file_progress["file"] = data.get("file", "")
            current_file_progress["current"] = data.get("current", 0)
            current_file_progress["total"] = data.get("total", 0)
            current_file_progress["bytes"] = data.get("bytes", 0)
        
        with Live(OutputHelper.create_progress_panel(0, max(total_bytes, 1), title=f"Uploading {total_files} item(s)", message="Starting..."), console=OutputHelper._console, refresh_per_second=10) as live:
            for idx, local in enumerate(files_to_upload):
                base_name = os.path.basename(local)
                is_dir = os.path.isdir(local)
                remote_path = posixpath.join(remote, base_name)
                
                item_bytes = count_bytes(local)
                item_start_bytes = uploaded_bytes[0]
                
                # Reset current file progress
                current_file_progress["file"] = base_name
                current_file_progress["current"] = 0
                current_file_progress["total"] = 0
                current_file_progress["bytes"] = 0
                
                live.update(OutputHelper.create_progress_panel(
                    uploaded_bytes[0], total_bytes,
                    title=f"Uploading {total_files} item(s)",
                    message=f"Starting {base_name}..."
                ))
                
                max_retries = 2
                upload_success = False
                
                for retry in range(max_retries):
                    try:
                        # Create new client for each file to ensure clean connection state
                        with _create_agent_client() as file_client:
                            # Use streaming for real-time progress
                            if is_dir:
                                # Start upload in background thread to monitor progress
                                upload_result = [None]
                                upload_error = [None]
                                upload_done = [False]
                                
                                def upload_dir():
                                    try:
                                        upload_result[0] = file_client.send_command_streaming(
                                            'putdir_from_local_streaming',
                                            local_path=local,
                                            remote_path=remote_path,
                                            timeout=300,
                                            progress_callback=progress_callback
                                        )
                                    except Exception as e:
                                        upload_error[0] = e
                                    finally:
                                        upload_done[0] = True
                                
                                thread = threading.Thread(target=upload_dir, daemon=True)
                                thread.start()
                                
                                # Monitor progress and update display
                                while not upload_done[0]:
                                    curr = current_file_progress.get("current", 0)
                                    tot = current_file_progress.get("total", 1)
                                    file_name = current_file_progress.get("file", "")
                                    
                                    # Estimate bytes based on file progress if not directly provided
                                    if tot > 0:
                                        progress_ratio = curr / tot
                                        estimated_bytes = item_start_bytes + int(item_bytes * progress_ratio)
                                        message = f"{base_name}/: {file_name} ({curr}/{tot} files)"
                                    else:
                                        estimated_bytes = item_start_bytes
                                        message = f"Scanning {base_name}/..."
                                    
                                    live.update(OutputHelper.create_progress_panel(
                                        estimated_bytes, total_bytes,
                                        title=f"Uploading {total_files} item(s)",
                                        message=message
                                    ))
                                    time.sleep(0.1)
                                
                                thread.join()
                                
                                if upload_error[0]:
                                    raise upload_error[0]
                                
                                # Update total uploaded bytes after directory completion
                                uploaded_bytes[0] = item_start_bytes + item_bytes
                            else:
                                result = file_client.send_command_streaming(
                                    'put_from_local_streaming',
                                    local_path=local,
                                    remote_path=remote_path,
                                    timeout=60,
                                    progress_callback=progress_callback
                                )
                                # Update total uploaded bytes after file completion
                                uploaded_bytes[0] = item_start_bytes + item_bytes
                        
                        success_count += 1
                        upload_success = True
                        live.update(OutputHelper.create_progress_panel(
                            uploaded_bytes[0], total_bytes,
                            title=f"Uploading {total_files} item(s)",
                            message=f"✓ {base_name}"
                        ))
                        break  # Success, exit retry loop
                        
                    except Exception as e:
                        if retry == max_retries - 1:
                            # Last retry failed
                            OutputHelper._console.print(f"[red]Failed to upload {base_name}: {str(e)}[/red]")
                        else:
                            # Wait before retry
                            time.sleep(0.2)
                
                if not upload_success:
                    live.update(OutputHelper.create_progress_panel(
                        uploaded_bytes[0], total_bytes,
                        title=f"Uploading {total_files} item(s)",
                        message=f"✗ {base_name}"
                    ))
            
            live.update(OutputHelper.create_progress_panel(total_bytes, total_bytes, title=f"Uploading {total_files} item(s)"))
        
        # Format bytes for display
        def format_bytes(b):
            if b < 1024:
                return f"{b} B"
            elif b < 1024 * 1024:
                return f"{b / 1024:.1f} KB"
            else:
                return f"{b / (1024 * 1024):.1f} MB"
        
        display_remote = remote.replace(device_root_fs, "", 1)
        OutputHelper.print_panel(
            f"Uploaded [green]{success_count}[/green] out of {total_files} item(s) ([green]{format_bytes(uploaded_bytes[0])}[/green])\nto [bright_blue]{display_remote}[/bright_blue]",
            title="Upload Complete",
            border_style="green" if success_count == total_files else "yellow"
        )



