import json
import sys

import typer
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from ..agent.client import AgentClient
from ..app import app
from ..connection import _create_agent_client, _ensure_connected
from ..helpers import CONSOLE_WIDTH, OutputHelper, get_panel_box


# ---------------------------------------------------------------------------
# Top-level command
# ---------------------------------------------------------------------------

@app.command(name="ble", rich_help_panel="Connectivity")
def ble(
    args: list[str] = typer.Argument(None, help="BLE arguments"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True),
):
    """Manage BLE (Bluetooth Low Energy) on the connected device."""
    if show_help:
        help_text = """\
Manage BLE on the connected MicroPython device using aioble.

[bold cyan]Usage:[/bold cyan]
  replx ble                                               [dim]# BLE status[/dim]
  replx ble off                                           [dim]# Disable BLE[/dim]
  replx ble scan [yellow][duration_ms][/yellow]                          [dim]# Scan for devices[/dim]
  replx ble advertise [yellow][name] [duration_ms][/yellow]               [dim]# Broadcast advertisement[/dim]
    [dim]--no-connect                                       #  Broadcaster mode (non-connectable)[/dim]
  replx ble serve [cyan]--svc UUID --char UUID[/cyan] [OPTIONS]        [dim]# Run GATT server (Ctrl+C to stop)[/dim]
  replx ble connect [yellow]ADDR[/yellow]                                 [dim]# Test connection[/dim]
  replx ble read [yellow]ADDR SVC_UUID CHAR_UUID[/yellow]                 [dim]# Read characteristic[/dim]
  replx ble write [yellow]ADDR SVC_UUID CHAR_UUID HEX[/yellow]            [dim]# Write characteristic[/dim]
  replx ble notify [yellow]ADDR SVC_UUID CHAR_UUID[/yellow]               [dim]# Subscribe to notifications[/dim]
    [dim]--count N, --timeout MS[/dim]
  replx ble stream send [yellow]ADDR PSM HEX[/yellow]                     [dim]# L2CAP send[/dim]
  replx ble stream recv [yellow]PSM[/yellow]                              [dim]# L2CAP receive (Ctrl+C to stop)[/dim]

[bold cyan]serve Options:[/bold cyan]
  [cyan]--svc UUID[/cyan]       Service UUID (e.g. 0x1234 or full 128-bit)
  [cyan]--char UUID[/cyan]      Characteristic UUID
  [cyan]--value HEX[/cyan]      Initial value (hex bytes, e.g. AABBCC)
  [cyan]--read[/cyan]           Add READ property
  [cyan]--write[/cyan]          Add WRITE property
  [cyan]--notify[/cyan]         Add NOTIFY property
  [cyan]--indicate[/cyan]       Add INDICATE property
  [cyan]--name NAME[/cyan]      GAP device name (default: replx-ble)
  [cyan]--interval MS[/cyan]    Auto-notify interval in ms (requires --notify)

[bold cyan]Note:[/bold cyan]
  • Requires aioble on the device: replx mip install aioble
  • serve / advertise / stream recv run until Ctrl+C"""
        OutputHelper.print_panel(help_text, title="ble", border_style="help")
        raise typer.Exit()

    _ensure_connected()
    client = _create_agent_client()

    if not args:
        _ble_info(client)
        return

    cmd = args[0]

    if cmd == "off":
        _ble_off(client)

    elif cmd == "scan":
        duration_ms = int(args[1]) if len(args) >= 2 else 5000
        _ble_scan(client, duration_ms)

    elif cmd == "advertise":
        # parse: advertise [name] [duration_ms] [--no-connect]
        no_connect = "--no-connect" in args
        positional = [a for a in args[1:] if not a.startswith("-")]
        name = positional[0] if len(positional) >= 1 else "replx-ble"
        duration_ms = int(positional[1]) if len(positional) >= 2 else 10000
        _ble_advertise(client, name, duration_ms, connectable=not no_connect)

    elif cmd == "serve":
        _ble_serve_from_args(client, args[1:])

    elif cmd == "connect":
        if len(args) < 2:
            OutputHelper.print_panel(
                "Usage: [bright_blue]replx ble connect ADDR[/bright_blue]",
                title="BLE Error", border_style="error",
            )
            raise typer.Exit(1)
        _ble_connect(client, args[1])

    elif cmd == "read":
        if len(args) < 4:
            OutputHelper.print_panel(
                "Usage: [bright_blue]replx ble read ADDR SVC_UUID CHAR_UUID[/bright_blue]",
                title="BLE Error", border_style="error",
            )
            raise typer.Exit(1)
        _ble_read(client, args[1], args[2], args[3])

    elif cmd == "write":
        if len(args) < 5:
            OutputHelper.print_panel(
                "Usage: [bright_blue]replx ble write ADDR SVC_UUID CHAR_UUID HEX[/bright_blue]",
                title="BLE Error", border_style="error",
            )
            raise typer.Exit(1)
        _ble_write(client, args[1], args[2], args[3], args[4])

    elif cmd == "notify":
        if len(args) < 4:
            OutputHelper.print_panel(
                "Usage: [bright_blue]replx ble notify ADDR SVC_UUID CHAR_UUID[/bright_blue]",
                title="BLE Error", border_style="error",
            )
            raise typer.Exit(1)
        # parse --count and --timeout
        count = 1
        timeout_ms = 10000
        i = 4
        while i < len(args):
            if args[i] == "--count" and i + 1 < len(args):
                count = int(args[i + 1]); i += 2
            elif args[i] == "--timeout" and i + 1 < len(args):
                timeout_ms = int(args[i + 1]); i += 2
            else:
                i += 1
        _ble_notify(client, args[1], args[2], args[3], count, timeout_ms)

    elif cmd == "stream":
        if len(args) < 2:
            OutputHelper.print_panel(
                "Usage: [bright_blue]replx ble stream send ADDR PSM HEX[/bright_blue] "
                "or [bright_blue]replx ble stream recv PSM[/bright_blue]",
                title="BLE Error", border_style="error",
            )
            raise typer.Exit(1)
        sub = args[1]
        if sub == "send":
            if len(args) < 5:
                OutputHelper.print_panel(
                    "Usage: [bright_blue]replx ble stream send ADDR PSM HEX[/bright_blue]",
                    title="BLE Error", border_style="error",
                )
                raise typer.Exit(1)
            _ble_stream_send(client, args[2], args[3], args[4])
        elif sub == "recv":
            if len(args) < 3:
                OutputHelper.print_panel(
                    "Usage: [bright_blue]replx ble stream recv PSM[/bright_blue]",
                    title="BLE Error", border_style="error",
                )
                raise typer.Exit(1)
            _ble_stream_recv(client, args[2])
        else:
            OutputHelper.print_panel(
                f"Unknown stream subcommand: [yellow]{sub}[/yellow]\n\n"
                "Use [bright_blue]replx ble --help[/bright_blue] for usage.",
                title="BLE Error", border_style="error",
            )
            raise typer.Exit(1)

    else:
        OutputHelper.print_panel(
            f"Unknown subcommand: [yellow]{cmd}[/yellow]\n\n"
            "Use [bright_blue]replx ble --help[/bright_blue] for usage.",
            title="BLE Error", border_style="error",
        )
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AIOBLE_CHECK = '''\
try:
    import aioble
except ImportError:
    import json
    print(json.dumps({"error": "aioble not found"}))
    raise SystemExit
'''

def _parse_uuid(uuid_str: str) -> str:
    """Return a Python expression string for a UUID usable in exec code."""
    s = uuid_str.strip()
    if s.lower().startswith("0x"):
        return s  # e.g. 0x1234 — bluetooth.UUID accepts int
    try:
        int(s, 16)
        return f"0x{s}"
    except ValueError:
        pass
    # Full 128-bit UUID string
    return f'"{s}"'


def _hex_to_bytes_expr(hex_str: str) -> str:
    """Return a bytes literal expression from a hex string like 'AABBCC'."""
    h = hex_str.strip().replace(" ", "").upper()
    return "bytes.fromhex('" + h + "')"


def _addr_to_parts(addr: str) -> tuple[str, str]:
    """Return (addr_bytes_expr, addr_type_expr) for an address string."""
    # Accept 'AA:BB:CC:DD:EE:FF' or 'AA:BB:CC:DD:EE:FF/1'
    if "/" in addr:
        addr_str, addr_type_str = addr.rsplit("/", 1)
        addr_type = int(addr_type_str)
    else:
        addr_str = addr
        addr_type = 1  # random is the common default for BLE
    octets = addr_str.split(":")
    byte_vals = ", ".join(str(int(o, 16)) for o in octets)
    return f"bytes([{byte_vals}])", str(addr_type)


def _exec(client: AgentClient, code: str, timeout: float = 15.0) -> dict:
    result = client.send_command("exec", code=code, timeout=timeout)
    output = result.get("output", "").strip()
    if not output:
        return {}
    # Take last non-empty line as JSON result
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return {"raw": output}


def _check_aioble(data: dict) -> bool:
    """Return False and print error if aioble is missing."""
    if data.get("error") == "aioble not found":
        OutputHelper.print_panel(
            "aioble is not installed on the device.\n\n"
            "Install it with: [bright_blue]replx mip install aioble[/bright_blue]",
            title="BLE Error", border_style="error",
        )
        return False
    return True


# ---------------------------------------------------------------------------
# ble (info)
# ---------------------------------------------------------------------------

def _ble_info(client: AgentClient):
    code = _AIOBLE_CHECK + '''
import bluetooth, json

def _cfg(ble, key):
    try:
        return ble.config(key)
    except (ValueError, OSError):
        return None

ble = bluetooth.BLE()
ble.active(True)
mac_raw = ble.config("mac")
addr_type = mac_raw[0]
mac_bytes = mac_raw[1]
mac_str = ":".join("%02X" % b for b in mac_bytes)
print(json.dumps({
    "active": True,
    "mac": mac_str,
    "addr_type": addr_type,
    "addr_mode": _cfg(ble, "addr_mode"),
    "gap_name": _cfg(ble, "gap_name"),
    "mtu": _cfg(ble, "mtu"),
}))
'''
    try:
        data = _exec(client, code)
        if not _check_aioble(data):
            raise typer.Exit(1)
        lines = []
        lines.append(f"  [green]● Active[/green]")
        lines.append(f"  MAC:       [bright_cyan]{data.get('mac', '?')}[/bright_cyan]")
        lines.append(f"  Addr type: {data.get('addr_type', '?')} "
                     f"({'random' if data.get('addr_type') == 1 else 'public'})")
        lines.append(f"  Addr mode: {data.get('addr_mode', '?')}")
        lines.append(f"  GAP name:  {data.get('gap_name', '?')}")
        lines.append(f"  MTU:       {data.get('mtu', '?')} bytes")
        OutputHelper.print_panel("\n".join(lines), title="BLE Status", border_style="data")
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(f"Failed to get BLE status: {e}", title="BLE Error", border_style="error")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# ble off
# ---------------------------------------------------------------------------

def _ble_off(client: AgentClient):
    code = '''
import bluetooth, json
ble = bluetooth.BLE()
ble.active(False)
print(json.dumps({"ok": True}))
'''
    try:
        data = _exec(client, code)
        if data.get("ok"):
            OutputHelper.print_panel("BLE disabled.", title="BLE", border_style="success")
        else:
            OutputHelper.print_panel("BLE deactivation returned unexpectedly.", title="BLE", border_style="warning")
    except Exception as e:
        OutputHelper.print_panel(f"Failed to disable BLE: {e}", title="BLE Error", border_style="error")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# ble scan
# ---------------------------------------------------------------------------

def _ble_scan(client: AgentClient, duration_ms: int):
    code = _AIOBLE_CHECK + f'''
import aioble, asyncio, json, bluetooth

async def main():
    results = []
    async with aioble.scan({duration_ms}, interval_us=30000, window_us=30000, active=True) as scanner:
        async for result in scanner:
            addr_str = ":".join("%02X" % b for b in bytes(result.device.addr))
            entry = {{
                "addr": addr_str,
                "addr_type": result.device.addr_type,
                "rssi": result.rssi,
                "name": result.name(),
                "services": [str(s) for s in (result.services() or [])],
            }}
            results.append(entry)
    # deduplicate by addr
    seen = {{}}
    for e in results:
        key = e["addr"]
        if key not in seen or e["rssi"] > seen[key]["rssi"]:
            seen[key] = e
    print(json.dumps(list(seen.values())))

asyncio.run(main())
'''
    console = OutputHelper.make_console()
    spinner = Spinner("dots", text=Text(f" Scanning for {duration_ms} ms...", style="bright_cyan"))
    try:
        with Live(spinner, console=console, refresh_per_second=10, transient=True):
            data = _exec(client, code, timeout=duration_ms / 1000 + 8)

        if not _check_aioble(data if isinstance(data, dict) else {}):
            raise typer.Exit(1)

        devices = data if isinstance(data, list) else []

        if not devices:
            OutputHelper.print_panel("No BLE devices found.", title="BLE Scan", border_style="neutral")
            return

        table = Table(box=None, show_header=True, pad_edge=False)
        table.add_column("Address", style="bright_cyan", no_wrap=True)
        table.add_column("T", justify="center")
        table.add_column("RSSI", justify="right", style="yellow")
        table.add_column("Name", style="green")
        table.add_column("Services")

        for d in sorted(devices, key=lambda x: x.get("rssi", -999), reverse=True):
            svcs = ", ".join(d.get("services") or []) or "-"
            table.add_row(
                d.get("addr", "?"),
                str(d.get("addr_type", "?")),
                str(d.get("rssi", "?")),
                d.get("name") or "-",
                svcs,
            )

        from rich.console import Console
        from io import StringIO
        buf = StringIO()
        c = Console(file=buf, highlight=False)
        c.print(table)
        OutputHelper.print_panel(buf.getvalue().rstrip(), title=f"BLE Scan  ({len(devices)} found)", border_style="data")

    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(f"Scan failed: {e}", title="BLE Error", border_style="error")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# ble advertise
# ---------------------------------------------------------------------------

def _ble_advertise(client: AgentClient, name: str, duration_ms: int, connectable: bool):
    escaped_name = name.replace('"', '\\"')
    mode = "connectable" if connectable else "broadcaster (non-connectable)"
    code = _AIOBLE_CHECK + f'''
import aioble, asyncio, json

async def main():
    aioble.core.log_level = 0
    connection = await aioble.advertise(
        {duration_ms * 1000},
        name="{escaped_name}",
        connectable={str(connectable)},
        timeout_ms={duration_ms},
    )
    if connection:
        addr = ":".join("%02X" % b for b in bytes(connection.device.addr))
        print(json.dumps({{"ok": True, "connected_from": addr}}))
    else:
        print(json.dumps({{"ok": True, "connected_from": None}}))

asyncio.run(main())
'''
    console = OutputHelper.make_console()
    spinner = Spinner("dots", text=Text(f" Advertising as \"{name}\" ({mode})...", style="bright_cyan"))
    try:
        with Live(spinner, console=console, refresh_per_second=10, transient=True):
            data = _exec(client, code, timeout=duration_ms / 1000 + 8)

        if not _check_aioble(data if isinstance(data, dict) else {}):
            raise typer.Exit(1)

        if isinstance(data, dict) and data.get("ok"):
            peer = data.get("connected_from")
            if peer:
                OutputHelper.print_panel(
                    f"Advertisement complete.\n  Connected from: [bright_cyan]{peer}[/bright_cyan]",
                    title="BLE Advertise", border_style="success",
                )
            else:
                OutputHelper.print_panel(
                    "Advertisement complete. No connection received.",
                    title="BLE Advertise", border_style="neutral",
                )
        else:
            OutputHelper.print_panel(
                f"Advertisement finished. Response: {data}",
                title="BLE Advertise", border_style="neutral",
            )
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(f"Advertise failed: {e}", title="BLE Error", border_style="error")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# ble serve
# ---------------------------------------------------------------------------

def _ble_serve_from_args(client: AgentClient, args: list[str]):
    """Parse serve flags and dispatch to _ble_serve."""
    svc_uuid = None
    char_uuid = None
    value_hex = None
    name = "replx-ble"
    interval_ms = None
    props = []

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--svc" and i + 1 < len(args):
            svc_uuid = args[i + 1]; i += 2
        elif a == "--char" and i + 1 < len(args):
            char_uuid = args[i + 1]; i += 2
        elif a == "--value" and i + 1 < len(args):
            value_hex = args[i + 1]; i += 2
        elif a == "--name" and i + 1 < len(args):
            name = args[i + 1]; i += 2
        elif a == "--interval" and i + 1 < len(args):
            interval_ms = int(args[i + 1]); i += 2
        elif a == "--read":
            props.append("READ"); i += 1
        elif a == "--write":
            props.append("WRITE"); i += 1
        elif a == "--notify":
            props.append("NOTIFY"); i += 1
        elif a == "--indicate":
            props.append("INDICATE"); i += 1
        else:
            i += 1

    if not svc_uuid or not char_uuid:
        OutputHelper.print_panel(
            "Required: [bright_blue]--svc UUID --char UUID[/bright_blue]\n\n"
            "Use [bright_blue]replx ble --help[/bright_blue] for usage.",
            title="BLE Error", border_style="error",
        )
        raise typer.Exit(1)

    if not props:
        props = ["READ", "WRITE", "NOTIFY"]

    _ble_serve(client, svc_uuid, char_uuid, value_hex, name, props, interval_ms)


def _ble_serve(
    client: AgentClient,
    svc_uuid: str,
    char_uuid: str,
    value_hex: str | None,
    name: str,
    props: list[str],
    interval_ms: int | None,
):
    svc_expr = _parse_uuid(svc_uuid)
    char_expr = _parse_uuid(char_uuid)
    escaped_name = name.replace('"', '\\"')

    # Build flags expression
    flag_parts = []
    if "READ" in props:
        flag_parts.append("aioble.FLAG_READ")
    if "WRITE" in props:
        flag_parts.append("aioble.FLAG_WRITE")
    if "NOTIFY" in props:
        flag_parts.append("aioble.FLAG_NOTIFY")
    if "INDICATE" in props:
        flag_parts.append("aioble.FLAG_INDICATE")
    flags_expr = " | ".join(flag_parts) if flag_parts else "aioble.FLAG_READ"

    if value_hex:
        init_value_expr = _hex_to_bytes_expr(value_hex)
    else:
        init_value_expr = "b''"

    interval_code = ""
    if interval_ms and "NOTIFY" in props:
        interval_code = f"""
    async def _auto_notify():
        import time
        counter = 0
        while True:
            await asyncio.sleep_ms({interval_ms})
            val = char.read()
            char._notify(conn, val)
            counter += 1
    asyncio.create_task(_auto_notify())
"""

    code = _AIOBLE_CHECK + f'''
import aioble, asyncio, bluetooth, json

async def main():
    svc = aioble.Service(bluetooth.UUID({svc_expr}))
    char = aioble.Characteristic(svc, bluetooth.UUID({char_expr}), {flags_expr})
    aioble.register_services(svc)
    bluetooth.BLE().config(gap_name="{escaped_name}")

    char.write({init_value_expr})

    import json as _json
    print(_json.dumps({{"event": "ready", "svc": "{svc_uuid}", "char": "{char_uuid}", "props": {props!r}}}))

    while True:
        conn = await aioble.advertise(100_000, name="{escaped_name}", connectable=True)
        addr = ":".join("%02X" % b for b in bytes(conn.device.addr))
        print(_json.dumps({{"event": "connected", "addr": addr}}))
{interval_code}
        async with conn:
            while conn.is_connected():
                await asyncio.sleep_ms(200)
        print(_json.dumps({{"event": "disconnected", "addr": addr}}))

asyncio.run(main())
'''

    console = OutputHelper.make_console()

    events: list[str] = []

    def _on_event(data: dict):
        evt = data.get("output", "").strip() if isinstance(data, dict) else str(data)
        for line in evt.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                event_type = obj.get("event", "")
                if event_type == "ready":
                    msg = (f"[green]● GATT server ready[/green]\n"
                           f"  SVC:   [bright_cyan]{obj.get('svc')}[/bright_cyan]\n"
                           f"  CHAR:  [bright_cyan]{obj.get('char')}[/bright_cyan]\n"
                           f"  Props: {', '.join(obj.get('props', []))}\n"
                           f"  Name:  \"{escaped_name}\" — advertising...")
                elif event_type == "connected":
                    msg = f"  [green]→ Connected[/green]: [bright_cyan]{obj.get('addr')}[/bright_cyan]"
                elif event_type == "disconnected":
                    msg = f"  [dim]← Disconnected[/dim]: {obj.get('addr')} — re-advertising..."
                elif event_type == "write":
                    msg = f"  [yellow]✎ Write[/yellow]: {obj.get('value')}"
                else:
                    msg = f"  {line}"
            except (json.JSONDecodeError, ValueError):
                msg = f"  {line}"
            events.append(msg)
            console.print(msg)

    try:
        OutputHelper.print_panel(
            f"Starting GATT server. Press [bold]Ctrl+C[/bold] to stop.",
            title="BLE Serve", border_style="mode",
        )
        client.send_command_streaming("exec", code=code, timeout=86400.0, progress_callback=_on_event)
        OutputHelper.print_panel("GATT server stopped.", title="BLE Serve", border_style="neutral")
    except KeyboardInterrupt:
        OutputHelper.print_panel("GATT server stopped by user.", title="BLE Serve", border_style="neutral")
    except Exception as e:
        OutputHelper.print_panel(f"Serve failed: {e}", title="BLE Error", border_style="error")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# ble connect
# ---------------------------------------------------------------------------

def _ble_connect(client: AgentClient, addr: str):
    addr_bytes_expr, addr_type_expr = _addr_to_parts(addr)
    code = _AIOBLE_CHECK + f'''
import aioble, asyncio, bluetooth, json

async def main():
    device = aioble.Device({addr_type_expr}, {addr_bytes_expr})
    try:
        conn = await device.connect(timeout_ms=5000)
        await conn.disconnect()
        print(json.dumps({{"ok": True, "addr": "{addr}"}}))
    except Exception as e:
        print(json.dumps({{"ok": False, "error": str(e)}}))

asyncio.run(main())
'''
    console = OutputHelper.make_console()
    spinner = Spinner("dots", text=Text(f" Connecting to {addr}...", style="bright_cyan"))
    try:
        with Live(spinner, console=console, refresh_per_second=10, transient=True):
            data = _exec(client, code, timeout=12)

        if not _check_aioble(data):
            raise typer.Exit(1)

        if data.get("ok"):
            OutputHelper.print_panel(
                f"[green]● Reachable[/green]: [bright_cyan]{addr}[/bright_cyan]",
                title="BLE Connect", border_style="success",
            )
        else:
            OutputHelper.print_panel(
                f"[red]✗ Unreachable[/red]: {addr}\n  {data.get('error', '')}",
                title="BLE Connect", border_style="error",
            )
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(f"Connect failed: {e}", title="BLE Error", border_style="error")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# ble read
# ---------------------------------------------------------------------------

def _ble_read(client: AgentClient, addr: str, svc_uuid: str, char_uuid: str):
    addr_bytes_expr, addr_type_expr = _addr_to_parts(addr)
    svc_expr = _parse_uuid(svc_uuid)
    char_expr = _parse_uuid(char_uuid)
    code = _AIOBLE_CHECK + f'''
import aioble, asyncio, bluetooth, json, binascii

async def main():
    device = aioble.Device({addr_type_expr}, {addr_bytes_expr})
    try:
        async with await device.connect(timeout_ms=5000) as conn:
            svc = await conn.service(bluetooth.UUID({svc_expr}), timeout_ms=3000)
            if svc is None:
                print(json.dumps({{"error": "service not found"}}))
                return
            char = await svc.characteristic(bluetooth.UUID({char_expr}), timeout_ms=3000)
            if char is None:
                print(json.dumps({{"error": "characteristic not found"}}))
                return
            data = await char.read(timeout_ms=3000)
            print(json.dumps({{
                "value_hex": binascii.hexlify(data).decode(),
                "value_bytes": list(data),
            }}))
    except Exception as e:
        print(json.dumps({{"error": str(e)}}))

asyncio.run(main())
'''
    console = OutputHelper.make_console()
    spinner = Spinner("dots", text=Text(f" Reading from {addr}...", style="bright_cyan"))
    try:
        with Live(spinner, console=console, refresh_per_second=10, transient=True):
            data = _exec(client, code, timeout=15)

        if not _check_aioble(data):
            raise typer.Exit(1)

        if "error" in data:
            OutputHelper.print_panel(
                f"[red]Read failed:[/red] {data['error']}",
                title="BLE Read", border_style="error",
            )
            raise typer.Exit(1)

        hex_val = data.get("value_hex", "?")
        byte_list = data.get("value_bytes", [])
        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in byte_list)
        OutputHelper.print_panel(
            f"  Hex:   [bright_yellow]{hex_val.upper()}[/bright_yellow]\n"
            f"  Bytes: {byte_list}\n"
            f"  ASCII: [dim]{printable}[/dim]",
            title=f"BLE Read  {addr}", border_style="data",
        )
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(f"Read failed: {e}", title="BLE Error", border_style="error")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# ble write
# ---------------------------------------------------------------------------

def _ble_write(client: AgentClient, addr: str, svc_uuid: str, char_uuid: str, hex_value: str):
    addr_bytes_expr, addr_type_expr = _addr_to_parts(addr)
    svc_expr = _parse_uuid(svc_uuid)
    char_expr = _parse_uuid(char_uuid)
    bytes_expr = _hex_to_bytes_expr(hex_value)
    code = _AIOBLE_CHECK + f'''
import aioble, asyncio, bluetooth, json

async def main():
    device = aioble.Device({addr_type_expr}, {addr_bytes_expr})
    try:
        async with await device.connect(timeout_ms=5000) as conn:
            svc = await conn.service(bluetooth.UUID({svc_expr}), timeout_ms=3000)
            if svc is None:
                print(json.dumps({{"error": "service not found"}}))
                return
            char = await svc.characteristic(bluetooth.UUID({char_expr}), timeout_ms=3000)
            if char is None:
                print(json.dumps({{"error": "characteristic not found"}}))
                return
            await char.write({bytes_expr}, timeout_ms=3000)
            print(json.dumps({{"ok": True, "written_hex": "{hex_value.upper()}"}}))
    except Exception as e:
        print(json.dumps({{"error": str(e)}}))

asyncio.run(main())
'''
    console = OutputHelper.make_console()
    spinner = Spinner("dots", text=Text(f" Writing to {addr}...", style="bright_cyan"))
    try:
        with Live(spinner, console=console, refresh_per_second=10, transient=True):
            data = _exec(client, code, timeout=15)

        if not _check_aioble(data):
            raise typer.Exit(1)

        if "error" in data:
            OutputHelper.print_panel(
                f"[red]Write failed:[/red] {data['error']}",
                title="BLE Write", border_style="error",
            )
            raise typer.Exit(1)

        OutputHelper.print_panel(
            f"[green]✓ Written[/green]: [bright_yellow]{data.get('written_hex', hex_value.upper())}[/bright_yellow]  →  [bright_cyan]{addr}[/bright_cyan]",
            title="BLE Write", border_style="success",
        )
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(f"Write failed: {e}", title="BLE Error", border_style="error")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# ble notify
# ---------------------------------------------------------------------------

def _ble_notify(client: AgentClient, addr: str, svc_uuid: str, char_uuid: str, count: int, timeout_ms: int):
    addr_bytes_expr, addr_type_expr = _addr_to_parts(addr)
    svc_expr = _parse_uuid(svc_uuid)
    char_expr = _parse_uuid(char_uuid)
    code = _AIOBLE_CHECK + f'''
import aioble, asyncio, bluetooth, json, binascii

async def main():
    device = aioble.Device({addr_type_expr}, {addr_bytes_expr})
    try:
        async with await device.connect(timeout_ms=5000) as conn:
            svc = await conn.service(bluetooth.UUID({svc_expr}), timeout_ms=3000)
            if svc is None:
                print(json.dumps({{"error": "service not found"}}))
                return
            char = await svc.characteristic(bluetooth.UUID({char_expr}), timeout_ms=3000)
            if char is None:
                print(json.dumps({{"error": "characteristic not found"}}))
                return
            await char.subscribe(notify=True, timeout_ms=3000)
            received = 0
            while received < {count}:
                data = await char.notified(timeout_ms={timeout_ms})
                if data is None:
                    print(json.dumps({{"error": "notification timeout"}}))
                    return
                hex_val = binascii.hexlify(data).decode()
                print(json.dumps({{"n": received + 1, "value_hex": hex_val, "bytes": list(data)}}))
                received += 1
    except Exception as e:
        print(json.dumps({{"error": str(e)}}))

asyncio.run(main())
'''
    console = OutputHelper.make_console()
    spinner = Spinner("dots", text=Text(f" Waiting for {count} notification(s) from {addr}...", style="bright_cyan"))

    received_lines: list[str] = []

    try:
        total_timeout = (timeout_ms / 1000) * count + 15
        with Live(spinner, console=console, refresh_per_second=10, transient=True):
            result = client.send_command("exec", code=code, timeout=total_timeout)
        output = result.get("output", "").strip()

        # Check for aioble missing (first line)
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("error") == "aioble not found":
                    _check_aioble(obj)
                    raise typer.Exit(1)
            except (json.JSONDecodeError, ValueError):
                pass
            break

        notifications = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if "error" in obj:
                    OutputHelper.print_panel(
                        f"[red]Notify failed:[/red] {obj['error']}",
                        title="BLE Notify", border_style="error",
                    )
                    raise typer.Exit(1)
                notifications.append(obj)
            except (json.JSONDecodeError, ValueError):
                pass

        if not notifications:
            OutputHelper.print_panel("No notifications received.", title="BLE Notify", border_style="neutral")
            return

        lines_out = []
        for n in notifications:
            hex_val = n.get("value_hex", "?").upper()
            byte_list = n.get("bytes", [])
            printable = "".join(chr(b) if 32 <= b < 127 else "." for b in byte_list)
            lines_out.append(f"  [{n.get('n')}] [bright_yellow]{hex_val}[/bright_yellow]  [dim]{printable}[/dim]")

        OutputHelper.print_panel(
            "\n".join(lines_out),
            title=f"BLE Notify  {addr}  ({len(notifications)} received)",
            border_style="data",
        )
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(f"Notify failed: {e}", title="BLE Error", border_style="error")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# ble stream send
# ---------------------------------------------------------------------------

def _ble_stream_send(client: AgentClient, addr: str, psm: str, hex_value: str):
    addr_bytes_expr, addr_type_expr = _addr_to_parts(addr)
    psm_val = int(psm, 16) if psm.lower().startswith("0x") else int(psm)
    bytes_expr = _hex_to_bytes_expr(hex_value)
    code = _AIOBLE_CHECK + f'''
import aioble, aioble.l2cap, asyncio, bluetooth, json

async def main():
    device = aioble.Device({addr_type_expr}, {addr_bytes_expr})
    try:
        async with await device.connect(timeout_ms=5000) as conn:
            channel = await aioble.l2cap.connect(conn, {psm_val}, mtu=128, timeout_ms=5000)
            async with channel:
                buf = {bytes_expr}
                await channel.send(buf, timeout_ms=5000)
                print(json.dumps({{"ok": True, "sent_bytes": len(buf), "hex": "{hex_value.upper()}"}}))
    except Exception as e:
        print(json.dumps({{"error": str(e)}}))

asyncio.run(main())
'''
    console = OutputHelper.make_console()
    spinner = Spinner("dots", text=Text(f" L2CAP sending to {addr} (PSM={psm})...", style="bright_cyan"))
    try:
        with Live(spinner, console=console, refresh_per_second=10, transient=True):
            data = _exec(client, code, timeout=20)

        if not _check_aioble(data):
            raise typer.Exit(1)

        if "error" in data:
            OutputHelper.print_panel(
                f"[red]Stream send failed:[/red] {data['error']}",
                title="BLE Stream", border_style="error",
            )
            raise typer.Exit(1)

        OutputHelper.print_panel(
            f"[green]✓ Sent[/green] {data.get('sent_bytes')} bytes: "
            f"[bright_yellow]{data.get('hex')}[/bright_yellow]  →  [bright_cyan]{addr}[/bright_cyan] (PSM={psm})",
            title="BLE Stream Send", border_style="success",
        )
    except typer.Exit:
        raise
    except Exception as e:
        OutputHelper.print_panel(f"Stream send failed: {e}", title="BLE Error", border_style="error")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# ble stream recv
# ---------------------------------------------------------------------------

def _ble_stream_recv(client: AgentClient, psm: str):
    psm_val = int(psm, 16) if psm.lower().startswith("0x") else int(psm)
    code = _AIOBLE_CHECK + f'''
import aioble, aioble.l2cap, asyncio, bluetooth, json, binascii

async def main():
    import json as _json
    print(_json.dumps({{"event": "ready", "psm": {psm_val}}}))
    while True:
        channel = await aioble.l2cap.accept(None, {psm_val}, mtu=128)
        async with channel:
            addr = ":".join("%02X" % b for b in bytes(channel.connection.device.addr))
            print(_json.dumps({{"event": "connected", "addr": addr}}))
            buf = bytearray(256)
            while True:
                try:
                    n = await channel.recvinto(buf, timeout_ms=0)
                    if n == 0:
                        break
                    chunk = bytes(buf[:n])
                    print(_json.dumps({{
                        "event": "data",
                        "addr": addr,
                        "hex": binascii.hexlify(chunk).decode(),
                        "bytes": n,
                    }}))
                except Exception:
                    break
            print(_json.dumps({{"event": "disconnected", "addr": addr}}))

asyncio.run(main())
'''

    console = OutputHelper.make_console()

    def _on_event(data: dict):
        evt_str = data.get("output", "").strip() if isinstance(data, dict) else str(data)
        for line in evt_str.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                event_type = obj.get("event", "")
                if event_type == "ready":
                    msg = f"[green]● L2CAP server ready[/green] — PSM={obj.get('psm')}  (Ctrl+C to stop)"
                elif event_type == "connected":
                    msg = f"  [green]→ Connected[/green]: [bright_cyan]{obj.get('addr')}[/bright_cyan]"
                elif event_type == "data":
                    msg = (f"  [yellow]⇩ Data[/yellow] from [bright_cyan]{obj.get('addr')}[/bright_cyan]: "
                           f"[bright_yellow]{obj.get('hex', '').upper()}[/bright_yellow] "
                           f"([dim]{obj.get('bytes')} bytes[/dim])")
                elif event_type == "disconnected":
                    msg = f"  [dim]← Disconnected[/dim]: {obj.get('addr')} — waiting..."
                else:
                    msg = f"  {line}"
            except (json.JSONDecodeError, ValueError):
                msg = f"  {line}"
            console.print(msg)

    try:
        OutputHelper.print_panel(
            f"Starting L2CAP receiver on PSM={psm}. Press [bold]Ctrl+C[/bold] to stop.",
            title="BLE Stream Recv", border_style="mode",
        )
        client.send_command_streaming("exec", code=code, timeout=86400.0, progress_callback=_on_event)
        OutputHelper.print_panel("L2CAP receiver stopped.", title="BLE Stream Recv", border_style="neutral")
    except KeyboardInterrupt:
        OutputHelper.print_panel("L2CAP receiver stopped by user.", title="BLE Stream Recv", border_style="neutral")
    except Exception as e:
        OutputHelper.print_panel(f"Stream recv failed: {e}", title="BLE Error", border_style="error")
        raise typer.Exit(1)
