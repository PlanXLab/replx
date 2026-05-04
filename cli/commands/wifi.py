import json
import re

import typer
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from ..agent.client import AgentClient
from ..app import app
from ..connection import _create_agent_client, _ensure_connected
from ..helpers import CONSOLE_WIDTH, OutputHelper, get_panel_box


@app.command(rich_help_panel="Connectivity")
def wifi(
    args: list[str] = typer.Argument(None, help="WiFi arguments"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True)
):
    if show_help:
        console = OutputHelper.make_console(width=CONSOLE_WIDTH)
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
    console = OutputHelper.make_console()
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

            # Connection status can lag behind final association/auth state,
            # especially on busy APs. Re-check for a short stabilization window.
            verify = None
            for _ in range(8):  # up to ~4s
                pytime.sleep(0.5)
                verify = _wifi_check_current_connection(client, target_ssid=ssid)
                if not verify.get("connected"):
                    continue

                same_ssid = verify.get("same_ssid")
                current_ssid = verify.get("ssid")
                # Accept if SSID matches, or if SSID is temporarily unavailable
                # but interface is already connected.
                if same_ssid or not current_ssid:
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
        try:
            verify = None
            for _ in range(8):  # up to ~4s
                pytime.sleep(0.5)
                verify = _wifi_check_current_connection(client, target_ssid=ssid)
                if not verify.get("connected"):
                    continue
                same_ssid = verify.get("same_ssid")
                current_ssid = verify.get("ssid")
                if same_ssid or not current_ssid:
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

import utime
wlan = network.WLAN(network.STA_IF)
was_active = wlan.active()
if not was_active:
    wlan.active(True)
    utime.sleep_ms(200)

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
        
        console = OutputHelper.make_console(width=CONSOLE_WIDTH)
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
        icon, color = chr(0xF04A2), "green"
    elif rssi >= -65:
        icon, color = chr(0xF08BE), "green"
    elif rssi >= -75:
        icon, color = chr(0xF08BD), "yellow"
    elif rssi >= -85:
        icon, color = chr(0xF08BC), "yellow"
    else:
        icon, color = chr(0xF08BF), "red"
    return f"[{color}]{icon}[/{color}]"


def _signal_str(rssi: int) -> str:
    if rssi >= -50:
        color = "green"
    elif rssi >= -65:
        color = "green"
    elif rssi >= -75:
        color = "yellow"
    elif rssi >= -85:
        color = "yellow"
    else:
        color = "red"
    return f"[{color}]{rssi}dBm[/{color}]"
