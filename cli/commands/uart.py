import codecs
import datetime
import json
import os
import signal
import sys
from typing import Optional

import typer

from replx.utils.constants import CTRL_C
from ..helpers import OutputHelper
from ..connection import _ensure_connected, _create_agent_client, _get_device_port
from ..app import app


_RP_UART_TX_CH: dict[int, int] = {
    0: 0, 12: 0, 16: 0, 28: 0,
    4: 1,  8: 1, 20: 1, 24: 1,
}

_TEENSY_UART_TX_CH: dict[int, int] = {
    1: 1, 8: 2, 14: 3, 17: 4, 20: 5, 24: 6, 28: 7, 34: 8,
}

_ESC = '\x1b'
_DIM = _ESC + '[2m'
_RESET = _ESC + '[0m'
_CYAN = _ESC + '[38;2;84;208;216m'

def _cursor_up(n: int = 1) -> str:
    return f'{_ESC}[{n}A'

def _clear_line() -> str:
    return _ESC + '[G' + _ESC + '[2K'


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
            if _norm_port(c.get('port', '')) == norm:
                return c.get('core', '')
    except Exception:
        pass
    return ''


def _parse_gp_pin(token: str, label: str = 'pin') -> int:
    s = (token or '').strip()
    if len(s) < 3 or s[:2].lower() != 'gp' or not s[2:].isdigit():
        raise ValueError(f"Invalid {label}: {token!r}. Use GP<num> format, e.g. GP0")
    pin_no = int(s[2:])
    if pin_no < 0:
        raise ValueError(f"Invalid {label}: {token!r}")
    return pin_no


def _resolve_uart_ch(core: str, tx_no: int) -> int:
    c = (core or '').upper()
    if 'RP2350' in c or 'RP2040' in c:
        if tx_no not in _RP_UART_TX_CH:
            valid = ', '.join(f"GP{p}" for p in sorted(_RP_UART_TX_CH))
            raise ValueError(
                f"GP{tx_no} is not a valid UART TX pin on {core}.\n"
                f"Valid TX pins: {valid}"
            )
        return _RP_UART_TX_CH[tx_no]
    if 'MIMXRT1062' in c:
        if tx_no not in _TEENSY_UART_TX_CH:
            valid = ', '.join(f"GP{p}" for p in sorted(_TEENSY_UART_TX_CH))
            raise ValueError(
                f"GP{tx_no} is not a valid UART TX pin on Teensy 4.x.\n"
                f"Valid TX pins: {valid}"
            )
        return _TEENSY_UART_TX_CH[tx_no]
    return 1


_PARITY_VALUES = {'none', 'odd', 'even'}
_STOP_VALUES = {1, 2}

def _parity_val(parity: str) -> str:
    if parity == 'none':
        return 'None'
    if parity == 'odd':
        return '1'
    if parity == 'even':
        return '0'
    raise ValueError(f"Invalid parity: {parity!r}. Use none, odd, even")


def _make_uart_init(cfg: dict) -> str:
    ch = cfg['ch']
    tx = cfg['tx']
    rx = cfg.get('rx')
    baud = cfg['baud']
    bits = cfg['bits']
    parity = _parity_val(cfg['parity'])
    stop = cfg['stop']
    timeout = cfg.get('timeout_ms', 1000)
    if rx is not None:
        return (
            f"from machine import UART,Pin\n"
            f"u=UART({ch},baudrate={baud},tx=Pin({tx}),rx=Pin({rx}),"
            f"bits={bits},parity={parity},stop={stop},timeout={timeout})"
        )
    return (
        f"from machine import UART,Pin\n"
        f"u=UART({ch},baudrate={baud},tx=Pin({tx}),"
        f"bits={bits},parity={parity},stop={stop},timeout={timeout})"
    )


def _make_open_code(cfg: dict) -> str:
    init = _make_uart_init(cfg)
    return (
        f"import json\n{init}\n"
        f"u.deinit()\n"
        f"print(json.dumps({{'ok':True,'ch':{cfg['ch']},"
        f"'tx':{cfg['tx']},'rx':{cfg.get('rx')!r},"
        f"'baud':{cfg['baud']},'bits':{cfg['bits']},"
        f"'parity':{cfg['parity']!r},'stop':{cfg['stop']},"
        f"'timeout_ms':{cfg.get('timeout_ms', 1000)}}}));"
    )


def _make_write_code(cfg: dict, data: list[int]) -> str:
    init = _make_uart_init(cfg)
    data_str = "bytes([" + ",".join(hex(b) for b in data) + "])"
    return (
        f"import json\n{init}\n"
        f"n=u.write({data_str})\n"
        f"u.deinit()\n"
        f"print(json.dumps({{'written':n}}))"
    )


def _make_read_code(cfg: dict, nbytes: Optional[int], any_mode: bool, wait_ms: int) -> str:
    init = _make_uart_init(cfg)
    if any_mode:
        read_block = "data=u.read(u.any()) if u.any() else b''"
    elif nbytes is not None:
        if wait_ms == 0:
            read_block = (
                f"buf=bytearray()\n"
                f"while len(buf)<{nbytes}:\n"
                f"  n=u.any()\n"
                f"  if n>0: buf+=u.read(min(n,{nbytes}-len(buf)))\n"
                f"  else: time.sleep_ms(5)\n"
                f"data=bytes(buf)"
            )
        else:
            read_block = (
                f"buf=bytearray()\n"
                f"_dl=time.ticks_ms()+{wait_ms}\n"
                f"while len(buf)<{nbytes} and time.ticks_diff(_dl,time.ticks_ms())>0:\n"
                f"  n=u.any()\n"
                f"  if n>0: buf+=u.read(min(n,{nbytes}-len(buf)))\n"
                f"  else: time.sleep_ms(5)\n"
                f"data=bytes(buf)"
            )
    else:
        if wait_ms == 0:
            read_block = (
                f"buf=bytearray()\n"
                f"while True:\n"
                f"  if u.any(): break\n"
                f"  time.sleep_ms(5)\n"
                f"_idle=time.ticks_ms()+50\n"
                f"while True:\n"
                f"  n=u.any()\n"
                f"  if n>0: buf+=u.read(n); _idle=time.ticks_ms()+50\n"
                f"  elif time.ticks_diff(_idle,time.ticks_ms())<=0: break\n"
                f"  else: time.sleep_ms(5)\n"
                f"data=bytes(buf)"
            )
        else:
            read_block = (
                f"buf=bytearray()\n"
                f"_dl=time.ticks_ms()+{wait_ms}\n"
                f"while time.ticks_diff(_dl,time.ticks_ms())>0:\n"
                f"  if u.any(): break\n"
                f"  time.sleep_ms(5)\n"
                f"if u.any():\n"
                f"  _idle=time.ticks_ms()+50\n"
                f"  while time.ticks_diff(_dl,time.ticks_ms())>0:\n"
                f"    n=u.any()\n"
                f"    if n>0: buf+=u.read(n); _idle=time.ticks_ms()+50\n"
                f"    elif time.ticks_diff(_idle,time.ticks_ms())<=0: break\n"
                f"    else: time.sleep_ms(5)\n"
                f"data=bytes(buf)"
            )
    return (
        f"import json,binascii,time\n{init}\n"
        f"{read_block}\n"
        f"u.deinit()\n"
        r"print(json.dumps({'hex':binascii.hexlify(data).decode() if data else '','len':len(data) if data else 0}))"
    )


def _make_xfer_code(cfg: dict, tx_data: list[int], rx_nbytes: Optional[int], wait_ms: int) -> str:
    init = _make_uart_init(cfg)
    data_str = "bytes([" + ",".join(hex(b) for b in tx_data) + "])"
    if rx_nbytes is None:
        if wait_ms == 0:
            read_block = (
                f"buf=bytearray()\n"
                f"while True:\n"
                f"  if u.any(): break\n"
                f"  time.sleep_ms(5)\n"
                f"_idle=time.ticks_ms()+50\n"
                f"while True:\n"
                f"  n=u.any()\n"
                f"  if n>0: buf+=u.read(n); _idle=time.ticks_ms()+50\n"
                f"  elif time.ticks_diff(_idle,time.ticks_ms())<=0: break\n"
                f"  else: time.sleep_ms(5)\n"
                f"data=bytes(buf)"
            )
        else:
            read_block = (
                f"buf=bytearray()\n"
                f"_dl=time.ticks_ms()+{wait_ms}\n"
                f"while time.ticks_diff(_dl,time.ticks_ms())>0:\n"
                f"  if u.any(): break\n"
                f"  time.sleep_ms(5)\n"
                f"if u.any():\n"
                f"  _idle=time.ticks_ms()+50\n"
                f"  while time.ticks_diff(_dl,time.ticks_ms())>0:\n"
                f"    n=u.any()\n"
                f"    if n>0: buf+=u.read(n); _idle=time.ticks_ms()+50\n"
                f"    elif time.ticks_diff(_idle,time.ticks_ms())<=0: break\n"
                f"    else: time.sleep_ms(5)\n"
                f"data=bytes(buf)"
            )
    elif wait_ms == 0:
        read_block = (
            f"buf=bytearray()\n"
            f"while len(buf)<{rx_nbytes}:\n"
            f"  n=u.any()\n"
            f"  if n>0: buf+=u.read(min(n,{rx_nbytes}-len(buf)))\n"
            f"  else: time.sleep_ms(5)\n"
            f"data=bytes(buf)"
        )
    else:
        read_block = (
            f"buf=bytearray()\n"
            f"_dl=time.ticks_ms()+{wait_ms}\n"
            f"while len(buf)<{rx_nbytes} and time.ticks_diff(_dl,time.ticks_ms())>0:\n"
            f"  n=u.any()\n"
            f"  if n>0: buf+=u.read(min(n,{rx_nbytes}-len(buf)))\n"
            f"  else: time.sleep_ms(5)\n"
            f"data=bytes(buf)"
        )
    return (
        f"import json,binascii,time\n{init}\n"
        f"u.write({data_str})\n"
        f"{read_block}\n"
        f"u.deinit()\n"
        r"print(json.dumps({'hex':binascii.hexlify(data).decode() if data else '','len':len(data) if data else 0,'written':"
        + str(len(tx_data))
        + r"}))"
    )


def _make_monitor_code(cfg: dict, idle_ms: int, text_mode: bool) -> str:
    init = _make_uart_init({**cfg, 'timeout_ms': 10})
    output_stmt = "            print(binascii.hexlify(data).decode())\n"

    if idle_ms > 0:
        data_block = (
            f"        if data:\n"
            f"            now=time.ticks_ms()\n"
            f"            if last_t is not None and time.ticks_diff(now,last_t)>={idle_ms}:\n"
            f"                print('__IDLE__')\n"
            f"            last_t=now\n"
            + output_stmt
        )
        return (
            f"import binascii,time\n{init}\n"
            f"last_t=None\n"
            f"while True:\n"
            f"    n=u.any()\n"
            f"    if n>0:\n"
            f"        data=u.read(n)\n"
            + data_block +
            f"    else:\n"
            f"        time.sleep_ms(1)\n"
        )
    else:
        data_block = f"        if data:\n" + output_stmt
        return (
            f"import binascii,time\n{init}\n"
            f"while True:\n"
            f"    n=u.any()\n"
            f"    if n>0:\n"
            f"        data=u.read(n)\n"
            + data_block +
            f"    else:\n"
            f"        time.sleep_ms(1)\n"
        )


def _ascii_char(b: int) -> str:
    if 0x20 <= b <= 0x7E:
        return chr(b)
    return '.'


def _render_hex_row(data: bytes, offset: int, width: int) -> str:
    half = width // 2
    if len(data) > half:
        left = ' '.join(f"{b:02X}" for b in data[:half])
        right = ' '.join(f"{b:02X}" for b in data[half:])
        hex_str = left + '  ' + right
    else:
        hex_str = ' '.join(f"{b:02X}" for b in data)

    full_width = (half * 3 - 1) + 2 + (half * 3 - 1)
    hex_padded = hex_str.ljust(full_width)

    ascii_str = ''.join(_ascii_char(b) for b in data).ljust(width)
    return (
        f"[dim]{offset:06X}[/dim]  "
        f"[bright_cyan]{hex_padded}[/bright_cyan]  "
        f"[dim]{ascii_str}[/dim]"
    )


def _render_hex_dump(data: bytes, width: int = 16) -> str:
    if not data:
        return "[dim](no data)[/dim]"
    lines = []
    half = width // 2
    header_hex = (
        ' '.join(f"{i:02X}" for i in range(half))
        + '  '
        + ' '.join(f"{i:02X}" for i in range(half, width))
    )
    full_width = (half * 3 - 1) + 2 + (half * 3 - 1)
    lines.append(
        f"[dim]Offset  {header_hex}  ASCII[/dim]"
    )
    lines.append("[dim]" + "─" * (8 + full_width + 2 + width) + "[/dim]")
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        lines.append(_render_hex_row(chunk, off, width))
    return '\n'.join(lines)


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


def _render_row_term(data: bytes, offset_val: int, width: int) -> str:
    half = width // 2
    cells = []
    for i in range(width):
        if i < len(data):
            cells.append(_CYAN + f"{data[i]:02X}" + _RESET)
        else:
            cells.append('  ')
    hex_str = ' '.join(cells[:half]) + '  ' + ' '.join(cells[half:])
    ascii_str = ''.join(
        (chr(b) if 0x20 <= b <= 0x7E else '.') if i < len(data) else ' '
        for i, b in enumerate(data if len(data) <= width
                               else data[:width])
    )
    if len(data) < width:
        ascii_str = ascii_str + ' ' * (width - len(data))
    return (
        _DIM + f"{offset_val:06X}" + _RESET + '  '
        + hex_str + '  '
        + _DIM + ascii_str + _RESET
    )


def _run_monitor_session(client, code: str, width: int, idle_ms: int, text_mode: bool, chunk_mode: bool) -> None:
    w = sys.stdout.write
    flush = sys.stdout.flush
    half = width // 2

    buf = bytearray()
    offset = [0]
    partial_on_screen = [False]
    line_buf = ['']
    utf8_decoder = codecs.getincrementaldecoder('utf-8')('replace')
    stop_requested = False
    pending_input: list[bytes] = []
    original_sigint = signal.getsignal(signal.SIGINT)

    def _now_str() -> str:
        n = datetime.datetime.now()
        return n.strftime('%H:%M:%S.') + f'{n.microsecond // 1000:03d}'

    def _print_chunk_header(nbytes: int):
        w(_DIM + f'{_now_str()}  {nbytes} bytes' + _RESET + '\n')

    def _print_chunk_hex(chunk: bytes):
        full_w = (half * 3 - 1) + 2 + (half * 3 - 1)
        sep_w = 8 + full_w + 2 + width
        for off in range(0, len(chunk), width):
            w(_render_row_term(chunk[off:off + width], off, width) + '\n')
        w(_DIM + '─' * sep_w + _RESET + '\n')
        flush()

    def _erase_partial():
        if partial_on_screen[0]:
            w(_cursor_up(1) + _clear_line())
            partial_on_screen[0] = False

    def _commit_partial():
        if partial_on_screen[0] and buf:
            offset[0] += len(buf)
            buf.clear()
            partial_on_screen[0] = False

    def _emit(data: bytes, final: bool):
        w(_render_row_term(data, offset[0], width) + '\n')
        flush()
        if final:
            offset[0] += len(data)
            partial_on_screen[0] = False
        else:
            partial_on_screen[0] = True

    def feed_chunk(chunk: bytes):
        buf.extend(chunk)
        _erase_partial()
        while len(buf) >= width:
            row = bytes(buf[:width])
            del buf[:width]
            _emit(row, final=True)
        if buf:
            _emit(bytes(buf), final=False)

    def handle_hex_line(line: str):
        try:
            chunk = bytes.fromhex(line)
        except ValueError:
            return
        if chunk:
            feed_chunk(chunk)

    def flush_idle(label: str):
        _commit_partial()
        w(_DIM + f'── {label} ──' + _RESET + '\n')
        flush()

    def output_callback(data: bytes, stream_type: str = 'stdout'):
        if stream_type == 'stderr':
            return
        if text_mode:
            line_buf[0] += data.decode('ascii', errors='replace')
            while '\n' in line_buf[0]:
                line, line_buf[0] = line_buf[0].split('\n', 1)
                line = line.strip()
                if not line or line == '__IDLE__':
                    if line == '__IDLE__':
                        flush_idle(f'idle {idle_ms}ms')
                    continue
                try:
                    raw = bytes.fromhex(line)
                except ValueError:
                    continue
                text = utf8_decoder.decode(raw)
                if not text:
                    continue
                if chunk_mode:
                    _print_chunk_header(len(raw))
                    safe = ''.join(c if (c.isprintable() or c == '\n') else '.' for c in text)
                    w(safe)
                    if not safe.endswith('\n'):
                        w('\n')
                    w(_DIM + '─' * 40 + _RESET + '\n')
                    flush()
                else:
                    safe = ''.join(c if (c.isprintable() or c == '\n') else '.' for c in text)
                    w(safe)
                    flush()
            return
        line_buf[0] += data.decode('utf-8', errors='replace')
        while '\n' in line_buf[0]:
            line, line_buf[0] = line_buf[0].split('\n', 1)
            line = line.strip()
            if not line:
                continue
            if line == '__IDLE__':
                flush_idle(f"idle {idle_ms}ms")
                continue
            if chunk_mode:
                try:
                    chunk = bytes.fromhex(line)
                except ValueError:
                    continue
                if chunk:
                    _print_chunk_header(len(chunk))
                    _print_chunk_hex(chunk)
            else:
                handle_hex_line(line)

    def input_provider() -> bytes:
        if pending_input:
            return pending_input.pop(0)
        return b''

    def stop_check() -> bool:
        return stop_requested

    def sigint_handler(signum, frame):
        nonlocal stop_requested
        stop_requested = True
        pending_input.append(CTRL_C)

    if not text_mode and not chunk_mode:
        hdr = (
            ' '.join(f"{i:02X}" for i in range(half))
            + '  '
            + ' '.join(f"{i:02X}" for i in range(half, width))
        )
        full_width = (half * 3 - 1) + 2 + (half * 3 - 1)
        w(_DIM + f"Offset  {hdr}  ASCII" + _RESET + '\n')
        w(_DIM + '─' * (8 + full_width + 2 + width) + _RESET + '\n')
        flush()

    try:
        signal.signal(signal.SIGINT, sigint_handler)
        client.run_interactive(
            script_content=code,
            echo=False,
            output_callback=output_callback,
            input_provider=input_provider,
            stop_check=stop_check,
        )
    finally:
        signal.signal(signal.SIGINT, original_sigint)
        if not text_mode and not chunk_mode:
            _commit_partial()


def _display_bus(bus: dict) -> None:
    rx_str = f"GP{bus['rx']}" if bus.get('rx') is not None else "[dim]none[/dim]"
    parity = bus.get('parity', 'none')
    OutputHelper.print_panel(
        f"CH: [bright_cyan]{bus['ch']}[/bright_cyan]\n"
        f"TX: [bright_green]GP{bus['tx']}[/bright_green]  "
        f"RX: [bright_green]{rx_str}[/bright_green]\n"
        f"Baud: [bright_cyan]{bus['baud']}[/bright_cyan]  "
        f"Bits: [bright_cyan]{bus['bits']}[/bright_cyan]  "
        f"Parity: [bright_cyan]{parity}[/bright_cyan]  "
        f"Stop: [bright_cyan]{bus['stop']}[/bright_cyan]",
        title="UART Bus",
        border_style="cyan",
    )


def _get_bus(client) -> dict:
    bus = client.send_command('uart_bus_get')
    if not bus:
        OutputHelper.print_panel(
            "No UART bus config. Open first:\n\n"
            "  replx PORT uart open --tx [yellow]GP<num>[/yellow] [--rx [yellow]GP<num>[/yellow]] [--baud N]",
            title="UART Error",
            border_style="red",
        )
        raise typer.Exit(1)
    return bus


def _require_rx(bus: dict, subcmd: str) -> None:
    if bus.get('rx') is None:
        OutputHelper.print_panel(
            f"uart {subcmd} requires an RX pin.\n"
            "Re-open with --rx GP<num>:\n\n"
            "  replx PORT uart open --tx GP<num> --rx [yellow]GP<num>[/yellow]",
            title="UART Error",
            border_style="red",
        )
        raise typer.Exit(1)


def _subcmd_open(client, port: str, core: str,
                 tx: Optional[str], rx: Optional[str],
                 baud: int, bits: int, parity: str, stop: int) -> None:
    if tx is None:
        raise ValueError("--tx is required. e.g. --tx GP0")

    try:
        tx_no = _parse_gp_pin(tx, 'TX')
        rx_no = _parse_gp_pin(rx, 'RX') if rx else None
        if parity not in _PARITY_VALUES:
            raise ValueError(f"--parity must be one of: {', '.join(_PARITY_VALUES)}")
        if stop not in _STOP_VALUES:
            raise ValueError(f"--stop must be 1 or 2")
        ch = _resolve_uart_ch(core, tx_no)
    except ValueError as e:
        OutputHelper.print_panel(str(e), title="UART Error", border_style="red")
        raise typer.Exit(1)

    cfg = dict(
        tx=tx_no, rx=rx_no, ch=ch,
        baud=baud, bits=bits, parity=parity, stop=stop,
        timeout_ms=1000
    )
    code = _make_open_code(cfg)
    raw = _exec(client, code, timeout=5.0)
    try:
        _parse_json_strict(raw)
    except RuntimeError as e:
        OutputHelper.print_panel(str(e), title="UART Error", border_style="red")
        raise typer.Exit(1)

    client.send_command('uart_bus_set',
                        tx=tx_no, rx=rx_no, ch=ch,
                        baud=baud, bits=bits, parity=parity,
                        stop=stop, timeout_ms=1000)
    _display_bus(cfg)


def _subcmd_bus(client) -> None:
    bus = client.send_command('uart_bus_get')
    if not bus:
        OutputHelper.print_panel(
            "[dim]No UART bus config saved.[/dim]\n"
            "Run: replx PORT uart open --tx [yellow]GP<num>[/yellow]",
            title="UART Bus",
            border_style="cyan",
        )
        return
    _display_bus(bus)


def _parse_hex_args(tokens: list[str]) -> list[int]:
    result: list[int] = []
    for token in tokens:
        parts = token.split()
        for part in parts:
            t = part.strip()
            if t.lower().startswith('0x'):
                t = t[2:]
            if not t:
                continue
            if len(t) % 2 != 0:
                raise ValueError(
                    f"Odd-length hex token: {part!r}. "
                    "Each token must have an even number of hex digits."
                )
            for i in range(0, len(t), 2):
                pair = t[i:i + 2]
                try:
                    result.append(int(pair, 16))
                except ValueError:
                    raise ValueError(f"Invalid hex byte {pair!r} in token {part!r}")
    return result


import re as _re

def _parse_text_data(tokens: list[str]) -> list[int]:
    text = ' '.join(tokens)

    def _repl(m: '_re.Match') -> str:
        s = m.group(1)
        if s == 'n':  return '\n'
        if s == 'r':  return '\r'
        if s == 't':  return '\t'
        if s == '\\': return '\\'
        if s.startswith('x'): return chr(int(s[1:], 16))
        return m.group(0)

    expanded = _re.sub(r'\\(n|r|t|\\|x[0-9A-Fa-f]{2})', _repl, text)
    return list(expanded.encode('utf-8'))


def _subcmd_write(client, pos_args: list[str], hex_mode: bool) -> None:
    if not pos_args:
        if hex_mode:
            raise ValueError(
                "Specify hex bytes.  e.g. uart write --hex 48 65 6C 6C 6F\n"
                "  or: uart write --hex 48656C6C6F"
            )
        raise ValueError(
            "Specify text to send.  e.g. uart write \"Hello\\n\"\n"
            "  or hex mode: uart write --hex 48 65 6C 6C 6F"
        )

    if hex_mode:
        try:
            data = _parse_hex_args(pos_args)
        except ValueError as e:
            raise ValueError(str(e))
        hex_repr = ' '.join(f'{b:02X}' for b in data)
        ascii_repr = ''.join(chr(b) if 0x20 <= b <= 0x7E else '.' for b in data)
        display = f"{hex_repr}  [dim]{ascii_repr}[/dim]"
        mode_label = 'HEX'
    else:
        data = _parse_text_data(pos_args)
        text_repr = repr(bytes(data).decode('utf-8', errors='replace'))
        hex_repr = ' '.join(f'{b:02X}' for b in data)
        display = f"{text_repr}  [dim]{hex_repr}[/dim]"
        mode_label = 'TEXT'

    bus = _get_bus(client)
    raw = _exec(client, _make_write_code(bus, data), timeout=5.0)
    result = _parse_json_strict(raw)
    OutputHelper.print_panel(
        f"Written: [bright_cyan]{result.get('written', len(data))}[/bright_cyan] bytes  [dim]({mode_label})[/dim]",
        title="UART Write",
        border_style="green",
    )
    OutputHelper._console.print(_render_hex_dump(bytes(data)))


def _subcmd_read(client, pos_args: list[str], timeout_ms: int, any_mode: bool) -> None:
    nbytes: Optional[int] = None
    if pos_args:
        try:
            nbytes = int(pos_args[0])
            if nbytes < 1:
                raise ValueError("NBYTES must be >= 1")
        except ValueError as e:
            raise ValueError(str(e))

    if any_mode:
        mode_label = 'drain (current buffer)'
    elif nbytes is not None:
        mode_label = f'read {nbytes} bytes  ' + ('infinite wait' if timeout_ms == 0 else f'timeout {timeout_ms} ms')
    else:
        mode_label = 'infinite wait' if timeout_ms == 0 else f'wait for data  timeout {timeout_ms} ms'

    bus = _get_bus(client)
    _require_rx(bus, 'read')

    exec_timeout = 3600.0 if (timeout_ms == 0 and not any_mode) else 5.0 + timeout_ms / 1000.0
    raw = _exec(client, _make_read_code(bus, nbytes, any_mode, timeout_ms), timeout=exec_timeout)
    result = _parse_json_strict(raw)

    hex_str = result.get('hex', '')
    length = result.get('len', 0)

    if not hex_str or length == 0:
        OutputHelper.print_panel(
            f"[dim]No data received[/dim]  [dim]({mode_label})[/dim]",
            title="UART Read",
            border_style="yellow",
        )
        return

    data = bytes.fromhex(hex_str)
    OutputHelper.print_panel(
        f"Received: [bright_cyan]{length}[/bright_cyan] bytes\n\n"
        + _render_hex_dump(data),
        title="UART Read",
        border_style="blue",
    )


def _subcmd_xfer(client, pos_args: list[str], count_n: Optional[int], timeout_ms: int, hex_mode: bool) -> None:
    if not pos_args:
        if hex_mode:
            raise ValueError("Specify hex bytes.  e.g. uart xfer --hex 01 02")
        raise ValueError("Specify text to send.  e.g. uart xfer \"Hello\\n\"")

    if count_n is not None and count_n < 1:
        raise ValueError("--rx-bytes N must be >= 1")

    if hex_mode:
        try:
            tx_data = _parse_hex_args(pos_args)
        except ValueError as e:
            raise ValueError(str(e))
        display = ' '.join(f'{b:02X}' for b in tx_data)
        mode_label = 'HEX'
    else:
        tx_data = _parse_text_data(pos_args)
        display = repr(bytes(tx_data).decode('utf-8', errors='replace'))
        mode_label = 'TEXT'

    bus = _get_bus(client)
    _require_rx(bus, 'xfer')

    if count_n is not None:
        rx_mode_label = f'{count_n} bytes  ' + ('infinite wait' if timeout_ms == 0 else f'timeout {timeout_ms} ms')
    else:
        rx_mode_label = 'infinite wait' if timeout_ms == 0 else f'wait for data  timeout {timeout_ms} ms'

    exec_timeout = 3600.0 if (timeout_ms == 0) else 5.0 + timeout_ms / 1000.0
    raw = _exec(client, _make_xfer_code(bus, tx_data, count_n, timeout_ms), timeout=exec_timeout)
    result = _parse_json_strict(raw)

    hex_str = result.get('hex', '')
    length = result.get('len', 0)
    written = result.get('written', len(tx_data))

    rx_section = (
        "\n\n" + _render_hex_dump(bytes.fromhex(hex_str))
        if hex_str and length > 0
        else "\n[dim]No RX data[/dim]"
    )
    OutputHelper.print_panel(
        f"TX: [bright_cyan]{written}[/bright_cyan] bytes  [dim]({mode_label})[/dim] → "
        f"[dim]{display}[/dim]\n"
        f"RX: [bright_cyan]{length}[/bright_cyan] bytes  [dim]({rx_mode_label})[/dim]"
        + rx_section,
        title="UART Xfer",
        border_style="blue",
    )


def _subcmd_monitor(client, width: int, idle_ms: int, text_mode: bool, chunk_mode: bool) -> None:
    bus = _get_bus(client)
    _require_rx(bus, 'monitor')

    code = _make_monitor_code(bus, idle_ms, text_mode)
    rx_pin = f"GP{bus['rx']}"
    if chunk_mode:
        mode_label = ('text' if text_mode else 'binary') + ' chunk'
    else:
        mode_label = "text" if text_mode else f"{width}-byte hex dump"
    idle_label = f"  idle-sep={idle_ms}ms" if idle_ms > 0 else ""
    OutputHelper.print_panel(
        f"TX: [bright_green]GP{bus['tx']}[/bright_green]  "
        f"RX: [bright_green]{rx_pin}[/bright_green]  "
        f"[bright_cyan]{bus['baud']}[/bright_cyan] baud\n"
        f"Mode: [magenta]{mode_label}[/magenta]{idle_label}\n"
        f"Press [bold]Ctrl+C[/bold] to stop.",
        title="UART Monitor",
        border_style="green",
    )
    _run_monitor_session(client, code, width, idle_ms, text_mode, chunk_mode)


def _subcmd_close(client) -> None:
    bus = client.send_command('uart_bus_get')
    if not bus:
        OutputHelper.print_panel(
            "[dim]No UART bus config to close.[/dim]",
            title="UART Close",
            border_style="yellow",
        )
        return

    init = _make_uart_init(bus)
    code = f"import json\n{init}\nu.deinit()\nprint(json.dumps({{'closed':True}}))"
    try:
        _exec(client, code, timeout=3.0)
    except Exception:
        pass
    client.send_command('uart_bus_clear')
    OutputHelper.print_panel(
        f"UART CH{bus['ch']} closed.",
        title="UART Close",
        border_style="green",
    )


def _print_uart_help() -> None:
    help_text = """\
UART open/write/read/xfer/monitor/close command.

[bold cyan]Usage:[/bold cyan]
  replx PORT uart open    --tx [yellow]GP<num>[/yellow] [[green]--rx GP<num>[/green]] [[green]--baud N[/green]] [[green]--bits 7|8[/green]] [[green]--parity none|odd|even[/green]] [[green]--stop 1|2[/green]]
  replx PORT uart bus
  replx PORT uart write   [yellow]TEXT...[/yellow]
  replx PORT uart write   [green]--hex[/green] [yellow]HEX...[/yellow]
  replx PORT uart read    [[yellow]NBYTES[/yellow]] [[green]--timeout MS[/green]] [[green]--any[/green]]
  replx PORT uart xfer    [yellow]TEXT...[/yellow] [[green]--rx-bytes N[/green]] [[green]--timeout MS[/green]]
  replx PORT uart xfer    [green]--hex[/green] [yellow]HEX...[/yellow] [[green]--rx-bytes N[/green]] [[green]--timeout MS[/green]]
  replx PORT uart monitor [[green]--width 8|16[/green]] [[green]--idle MS[/green]] [[green]--text[/green]] [[green]--chunk[/green]]
  replx PORT uart close

[bold cyan]Subcommands:[/bold cyan]
  [green]open[/green]     Configure UART and save to agent. Validates on board.
           [dim]--rx is optional; read/xfer/monitor require it.[/dim]
  [green]bus[/green]      Show saved UART config.
  [green]write[/green]    Send text to TX (default).  e.g. [cyan]"Hello\\n"[/cyan]
           [dim]--hex: send raw bytes. Accepts space-separated or concatenated hex.[/dim]
           [dim]  e.g. [cyan]--hex 48 65 6C 6C 6F[/cyan]  or  [cyan]--hex 48656C6C6F[/cyan][/dim]
  [green]read[/green]     Read bytes from RX.
           [dim](no args): wait for any data up to --timeout ms.[/dim]
           [dim]NBYTES: read exactly N bytes, wait up to --timeout ms.[/dim]
           [dim]--any: drain current RX buffer immediately (no waiting).[/dim]
  [green]xfer[/green]     Write TX then receive. Default: wait for any data up to --timeout.
           [dim]Default text mode; --hex for raw bytes.[/dim]
           [dim]--rx-bytes N: receive exactly N bytes (wait up to --timeout).[/dim]
  [green]monitor[/green]  Live RX display until Ctrl+C.
           [dim]Default: 16-byte hex+ASCII dump rows.[/dim]
           [dim]--text: stream raw UTF-8 output.[/dim]
           [dim]--chunk: per-chunk display with timestamp and length header.[/dim]
           [dim]--idle MS: insert separator after MS ms of silence.[/dim]
  [green]close[/green]    Deinit UART and clear bus config.

[bold cyan]Pin format:[/bold cyan]  [yellow]GP<num>[/yellow]  e.g. [cyan]GP0[/cyan], [cyan]GP4[/cyan], [cyan]GP20[/cyan]

[bold cyan]Channel resolution:[/bold cyan]
  RP2350    UART0: GP0,GP12,GP16,GP28  UART1: GP4,GP8,GP20,GP24
  Teensy    Fixed TX pin table (GP1→1, GP8→2, GP14→3 …)
  ESP32     CH=1 for all pins

[bold cyan]Options:[/bold cyan]
  [yellow]--tx GP<num>[/yellow]        TX pin [red](required)[/red]
  [yellow]--rx GP<num>[/yellow]        RX pin [dim](optional, needed for read/xfer/monitor)[/dim]
  [yellow]--baud N[/yellow]            Baud rate [dim](default: 115200)[/dim]
  [yellow]--bits 7|8[/yellow]          Data bits [dim](default: 8)[/dim]
  [yellow]--parity none|odd|even[/yellow]  Parity [dim](default: none)[/dim]
  [yellow]--stop 1|2[/yellow]          Stop bits [dim](default: 1)[/dim]
  [yellow]--timeout MS[/yellow]        RX wait timeout ms [dim](read/xfer only; 0 = infinite; default: 2000)[/dim]
                      [dim]Not valid with [bold]open[/bold] or [bold]monitor[/bold]. UART hardware timeout is fixed at 1000 ms.[/dim]
  [yellow]--any[/yellow]               Drain RX buffer immediately (read only)
  [yellow]--rx-bytes N[/yellow]        RX exact byte count for xfer [dim](optional; omit to wait for any data)[/dim]
  [yellow]--width 8|16[/yellow]        Hex dump row width [dim](default: 16)[/dim]
  [yellow]--idle MS[/yellow]           Idle separator threshold ms [dim](default: 0 = off)[/dim]
  [yellow]--text[/yellow]              Monitor text/ASCII stream mode
  [yellow]--chunk[/yellow]             Monitor per-chunk display: timestamp + length + content
  [yellow]--hex[/yellow]               write: send raw hex bytes instead of text

[bold cyan]Examples:[/bold cyan]
  replx COM3 uart open --tx GP0 --rx GP1 --baud 9600
  replx COM3 uart open --tx GP0 --baud 115200
  replx COM3 uart bus
  replx COM3 uart write "Hello\\n"
  replx COM3 uart write "AT+RST\\r\\n"
  replx COM3 uart write --hex 48 65 6C 6C 6F
  replx COM3 uart write --hex 48656C6C6F
  replx COM3 uart read                           [dim]# wait up to 2s (default)[/dim]
  replx COM3 uart read 16                        [dim]# wait up to 2s for exactly 16 bytes[/dim]
  replx COM3 uart read --timeout 5000            [dim]# wait up to 5s[/dim]
  replx COM3 uart read --timeout 0               [dim]# infinite wait (Ctrl+C to abort)[/dim]
  replx COM3 uart read --any                     [dim]# drain RX buffer immediately[/dim]
  replx COM3 uart xfer "AT+RST\\r\\n"              [dim]# wait for any response (default 2s)[/dim]
  replx COM3 uart xfer "AT+RST\\r\\n" --rx-bytes 8    [dim]# wait for exactly 8 bytes[/dim]
  replx COM3 uart xfer "AT" --timeout 3000       [dim]# wait up to 3s for any response[/dim]
  replx COM3 uart xfer --hex 01 03 00 00 00 0A --rx-bytes 25
  replx COM3 uart monitor
  replx COM3 uart monitor --width 8 --idle 200
  replx COM3 uart monitor --text
  replx COM3 uart monitor --chunk
  replx COM3 uart monitor --text --chunk
  replx COM3 uart close"""
    OutputHelper.print_panel(help_text, title="uart", border_style="dim")


@app.command(name="uart", rich_help_panel="Hardware")
def uart_cmd(
    args: Optional[list[str]] = typer.Argument(None, help="Subcommand: open bus write read xfer monitor close"),
    tx: Optional[str] = typer.Option(None, "--tx", metavar="GP<num>", help="TX pin (open only)"),
    rx: Optional[str] = typer.Option(None, "--rx", metavar="GP<num>", help="RX pin (open only)"),
    baud: int = typer.Option(115200, "--baud", metavar="N", help="Baud rate"),
    bits: int = typer.Option(8, "--bits", metavar="7|8", help="Data bits"),
    parity: str = typer.Option("none", "--parity", metavar="none|odd|even", help="Parity"),
    stop: int = typer.Option(1, "--stop", metavar="1|2", help="Stop bits"),
    timeout_ms: Optional[int] = typer.Option(None, "--timeout", metavar="MS", help="RX wait timeout ms (0=infinite, default 2000). read/xfer only; forbidden with open."),
    any_mode: bool = typer.Option(False, "--any", help="Drain RX buffer immediately (read only)"),
    count_n: Optional[int] = typer.Option(None, "--rx-bytes", metavar="N", help="RX exact byte count for xfer (optional; omit to wait for any data)"),
    width: int = typer.Option(16, "--width", metavar="8|16", help="Hex dump row width (monitor)"),
    idle_ms: int = typer.Option(0, "--idle", metavar="MS", help="Idle separator ms (monitor, 0=off)"),
    text_mode: bool = typer.Option(False, "--text", help="Monitor text/ASCII stream mode"),
    chunk_mode: bool = typer.Option(False, "--chunk", help="Monitor per-chunk mode: timestamp + length + content per received burst"),
    hex_mode: bool = typer.Option(False, "--hex", help="write: send raw hex bytes instead of text"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True),
):
    if show_help:
        _print_uart_help()
        raise typer.Exit()
    if not args:
        OutputHelper.print_panel(
            "Subcommands: [bright_blue]open  bus  write  read  xfer  monitor  close[/bright_blue]\n\n"
            "  [bright_green]replx PORT uart open --tx GP0 --rx GP1[/bright_green]\n"
            "  [bright_green]replx PORT uart write Hello[/bright_green]\n"
            "  [bright_green]replx PORT uart read 8[/bright_green]\n\n"
            "Use [bright_blue]replx uart --help[/bright_blue] for details.",
            title="UART",
            border_style="yellow",
        )
        raise typer.Exit(1)

    subcmd = args[0].lower()
    pos_args = args[1:]

    valid_subcmds = ('open', 'bus', 'write', 'read', 'xfer', 'monitor', 'close')
    if subcmd not in valid_subcmds:
        OutputHelper.print_panel(
            f"Unknown subcommand: {subcmd!r}\n\n"
            "Valid subcommands: " + "  ".join(valid_subcmds),
            title="UART Error",
            border_style="red",
        )
        raise typer.Exit(1)

    if width not in (8, 16):
        OutputHelper.print_panel("--width must be 8 or 16", title="UART Error", border_style="red")
        raise typer.Exit(1)

    if subcmd == 'open' and timeout_ms is not None:
        OutputHelper.print_panel(
            "--timeout is not valid for [bold]uart open[/bold].\n"
            "UART hardware timeout is fixed at 1000 ms internally.",
            title="UART Error",
            border_style="red",
        )
        raise typer.Exit(1)

    if subcmd == 'monitor' and timeout_ms is not None:
        OutputHelper.print_panel(
            "--timeout is not valid for [bold]uart monitor[/bold].\n"
            "Monitor runs until Ctrl+C. Use [bold]--idle MS[/bold] to insert separators on silence.",
            title="UART Error",
            border_style="red",
        )
        raise typer.Exit(1)

    effective_timeout = timeout_ms if timeout_ms is not None else 2000

    _ensure_connected()

    try:
        with _create_agent_client() as client:
            port = _get_device_port()

            if subcmd == 'bus':
                _subcmd_bus(client)
                return

            if subcmd == 'close':
                _subcmd_close(client)
                return

            if subcmd == 'open':
                core = _get_core(client, port)
                _subcmd_open(client, port, core, tx, rx, baud, bits, parity, stop)
                return

            if subcmd == 'write':
                _subcmd_write(client, pos_args, hex_mode)
            elif subcmd == 'read':
                _subcmd_read(client, pos_args, effective_timeout, any_mode)
            elif subcmd == 'xfer':
                _subcmd_xfer(client, pos_args, count_n, effective_timeout, hex_mode)
            elif subcmd == 'monitor':
                _subcmd_monitor(client, width, idle_ms, text_mode, chunk_mode)

    except ValueError as e:
        OutputHelper.print_panel(str(e), title="UART Error", border_style="red")
        raise typer.Exit(1)
    except RuntimeError as e:
        OutputHelper.print_panel(str(e), title="UART Error", border_style="red")
        raise typer.Exit(1)
