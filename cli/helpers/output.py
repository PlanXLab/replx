"""Output formatting and display utilities."""
import os
import sys
import re

from rich.console import Console
from rich.panel import Panel

from . import get_panel_box, CONSOLE_WIDTH, get_global_context


def format_size(size_bytes: int) -> str:
    """Format bytes to human readable string."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


class OutputHelper:
    """Output formatting and display utilities."""
    
    # Ensure stdout uses UTF-8 encoding
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    
    _console = Console()
    PANEL_WIDTH = None
    
    @staticmethod
    def _get_panel_width():
        """Get panel width."""
        if OutputHelper.PANEL_WIDTH is None:
            OutputHelper.PANEL_WIDTH = CONSOLE_WIDTH
        return OutputHelper.PANEL_WIDTH
    
    @staticmethod
    def print_panel(content: str, title: str = "", border_style: str = "blue"):
        """Print content in a rich panel box."""
        width = OutputHelper._get_panel_width()
        OutputHelper._console.print(Panel(content, title=title, title_align="left", border_style=border_style, box=get_panel_box(), expand=True, width=width))
    
    @staticmethod
    def create_progress_panel(current: int, total: int, title: str = "Progress", message: str = "", counter_text: str = None):
        """Create a progress panel for live updates with consistent width.
        
        Args:
            current: Current progress value
            total: Total progress value
            title: Panel title
            message: Optional message line above progress bar
            counter_text: Optional custom counter text (default: '(current/total)')
        """
        pct = 0 if total == 0 else min(1.0, current / total)
        
        # Calculate bar length based on panel width
        # Panel width - borders (4) - padding (2) - brackets (2) - percentage text (5) - space (1) - counter_text (~22)
        panel_width = OutputHelper._get_panel_width()
        bar_length = max(20, panel_width - 40)  # Minimum 20 chars for bar, leave room for counter
        
        block = min(bar_length, int(round(bar_length * pct)))
        bar = "█" * block + "░" * (bar_length - block)
        percent = int(pct * 100)
        
        # Use custom counter_text or default to (current/total)
        if counter_text is None:
            counter_text = f"({current}/{total})"
        
        content_lines = []
        if message:
            content_lines.append(message)
        content_lines.append(f"[{bar}] {percent}% {counter_text}")
        
        width = OutputHelper._get_panel_width()
        return Panel("\n".join(content_lines), title=title, title_align="left", border_style="green", box=get_panel_box(), expand=True, width=width)
    
    @staticmethod
    def create_spinner_panel(message: str, title: str = "Processing", spinner_frames: list = None, frame_idx: int = 0):
        """Create a spinner panel for indeterminate progress."""
        if spinner_frames is None:
            spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        
        spinner = spinner_frames[frame_idx % len(spinner_frames)]
        content = f"{spinner}  {message}"
        width = OutputHelper._get_panel_width()
        return Panel(content, title=title, title_align="left", border_style="yellow", box=get_panel_box(), expand=True, width=width)
    
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
        _core, _device, _version, _device_root_fs, _device_path = get_global_context()
        
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
                except (OSError, IndexError):
                    pass

        except IndexError:
            err_content = out[-1].strip()
        
        OutputHelper._console.print(f"[bright_magenta]{err_content}[/bright_magenta]")

    @staticmethod
    def handle_error(error: Exception, context: str = "Error") -> bool:
        """
        Handle common errors with user-friendly messages.
        
        Args:
            error: The exception to handle
            context: Context string for the error (e.g., "Directory Listing")
            
        Returns:
            True if error was handled, False if it should be re-raised
        """
        error_msg = str(error)
        
        if 'is busy' in error_msg:
            import re
            
            # Check if REPL session is active
            repl_match = re.search(r'Connection (\S+) is busy.*REPL session is active', error_msg)
            if repl_match:
                port = repl_match.group(1)
                message = (
                    f"[bright_cyan]{port}[/bright_cyan] has an active REPL session in another terminal.\n\n"
                    "[dim]Exit REPL first with [bold]exit()[/bold] or [bold]Ctrl+D[/bold] in the other terminal.[/dim]\n\n"
                    "Run [bright_cyan]replx session[/bright_cyan] to check connection status."
                )
                OutputHelper.print_panel(
                    message,
                    title="REPL Active",
                    border_style="yellow"
                )
                return True
            
            # Check if detached script is running
            detached_match = re.search(r'Connection (\S+) is busy.*detached script is running', error_msg)
            if detached_match:
                port = detached_match.group(1)
                message = (
                    f"[bright_cyan]{port}[/bright_cyan] is running a background script.\n\n"
                    "[dim]Stop it first with [bold]replx reset[/bold] or [bold]replx run --stop[/bold].[/dim]\n\n"
                    "Run [bright_cyan]replx session[/bright_cyan] to check connection status."
                )
                OutputHelper.print_panel(
                    message,
                    title="Script Running",
                    border_style="yellow"
                )
                return True
            
            # Parse connection and command from error message
            # Format: "Connection {port} is busy. Another command ({command}) is currently running..."
            match = re.search(r'Connection (\S+) is busy.*Another command \((\w+)\)', error_msg)
            if match:
                port = match.group(1)
                command = match.group(2)
                message = (
                    f"[bright_cyan]{port}[/bright_cyan] is currently executing "
                    f"[yellow]{command}[/yellow].\n\n"
                    "[dim]Wait for it to complete, or press [bold]Ctrl+C[/bold] in the other terminal to stop it.[/dim]\n\n"
                    "Run [bright_cyan]replx session[/bright_cyan] to check connection status."
                )
            else:
                message = (
                    f"{error_msg}\n\n"
                    "Run [bright_cyan]replx session[/bright_cyan] to check connection status."
                )
            OutputHelper.print_panel(
                message,
                title="Connection Busy",
                border_style="yellow"
            )
            return True
        elif 'Not connected' in error_msg:
            OutputHelper.print_panel(
                "No active connection.\n\n"
                "Run [bright_blue]replx --port PORT setup[/bright_blue] first.",
                title="Not Connected",
                border_style="red"
            )
            return True
        
        return False
