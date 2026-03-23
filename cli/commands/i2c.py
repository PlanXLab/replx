import json
import sys
from typing import Optional

import typer

from ..helpers import OutputHelper, CONSOLE_WIDTH
from ..connection import _ensure_connected, _create_agent_client, _get_device_port
from ..app import app

_ADDR_COLORS = [
    'bright_yellow', 'bright_cyan', 'bright_green',
    'bright_magenta', 'bright_red', 'bright_blue',
]

_TEENSY_PIN_TABLE = {
    (18, 19): 0,
    (17, 16): 1,
    (25, 24): 2,
    (38, 37): 3,
}


def _parse_hex(s: str) -> int:
    s = s.strip()
    if s.lower().startswith('0x'):
        return int(s, 16)
    try:
        return int(s, 16)
    except ValueError:
        raise ValueError(f"Invalid hex value: {s!r}")


def _parse_data(tokens: list) -> list:
    return [_parse_hex(t) for t in tokens]


def _parse_gp_pin(token: str, label: str = 'pin') -> int:
    """Parse GP<num> format string into an integer pin number."""
    s = (token or '').strip()
    if len(s) < 3 or s[:2].lower() != 'gp' or not s[2:].isdigit():
        raise ValueError(
            f"Invalid {label}: {token!r}. Use GP<num> format, e.g. GP20"
        )
    pin_no = int(s[2:])
    if pin_no < 0:
        raise ValueError(f"Invalid {label}: {token!r}")
    return pin_no


def _norm_port(port: str) -> str:
    if port and sys.platform.startswith('win'):
        return port.upper()
    return port


def _get_core(client, port: Optional[str]) -> str:
    try:
        info = client.send_command('session_info', timeout=1.5)
        connections = info.get('connections', [])
        if not connections:
            return ''
        if port is None:
            return connections[0].get('core', '')
        norm = _norm_port(port)
        for c in connections:
            cp = _norm_port(c.get('port', ''))
            if cp == norm:
                return c.get('core', '')
    except Exception:
        pass
    return ''


def _validate_core(core: str):
    c = core.upper() if core else ''
    if 'EFR32MG' in c:
        OutputHelper.print_panel(
            "EFR32MG does not support the i2c command.\n"
            "Use umachine.I2C directly in your script.",
            title="I2C Error",
            border_style="red",
        )
        raise typer.Exit(1)


def _resolve_ch(core: str, sda: int, scl: int) -> int:
    c = core.upper() if core else ''
    if 'RP2350' in c or 'RP2040' in c:
        if sda % 4 == 0:
            ch = 0
        elif sda % 4 == 2:
            ch = 1
        else:
            raise ValueError(
                f"GP{sda} is not a valid I2C SDA pin on {core}.\n"
                f"Valid SDA pins: GP0,GP2,GP4,GP6,GP8,GP10,GP12,GP14,GP16,GP18,GP20,GP22,GP24,GP26,GP28"
            )
        if scl != sda + 1:
            raise ValueError(
                f"SCL must be SDA+1 on {core}. For SDA=GP{sda}, SCL must be GP{sda + 1}."
            )
        return ch
    if 'MIMXRT1062' in c:
        key = (sda, scl)
        if key not in _TEENSY_PIN_TABLE:
            valid = ', '.join(f"SDA=GP{k[0]}/SCL=GP{k[1]}" for k in _TEENSY_PIN_TABLE)
            raise ValueError(
                f"SDA=GP{sda}/SCL=GP{scl} is not a valid I2C pin pair on Teensy 4.1.\n"
                f"Valid pairs: {valid}"
            )
        return _TEENSY_PIN_TABLE[key]
    return 0


def _make_init(ch: int, sda: int, scl: int, freq: int) -> str:
    return (
        f"from machine import I2C,Pin\n"
        f"i2c=I2C({ch},sda=Pin({sda}),scl=Pin({scl}),freq={freq})"
    )


# ── I2C Target code-gen ────────────────────────────────────────────────────

def _make_target_open_code(ch: int, sda: int, scl: int, addr: int, mem_size: int) -> str:
    return (
        "from machine import I2CTarget, Pin\n"
        "import json\n"
        f"_i2c_target_mem = bytearray({mem_size})\n"
        f"_i2c_target = I2CTarget({ch}, addr={addr}, "
        f"sda=Pin({sda}), scl=Pin({scl}), mem=_i2c_target_mem)\n"
        f"print(json.dumps({{'open': True, 'addr': {addr}, 'mem_size': {mem_size}}}))"
    )


def _make_target_mem_read_code(offset: int, nbytes: int) -> str:
    end = offset + nbytes
    return (
        "import json, binascii\n"
        f"_d = bytes(_i2c_target_mem[{offset}:{end}])\n"
        f"print(json.dumps({{'hex': binascii.hexlify(_d).decode(), "
        f"'offset': {offset}, 'len': len(_d)}}))"
    )


def _make_target_mem_write_code(offset: int, data: list) -> str:
    end = offset + len(data)
    return (
        "import json\n"
        f"_i2c_target_mem[{offset}:{end}] = bytes({data!r})\n"
        f"print(json.dumps({{'ok': True, 'offset': {offset}, 'len': {len(data)}}}))"
    )


def _make_target_close_code() -> str:
    return (
        "import json\n"
        "_i2c_target.deinit()\n"
        "del _i2c_target, _i2c_target_mem\n"
        "print(json.dumps({'closed': True}))"
    )


def _exec(client, code: str, timeout: float = 5.0) -> str:
    result = client.send_command('exec', code=code, timeout=timeout, max_retries=1)
    return (result.get('output') or '').strip()


def _parse_json_strict(raw: str):
    if not raw:
        raise RuntimeError("No output from device")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Device error:\n{raw}")


def _calc_timeout(repeat: int, interval_ms: int, op_ms: int = 300) -> float:
    total = 2.0 + repeat * (interval_ms / 1000.0 + op_ms / 1000.0)
    return min(total, 120.0)


def _bus_config_line(bus: dict) -> str:
    ch = bus.get('ch', 0)
    sda = bus.get('sda', '?')
    scl = bus.get('scl', '?')
    freq = bus.get('freq', 400000)
    return f"SDA={sda}  SCL={scl}  CH={ch}  {freq // 1000}kHz"


def _format_hex_bytes(data: list) -> str:
    return ' '.join(f"{b:02X}" for b in data)


def _format_int16_be(data: list) -> str:
    pairs = len(data) // 2
    vals = []
    for i in range(pairs):
        hi = data[i * 2]
        lo = data[i * 2 + 1]
        v = (hi << 8) | lo
        if v >= 0x8000:
            v -= 0x10000
        vals.append(str(v))
    return '  '.join(vals) + '  (BE)'


def _render_read_block(data: list, label: str = '') -> list:
    lines = []
    prefix = f"  {label}  " if label else "  "
    lines.append(f"{prefix}[cyan]HEX[/cyan]   {_format_hex_bytes(data)}")
    if len(data) >= 2 and len(data) % 2 == 0:
        lines.append(f"{prefix}[dim]INT16[/dim] {_format_int16_be(data)}")
    return lines


def _display_scan(found: list, sda: int, scl: int, ch: int, freq: int):
    COLS = 16
    ROWS = 8

    addr_color = {}
    for i, a in enumerate(found):
        addr_color[a] = _ADDR_COLORS[i % len(_ADDR_COLORS)]

    lines = []
    lines.append(f"[dim]SDA={sda}  SCL={scl}  CH={ch}  {freq // 1000}kHz[/dim]")
    lines.append("")

    hdr = "       "
    for c in range(COLS):
        hdr += f"{c:02X} "
    lines.append("[dim]" + hdr + "[/dim]")
    lines.append("[dim]  " + "\u2500" * 53 + "[/dim]")

    for r in range(ROWS):
        row_str = f"[dim]  {r * COLS:02X}:[/dim]  "
        for c in range(COLS):
            addr = r * COLS + c
            if addr in addr_color:
                col = addr_color[addr]
                row_str += f"[{col}]{addr:02X}[/{col}] "
            else:
                row_str += "[dim]\u00b7[/dim]  "
        lines.append(row_str)

    lines.append("")

    if found:
        parts = []
        for a in found:
            col = addr_color[a]
            parts.append(f"[{col}]0x{a:02X}[/{col}]")
        lines.append("  Discovery " + str(len(found)) + ": " + "  ".join(parts))
    else:
        lines.append("  [dim yellow]No devices found[/dim yellow]")

    OutputHelper.print_panel(
        "\n".join(lines),
        title="I2C Target Scan",
        border_style="cyan",
    )


def _display_bus(bus: dict):
    if not bus:
        OutputHelper.print_panel(
            "[dim]No bus config saved.\n"
            "Run: replx PORT i2c scan --sda SDA --scl SCL[/dim]",
            title="I2C Bus",
            border_style="cyan",
        )
        return
    if bus.get('target'):
        addr = bus.get('addr', 0)
        mem_size = bus.get('mem_size', 128)
        OutputHelper.print_panel(
            f"[bold yellow]Mode: TARGET[/bold yellow]  "
            f"I2C Addr: [bright_yellow]0x{addr:02X}[/bright_yellow]\n"
            f"{_bus_config_line(bus)}\n"
            f"Mem: [bright_cyan]{mem_size}[/bright_cyan] bytes",
            title="I2C Bus (Target)",
            border_style="yellow",
        )
    else:
        OutputHelper.print_panel(
            _bus_config_line(bus),
            title="I2C Bus",
            border_style="cyan",
        )


def _display_read(raw_output: str, addr: int, nbytes: int, reg: Optional[int],
                  repeat: int, bus: dict):
    title = f"I2C Read  0x{addr:02X}"
    if repeat > 1:
        title += f"  \u00d7{repeat}"

    reg_str = f"reg 0x{reg:02X}  " if reg is not None else ""
    sub = f"  {reg_str}{nbytes} bytes  [dim]{_bus_config_line(bus)}[/dim]"

    lines_raw = [ln for ln in raw_output.split('\n') if ln.strip()]
    content = [sub, ""]

    for i, line in enumerate(lines_raw):
        try:
            parsed = json.loads(line.strip())
        except Exception:
            content.append(f"  [red]Parse error: {line.strip()}[/red]")
            continue
        if isinstance(parsed, dict) and 'error' in parsed:
            label = f"[{i + 1}/{repeat}]" if repeat > 1 else ""
            content.append(f"  [red]{label} Error: {parsed['error']}[/red]")
        else:
            label = f"[{i + 1}/{repeat}]" if repeat > 1 else ""
            content.extend(_render_read_block(parsed, label))
        if repeat > 1 and i < len(lines_raw) - 1:
            content.append("")

    OutputHelper.print_panel("\n".join(content), title=title, border_style="blue")


def _display_write(raw_output: str, addr: int, data: list, repeat: int, bus: dict):
    title = f"I2C Write  0x{addr:02X}"
    if repeat > 1:
        title += f"  \u00d7{repeat}"

    data_str = _format_hex_bytes(data)
    sub = f"  [{data_str}]  {len(data)} bytes  [dim]{_bus_config_line(bus)}[/dim]"

    lines_raw = [ln for ln in raw_output.split('\n') if ln.strip()]
    content = [sub, ""]

    for i, line in enumerate(lines_raw):
        try:
            parsed = json.loads(line.strip())
        except Exception:
            content.append(f"  [red]Parse error: {line.strip()}[/red]")
            continue
        label = f"[{i + 1}/{repeat}]  " if repeat > 1 else ""
        if isinstance(parsed, dict) and parsed.get('ok'):
            content.append(f"  {label}[green]\u2192 ACK {parsed['ack']} byte(s)[/green]")
        else:
            err = parsed.get('error', 'NACK') if isinstance(parsed, dict) else 'NACK'
            content.append(f"  {label}[red]\u2192 {err}[/red]")

    OutputHelper.print_panel("\n".join(content), title=title, border_style="blue")


def _display_dump(raw_output: str, addr: int, from_r: int, to_r: int,
                  repeat: int, bus: dict):
    title = f"I2C Dump  0x{addr:02X}"
    if repeat > 1:
        title += f"  \u00d7{repeat}"

    sub = f"  reg 0x{from_r:02X}\u20130x{to_r:02X}  [dim]{_bus_config_line(bus)}[/dim]"

    lines_raw = [ln for ln in raw_output.split('\n') if ln.strip()]
    content = [sub]

    COLS = 16
    row_start = (from_r // COLS) * COLS
    row_end_base = (to_r // COLS) * COLS

    def render_reg_grid(reg_data: dict, label: str = '') -> list:
        out = []
        if label:
            out.append(f"\n  [dim]{label}[/dim]")
        out.append("")
        hdr = "  [dim]REG  " + " ".join(f"{c:02X}" for c in range(COLS)) + "[/dim]"
        out.append(hdr)
        out.append("  [dim]" + "\u2500" * (5 + COLS * 3) + "[/dim]")
        for row_base in range(row_start, row_end_base + COLS, COLS):
            row_str = f"  [dim]{row_base:02X}:[/dim]  "
            for c in range(COLS):
                reg = row_base + c
                if reg < from_r or reg > to_r:
                    row_str += "   "
                elif reg not in reg_data:
                    row_str += "   "
                elif reg_data[reg] is None:
                    row_str += "[dim]--[/dim] "
                else:
                    row_str += f"{reg_data[reg]:02X} "
            out.append(row_str)
        return out

    for i, line in enumerate(lines_raw):
        try:
            parsed = json.loads(line.strip())
        except Exception:
            content.append(f"\n  [red]Parse error: {line.strip()}[/red]")
            continue
        if isinstance(parsed, dict) and 'error' in parsed and len(parsed) == 1:
            content.append(f"\n  [red]Error: {parsed['error']}[/red]")
            continue
        reg_data = {int(k): v for k, v in parsed.items()}
        label = f"[{i + 1}/{repeat}]" if repeat > 1 else ""
        content.extend(render_reg_grid(reg_data, label))

    OutputHelper.print_panel("\n".join(content), title=title, border_style="blue")


def _get_bus(client) -> dict:
    bus = client.send_command('i2c_bus_get')
    if not bus:
        OutputHelper.print_panel(
            "No bus config found. Scan first:\n\n"
            "  replx PORT i2c scan --sda [yellow]GP<num>[/yellow] --scl [yellow]GP<num>[/yellow]",
            title="I2C Error",
            border_style="red",
        )
        raise typer.Exit(1)
    return bus


def _subcmd_scan(client, core: str, sda: Optional[str], scl: Optional[str], freq: int):
    explicit_pins = sda is not None and scl is not None

    if not explicit_pins:
        bus = client.send_command('i2c_bus_get')
        if not bus:
            OutputHelper.print_panel(
                "No bus config saved. Specify pins for the first scan:\n\n"
                "[bold cyan]Usage:[/bold cyan]\n"
                "  replx PORT i2c scan --sda [yellow]GP<num>[/yellow] --scl [yellow]GP<num>[/yellow]"
                " [--freq HZ]",
                title="I2C Error",
                border_style="red",
            )
            raise typer.Exit(1)
        sda_no, scl_no, ch, freq = bus['sda'], bus['scl'], bus['ch'], bus['freq']
    else:
        try:
            sda_no = _parse_gp_pin(sda, 'SDA')
            scl_no = _parse_gp_pin(scl, 'SCL')
            ch = _resolve_ch(core, sda_no, scl_no)
        except ValueError as e:
            OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
            raise typer.Exit(1)

    init = _make_init(ch, sda_no, scl_no, freq)
    code = (
        f"import json\n{init}\n"
        f"r=[]\n"
        f"for a in range(8,120):\n"
        f" try:i2c.writeto(a,b'');r.append(a)\n"
        f" except OSError:pass\n"
        f"print(json.dumps(r))"
    )

    raw = _exec(client, code, timeout=15.0)

    try:
        found = _parse_json_strict(raw)
        if not isinstance(found, list):
            raise ValueError("Unexpected result type")
    except Exception as e:
        OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
        raise typer.Exit(1)

    _display_scan(found, sda_no, scl_no, ch, freq)

    if explicit_pins:
        client.send_command('i2c_bus_set', sda=sda_no, scl=scl_no, ch=ch, freq=freq)


def _subcmd_bus(client):
    bus = client.send_command('i2c_bus_get')
    _display_bus(bus)


def _parse_seq_tokens(pos_args: list) -> tuple:
    if not pos_args:
        raise ValueError("Missing ADDR")
    addr = _parse_hex(pos_args[0])
    ops = []
    for tok in pos_args[1:]:
        t = tok.lower()
        if t.startswith('u'):
            ops.append(('us', int(t[1:])))
        elif t.startswith('m'):
            ops.append(('ms', int(t[1:])))
        else:
            ops.append(('w', _parse_hex(tok)))
    return addr, ops


def _subcmd_seq(client, pos_args: list):
    try:
        addr, ops = _parse_seq_tokens(pos_args)
    except ValueError as e:
        OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
        raise typer.Exit(1)

    if not ops:
        OutputHelper.print_panel(
            "No operations specified.",
            title="I2C Error", border_style="red",
        )
        raise typer.Exit(1)

    bus = _get_bus(client)
    ch, sda, scl, freq = bus['ch'], bus['sda'], bus['scl'], bus['freq']
    init = _make_init(ch, sda, scl, freq)

    lines = [f"import time\n{init}"]
    writes = 0
    for kind, val in ops:
        if kind == 'w':
            lines.append(f"i2c.writeto({hex(addr)},bytes([{hex(val)}]))")
            writes += 1
        elif kind == 'us':
            lines.append(f"time.sleep_us({val})")
        elif kind == 'ms':
            lines.append(f"time.sleep_ms({val})")
    lines.append("print('ok')")

    code = '\n'.join(lines)
    op_ms = writes * 5
    raw = _exec(client, code, timeout=max(5.0, op_ms / 1000.0 + 2.0))

    op_log = []
    for kind, val in ops:
        if kind == 'w':
            op_log.append(f"[cyan]{val:02X}[/cyan]")
        elif kind == 'us':
            op_log.append(f"[dim]u{val}[/dim]")
        elif kind == 'ms':
            op_log.append(f"[dim]m{val}[/dim]")

    ok = raw.strip() == 'ok'
    status = "[green]\u2192 ok[/green]" if ok else f"[red]\u2192 {raw.strip()}[/red]"
    content = (
        f"  {writes} write(s)  [dim]{_bus_config_line(bus)}[/dim]\n\n"
        f"  {' '.join(op_log)}\n\n"
        f"  {status}"
    )
    OutputHelper.print_panel(content, title=f"I2C Seq  0x{addr:02X}", border_style="blue")


def _render_mem_table(data: bytes, offset: int, bus: dict) -> list:
    COLS = 16
    addr = bus.get('addr', 0)
    lines = [
        f"  Addr: [bright_yellow]0x{addr:02X}[/bright_yellow]  "
        f"offset 0x{offset:02X}  {len(data)} bytes  "
        f"[dim]{_bus_config_line(bus)}[/dim]",
        "",
        "  [dim]     " + " ".join(f"{c:02X}" for c in range(COLS)) + "[/dim]",
        "  [dim]" + "─" * (5 + COLS * 3) + "[/dim]",
    ]
    for i in range(0, len(data), COLS):
        row = data[i:i + COLS]
        base = offset + i
        row_str = f"  [dim]{base:02X}:[/dim]  " + " ".join(f"{b:02X}" for b in row)
        ascii_str = "  " + "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in row)
        lines.append(row_str + ascii_str)
    return lines


def _target_mem_read(client, pos_args: list, bus: dict) -> None:
    mem_size = int(bus.get('mem_size', 128))
    try:
        offset = _parse_hex(pos_args[0]) if pos_args else 0
        nbytes = int(pos_args[1]) if len(pos_args) >= 2 else mem_size - offset
    except (ValueError, IndexError) as e:
        OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
        raise typer.Exit(1)
    raw = _exec(client, _make_target_mem_read_code(offset, nbytes), timeout=3.0)
    try:
        d = _parse_json_strict(raw)
    except RuntimeError as e:
        OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
        raise typer.Exit(1)
    data = bytes.fromhex(d.get('hex', ''))
    lines = _render_mem_table(data, offset, bus)
    OutputHelper.print_panel("\n".join(lines), title="I2C Target Mem Read", border_style="yellow")


def _target_mem_write(client, pos_args: list, bus: dict) -> None:
    if len(pos_args) < 2:
        OutputHelper.print_panel(
            "Usage: replx PORT i2c write [yellow]OFFSET HEX...[/yellow]\n"
            "[dim](target mode: OFFSET = register address in own mem)[/dim]",
            title="I2C Error", border_style="red",
        )
        raise typer.Exit(1)
    try:
        offset = _parse_hex(pos_args[0])
        data = _parse_data(pos_args[1:])
    except ValueError as e:
        OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
        raise typer.Exit(1)
    raw = _exec(client, _make_target_mem_write_code(offset, data), timeout=3.0)
    try:
        _parse_json_strict(raw)
    except RuntimeError as e:
        OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
        raise typer.Exit(1)
    addr = bus.get('addr', 0)
    OutputHelper.print_panel(
        f"  Addr: [bright_yellow]0x{addr:02X}[/bright_yellow]  "
        f"offset [bright_cyan]0x{offset:02X}[/bright_cyan]  "
        f"[bright_cyan]{len(data)}[/bright_cyan] bytes\n\n"
        f"  [{_format_hex_bytes(data)}]\n"
        f"  [green]\u2192 mem[0x{offset:02X}:0x{offset + len(data):02X}] written[/green]",
        title="I2C Target Mem Write", border_style="yellow",
    )


def _subcmd_read(client, pos_args: list, addr16: bool, repeat: int, interval: int):
    bus = _get_bus(client)
    if bus.get('target'):
        _target_mem_read(client, pos_args, bus)
        return

    if len(pos_args) < 2:
        OutputHelper.print_panel(
            "Missing ADDR NBYTES.\n\n"
            "[bold cyan]Usage:[/bold cyan]\n"
            "  replx PORT i2c read [yellow]ADDR NBYTES[/yellow] [[yellow]REG[/yellow]]",
            title="I2C Error",
            border_style="red",
        )
        raise typer.Exit(1)

    try:
        addr = _parse_hex(pos_args[0])
        nbytes = int(pos_args[1])
        reg = _parse_hex(pos_args[2]) if len(pos_args) >= 3 else None
    except ValueError as e:
        OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
        raise typer.Exit(1)

    ch, sda, scl, freq = bus['ch'], bus['sda'], bus['scl'], bus['freq']
    init = _make_init(ch, sda, scl, freq)
    addrsize = 16 if addr16 else 8

    if reg is None:
        inner = (
            f"  print(json.dumps(list(i2c.readfrom({hex(addr)},{nbytes}))))"
        )
    else:
        inner = (
            f"  print(json.dumps(list("
            f"i2c.readfrom_mem({hex(addr)},{hex(reg)},{nbytes},addrsize={addrsize}))))"
        )

    if repeat == 1:
        code = (
            f"import json\n{init}\n"
            f"try:\n"
            f"{inner}\n"
            f"except OSError as e:\n"
            f" print(json.dumps({{'error':str(e)}}))"
        )
    else:
        code = (
            f"import json,time\n{init}\n"
            f"for _ in range({repeat}):\n"
            f" try:\n"
            f" {inner}\n"
            f" except OSError as e:\n"
            f"  print(json.dumps({{'error':str(e)}}))\n"
            f" time.sleep_ms({interval})"
        )

    timeout = _calc_timeout(repeat, interval, op_ms=200)
    raw = _exec(client, code, timeout=timeout)

    if not raw:
        OutputHelper.print_panel("No output from device", title="I2C Error", border_style="red")
        raise typer.Exit(1)

    _display_read(raw, addr, nbytes, reg, repeat, bus)


def _subcmd_write(client, pos_args: list, addr16: bool, repeat: int, interval: int):
    bus = _get_bus(client)
    if bus.get('target'):
        _target_mem_write(client, pos_args, bus)
        return

    if len(pos_args) < 2:
        OutputHelper.print_panel(
            "Missing ADDR DATA.\n\n"
            "[bold cyan]Usage:[/bold cyan]\n"
            "  replx PORT i2c write [yellow]ADDR DATA...[/yellow]",
            title="I2C Error",
            border_style="red",
        )
        raise typer.Exit(1)

    try:
        addr = _parse_hex(pos_args[0])
        data = _parse_data(pos_args[1:])
    except ValueError as e:
        OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
        raise typer.Exit(1)

    ch, sda, scl, freq = bus['ch'], bus['sda'], bus['scl'], bus['freq']
    init = _make_init(ch, sda, scl, freq)
    data_bytes = "bytes([" + ",".join(hex(b) for b in data) + "])"

    if repeat == 1:
        code = (
            f"import json\n{init}\n"
            f"try:\n"
            f" n=i2c.writeto({hex(addr)},{data_bytes})\n"
            f" print(json.dumps({{'ack':n,'ok':True}}))\n"
            f"except OSError as e:\n"
            f" print(json.dumps({{'ok':False,'error':str(e)}}))"
        )
    else:
        code = (
            f"import json,time\n{init}\n"
            f"for _ in range({repeat}):\n"
            f" try:\n"
            f"  n=i2c.writeto({hex(addr)},{data_bytes})\n"
            f"  print(json.dumps({{'ack':n,'ok':True}}))\n"
            f" except OSError as e:\n"
            f"  print(json.dumps({{'ok':False,'error':str(e)}}))\n"
            f" time.sleep_ms({interval})"
        )

    timeout = _calc_timeout(repeat, interval, op_ms=200)
    raw = _exec(client, code, timeout=timeout)

    if not raw:
        OutputHelper.print_panel("No output from device", title="I2C Error", border_style="red")
        raise typer.Exit(1)

    _display_write(raw, addr, data, repeat, bus)


def _subcmd_dump(client, pos_args: list, addr16: bool, repeat: int, interval: int):
    if not pos_args:
        OutputHelper.print_panel(
            "Missing ADDR.\n\n"
            "[bold cyan]Usage:[/bold cyan]\n"
            "  replx PORT i2c dump [yellow]ADDR[/yellow] [[yellow]FROM[/yellow] [[yellow]TO[/yellow]]]",
            title="I2C Error",
            border_style="red",
        )
        raise typer.Exit(1)

    try:
        addr = _parse_hex(pos_args[0])
        from_r = _parse_hex(pos_args[1]) if len(pos_args) >= 2 else 0x00
        to_r = _parse_hex(pos_args[2]) if len(pos_args) >= 3 else 0x7F
    except ValueError as e:
        OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
        raise typer.Exit(1)

    bus = _get_bus(client)
    ch, sda, scl, freq = bus['ch'], bus['sda'], bus['scl'], bus['freq']
    init = _make_init(ch, sda, scl, freq)
    addrsize = 16 if addr16 else 8

    single_dump = (
        f" r={{}}\n"
        f" for a in range({from_r},{to_r + 1}):\n"
        f"  try:r[a]=i2c.readfrom_mem({hex(addr)},a,1,addrsize={addrsize})[0]\n"
        f"  except OSError:r[a]=None\n"
        f" print(json.dumps(r))"
    )

    if repeat == 1:
        code = f"import json\n{init}\n{single_dump}"
    else:
        code = (
            f"import json,time\n{init}\n"
            f"for _ in range({repeat}):\n"
            f"{single_dump}\n"
            f" time.sleep_ms({interval})"
        )

    op_ms = (to_r - from_r + 1) * 10
    timeout = _calc_timeout(repeat, interval, op_ms=op_ms)
    raw = _exec(client, code, timeout=timeout)

    if not raw:
        OutputHelper.print_panel("No output from device", title="I2C Error", border_style="red")
        raise typer.Exit(1)

    _display_dump(raw, addr, from_r, to_r, repeat, bus)


def _subcmd_open(
    client,
    core: str,
    sda: Optional[str],
    scl: Optional[str],
    freq: int,
    target: bool,
    addr_str: Optional[str],
    mem_size: int,
) -> None:
    if sda is None or scl is None:
        OutputHelper.print_panel(
            "--sda and --scl are required for [green]open[/green].\n\n"
            "[bold cyan]Usage:[/bold cyan]\n"
            "  replx PORT i2c open --sda [yellow]GP<num>[/yellow] --scl [yellow]GP<num>[/yellow]"
            " [[yellow]--freq HZ[/yellow]]\n"
            "  replx PORT i2c open --sda [yellow]GP<num>[/yellow] --scl [yellow]GP<num>[/yellow]"
            " [yellow]--target --addr 0xNN[/yellow] [[yellow]--mem-size N[/yellow]]",
            title="I2C Error",
            border_style="red",
        )
        raise typer.Exit(1)

    try:
        sda_no = _parse_gp_pin(sda, 'SDA')
        scl_no = _parse_gp_pin(scl, 'SCL')
        ch = _resolve_ch(core, sda_no, scl_no)
    except ValueError as e:
        OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
        raise typer.Exit(1)

    if target:
        if 'RP2350' not in core:
            OutputHelper.print_panel(
                f"I2C Target mode requires RP2350. Detected core: [yellow]{core}[/yellow]",
                title="I2C Error",
                border_style="red",
            )
            raise typer.Exit(1)
        if addr_str is None:
            OutputHelper.print_panel(
                "--addr is required for target mode.",
                title="I2C Error",
                border_style="red",
            )
            raise typer.Exit(1)
        try:
            addr = int(addr_str, 16) if addr_str.startswith('0x') or addr_str.startswith('0X') \
                else int(addr_str, 16)
            if not 1 <= addr <= 127:
                raise ValueError(f"I2C address must be 1–127, got {addr}")
        except ValueError as e:
            OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
            raise typer.Exit(1)

        raw = _exec(client, _make_target_open_code(ch, sda_no, scl_no, addr, mem_size), timeout=5.0)
        try:
            _parse_json_strict(raw)
        except RuntimeError as e:
            OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
            raise typer.Exit(1)

        cfg = dict(sda=sda_no, scl=scl_no, ch=ch, freq=freq,
                   target=True, addr=addr, mem_size=mem_size)
        client.send_command('i2c_bus_set', **cfg)
        _display_bus(cfg)
    else:
        cfg = dict(sda=sda_no, scl=scl_no, ch=ch, freq=freq)
        client.send_command('i2c_bus_set', **cfg)
        _display_bus(cfg)


def _subcmd_mem(client, pos_args: list) -> None:
    bus = _get_bus(client)
    if not bus.get('target'):
        OutputHelper.print_panel(
            "[green]mem[/green] subcommand is only available in target mode.\n"
            "Open target first:  replx PORT i2c open ... [yellow]--target --addr 0xNN[/yellow]",
            title="I2C Error",
            border_style="red",
        )
        raise typer.Exit(1)

    mem_size = int(bus.get('mem_size', 128))
    try:
        offset = _parse_hex(pos_args[0]) if pos_args else 0
        nbytes = int(pos_args[1]) if len(pos_args) >= 2 else mem_size - offset
    except (ValueError, IndexError) as e:
        OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
        raise typer.Exit(1)

    raw = _exec(client, _make_target_mem_read_code(offset, nbytes), timeout=3.0)
    try:
        d = _parse_json_strict(raw)
    except RuntimeError as e:
        OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
        raise typer.Exit(1)

    data = bytes.fromhex(d.get('hex', ''))
    lines = _render_mem_table(data, offset, bus)
    OutputHelper.print_panel(
        "\n".join(lines),
        title=f"I2C Target Mem Snapshot  ({mem_size} bytes total)",
        border_style="yellow",
    )


def _subcmd_close(client) -> None:
    bus = _get_bus(client)
    if bus.get('target'):
        raw = _exec(client, _make_target_close_code(), timeout=3.0)
        try:
            _parse_json_strict(raw)
        except RuntimeError as e:
            OutputHelper.print_panel(str(e), title="I2C Error", border_style="red")
            raise typer.Exit(1)
    client.send_command('i2c_bus_clear')
    OutputHelper.print_panel(
        "  I2C bus released. Run [green]scan[/green] or [green]open[/green] to reconfigure.",
        title="I2C Closed",
        border_style="cyan",
    )


def _print_i2c_help():
    help_text = """\
Interact with I2C devices connected to the board without writing a script.

[bold cyan]Usage (Controller mode):[/bold cyan]
  replx PORT i2c scan  --sda [yellow]GP<num>[/yellow] --scl [yellow]GP<num>[/yellow] [[yellow]--freq HZ[/yellow]]
  replx PORT i2c open  --sda [yellow]GP<num>[/yellow] --scl [yellow]GP<num>[/yellow] [[yellow]--freq HZ[/yellow]]
  replx PORT i2c bus
  replx PORT i2c read  [yellow]ADDR NBYTES[/yellow] [[yellow]REG[/yellow]] [[yellow]--addr16[/yellow]] [[yellow]-n N[/yellow] [yellow]--interval MS[/yellow]]
  replx PORT i2c write [yellow]ADDR DATA...[/yellow]      [[yellow]--addr16[/yellow]] [[yellow]-n N[/yellow] [yellow]--interval MS[/yellow]]
  replx PORT i2c dump  [yellow]ADDR[/yellow] [[yellow]FROM[/yellow] [[yellow]TO[/yellow]]]  [[yellow]--addr16[/yellow]] [[yellow]-n N[/yellow] [yellow]--interval MS[/yellow]]
  replx PORT i2c seq   [yellow]ADDR TOKEN...[/yellow]
  replx PORT i2c close

[bold yellow]Usage (Target mode — RP2350 only):[/bold yellow]
  replx PORT i2c open --sda [yellow]GP<num>[/yellow] --scl [yellow]GP<num>[/yellow] [yellow]--target --addr 0xNN[/yellow] [[yellow]--mem-size N[/yellow]]
  replx PORT i2c bus
  replx PORT i2c read  [[yellow]OFFSET[/yellow] [[yellow]NBYTES[/yellow]]]      [dim](read bytes written by controller)[/dim]
  replx PORT i2c write [yellow]OFFSET HEX...[/yellow]           [dim](preload bytes for controller to read)[/dim]
  replx PORT i2c mem   [[yellow]OFFSET[/yellow] [[yellow]NBYTES[/yellow]]]      [dim](full mem snapshot)[/dim]
  replx PORT i2c close

[bold cyan]Subcommands:[/bold cyan]
  [green]scan[/green]   Scan I2C bus for responding devices (controller).
  [green]open[/green]   Configure I2C bus without scanning. --target activates I2CTarget.
  [green]bus[/green]    Show saved bus config [dim](in-memory, cleared on agent restart)[/dim].
  [green]read[/green]   Controller: read bytes from device. Target: read own mem region.
  [green]write[/green]  Controller: write bytes to device. Target: write own mem region.
  [green]dump[/green]   Dump device register range (controller only).
  [green]seq[/green]    Execute a write+delay sequence in a single board call (controller).
         TOKEN: hex byte [dim](e.g. 3C)[/dim]  u<N> [dim]= sleep_us[/dim]  m<N> [dim]= sleep_ms[/dim]
  [green]mem[/green]    Snapshot entire target mem buffer (target mode only).
  [green]close[/green]  Release I2C peripheral and clear bus config.

[bold cyan]Options:[/bold cyan]
  [yellow]--sda GP<num>[/yellow]     SDA GPIO pin [red](open/scan: required)[/red]  e.g. [cyan]GP12[/cyan]
  [yellow]--scl GP<num>[/yellow]     SCL GPIO pin [red](open/scan: required)[/red]  e.g. [cyan]GP13[/cyan]
  [yellow]--freq HZ[/yellow]         I2C clock frequency Hz [dim](default: 400000)[/dim]
  [yellow]--target[/yellow]          Activate I2CTarget (RP2350 only)
  [yellow]--addr 0xNN[/yellow]       7-bit I2C address for target mode [dim](1-127)[/dim]
  [yellow]--mem-size N[/yellow]      Target mem buffer size bytes [dim](default: 256)[/dim]
  [yellow]--addr16[/yellow]          16-bit register addresses [dim](default: 8-bit)[/dim]
  [yellow]-n, --repeat N[/yellow]    Repeat N times, N≥1 [dim](default: 1)[/dim]
  [yellow]--interval MS[/yellow]     Delay between repeats ms [dim](default: 1000, requires -n≥2)[/dim]

[bold cyan]Arguments (controller mode):[/bold cyan]
  [yellow]ADDR[/yellow]      Device address  [dim]hex, 0x optional · e.g.[/dim] [cyan]68[/cyan] [cyan]0x68[/cyan]
  [yellow]NBYTES[/yellow]    Bytes to read   [dim]decimal           · e.g.[/dim] [cyan]6[/cyan]
  [yellow]REG[/yellow]       Register addr   [dim]hex, 0x optional · e.g.[/dim] [cyan]3B[/cyan] [cyan]0x3B[/cyan]
  [yellow]DATA[/yellow]      Bytes to write  [dim]hex space-sep     · e.g.[/dim] [cyan]6B 00[/cyan]
  [yellow]FROM TO[/yellow]   Dump range      [dim]hex, default[/dim] [cyan]00[/cyan][dim]–[/dim][cyan]7F[/cyan]

[bold cyan]Arguments (target mode):[/bold cyan]
  [yellow]OFFSET[/yellow]    Offset into target mem buffer  [dim]hex, 0x optional[/dim]
  [yellow]NBYTES[/yellow]    Bytes to read/write            [dim]decimal[/dim]
  [yellow]HEX...[/yellow]    Bytes to write to mem          [dim]hex space-sep  · e.g.[/dim] [cyan]DE AD BE EF[/cyan]

[bold cyan]Bus config:[/bold cyan]
  Saved in agent memory on successful scan/open. Read/write/dump rely on it.
  Cleared only when the agent restarts or [green]close[/green] is called.

[bold cyan]Board support:[/bold cyan]
  RP2350    GP%4==0→CH0, GP%4==2→CH1, SCL must equal SDA+1
  Teensy    Fixed Wire pairs: (GP18,GP19)→0  (GP17,GP16)→1  (GP25,GP24)→2  (GP38,GP37)→3
  ESP32     Any GPIO, CH=0 [dim](GPIO matrix)[/dim]
  EFR32MG   [red]Not supported[/red]

[bold cyan]Examples (controller):[/bold cyan]
  [dim]# Scan and save bus config[/dim]
  replx COM3 i2c scan --sda GP12 --scl GP13

  [dim]# Show saved bus config[/dim]
  replx COM3 i2c bus

  [dim]# WHO_AM_I (MPU-6050, reg 0x75, expect 0x68)[/dim]
  replx COM3 i2c read 0x68 1 75

  [dim]# Disable sleep (reg 0x6B = 0x00)[/dim]
  replx COM3 i2c write 0x68 6B 00

  [dim]# Read accelerometer XYZ 6 bytes from reg 0x3B[/dim]
  replx COM3 i2c read 0x68 6 3B

  [dim]# Repeat 5 times every 500ms[/dim]
  replx COM3 i2c read 0x68 6 3B -n 5 --interval 500

  [dim]# Dump all registers (0x00–0x7F)[/dim]
  replx COM3 i2c dump 0x68

  [dim]# PCF8574 LCD: pulse(0x30)×3 → 4-bit mode[/dim]
  replx COM3 i2c seq 27  3C u500 38 u100  m5  3C u500 38 u100  u200  3C u500 38 u100

[bold yellow]Examples (target):[/bold yellow]
  [dim]# Open as I2CTarget at address 0x42 with 256-byte mem[/dim]
  replx COM4 i2c open --sda GP12 --scl GP13 --target --addr 0x42 --mem-size 256

  [dim]# Preload bytes at offset 0x10 (controller will read these)[/dim]
  replx COM4 i2c write 0x10 DE AD BE EF

  [dim]# Inspect what the controller wrote at offset 0x00[/dim]
  replx COM4 i2c read 0x00 16

  [dim]# Full mem snapshot[/dim]
  replx COM4 i2c mem

  [dim]# Release target[/dim]
  replx COM4 i2c close"""
    OutputHelper.print_panel(help_text, title="i2c", border_style="dim")




@app.command(name="i2c", rich_help_panel="Hardware")
def i2c_cmd(
    args: Optional[list[str]] = typer.Argument(
        None, help="Subcommand: scan  open  close  bus  read  write  dump  seq  mem"
    ),
    sda: Optional[str] = typer.Option(None, "--sda", metavar="GP<num>",
                                      help="SDA GPIO pin in GP<num> format (open/scan) e.g. GP20"),
    scl: Optional[str] = typer.Option(None, "--scl", metavar="GP<num>",
                                      help="SCL GPIO pin in GP<num> format (open/scan) e.g. GP21"),
    freq: int = typer.Option(400000, "--freq", metavar="HZ",
                             help="I2C clock frequency in Hz"),
    target: bool = typer.Option(False, "--target",
                                help="Activate I2CTarget mode (RP2350 only, use with open)"),
    addr: Optional[str] = typer.Option(None, "--addr", metavar="0xNN",
                                       help="7-bit I2C address for target mode (1-127)"),
    mem_size: int = typer.Option(256, "--mem-size", metavar="N",
                                 help="Target mem buffer size in bytes (default: 256)"),
    addr16: bool = typer.Option(False, "--addr16",
                                help="Use 16-bit register addresses"),
    repeat: int = typer.Option(1, "--repeat", "-n", metavar="N",
                               help="Repeat N times (N>=1)"),
    interval: int = typer.Option(1000, "--interval", metavar="MS",
                                 help="Interval between repeats in ms"),
    show_help: bool = typer.Option(False, "--help", "-h",
                                   is_eager=True, hidden=True),
):
    if show_help or not args:
        _print_i2c_help()
        raise typer.Exit()

    subcmd = args[0].lower()
    pos_args = args[1:]

    _VALID = ('scan', 'open', 'close', 'bus', 'read', 'write', 'dump', 'seq', 'mem')
    if subcmd not in _VALID:
        OutputHelper.print_panel(
            f"Unknown subcommand: {subcmd!r}\n\n"
            "Valid subcommands: " + "  ".join(_VALID),
            title="I2C Error",
            border_style="red",
        )
        raise typer.Exit(1)

    if subcmd not in ('scan', 'open', 'close', 'bus') and repeat < 1:
        OutputHelper.print_panel(
            "--repeat must be >= 1",
            title="I2C Error",
            border_style="red",
        )
        raise typer.Exit(1)

    _ensure_connected()

    with _create_agent_client() as client:
        port = _get_device_port()

        if subcmd == 'bus':
            _subcmd_bus(client)
            return

        if subcmd == 'close':
            _subcmd_close(client)
            return

        core = _get_core(client, port)
        _validate_core(core)

        if subcmd == 'scan':
            _subcmd_scan(client, core, sda, scl, freq)
        elif subcmd == 'open':
            _subcmd_open(client, core, sda, scl, freq, target, addr, mem_size)
        elif subcmd == 'read':
            _subcmd_read(client, pos_args, addr16, repeat, interval)
        elif subcmd == 'write':
            _subcmd_write(client, pos_args, addr16, repeat, interval)
        elif subcmd == 'dump':
            _subcmd_dump(client, pos_args, addr16, repeat, interval)
        elif subcmd == 'seq':
            _subcmd_seq(client, pos_args)
        elif subcmd == 'mem':
            _subcmd_mem(client, pos_args)
