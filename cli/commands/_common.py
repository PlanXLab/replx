import json
import sys
from typing import Optional


def exec_code(client, code: str, timeout: float = 5.0) -> str:
    result = client.send_command('exec', code=code, timeout=timeout, max_retries=1)
    return ((result or {}).get('output') or '').strip()


def parse_json_strict(raw: str):
    if not raw:
        raise RuntimeError('No output from device')
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'Device error:\n{raw}') from exc


def normalize_port(port: Optional[str]) -> Optional[str]:
    if port and sys.platform.startswith('win'):
        return port.upper()
    return port


def parse_gp_pin(token: str, label: str = 'pin') -> int:
    """Parse a 'GP<num>' pin token. Used by adc/i2c/spi/uart commands."""
    s = (token or '').strip()
    if len(s) < 3 or s[:2].lower() != 'gp' or not s[2:].isdigit():
        raise ValueError(f"Invalid {label}: {token!r}. Use GP<num> format, e.g. GP2")
    pin_no = int(s[2:])
    if pin_no < 0:
        raise ValueError(f"Invalid {label}: {token!r}")
    return pin_no


def get_connection_info(client, port: Optional[str] = None) -> Optional[dict]:
    try:
        info = client.send_command('session_info', timeout=1.5)
    except Exception:
        return None

    if not info:
        return None

    connections = info.get('connections', [])
    if not connections:
        return None

    if port is None:
        return connections[0]

    normalized_port = normalize_port(port)
    for connection in connections:
        if normalize_port(connection.get('port', '')) == normalized_port:
            return connection
    return None


def get_connection_field(client, port: Optional[str], field: str) -> str:
    connection = get_connection_info(client, port)
    if not connection:
        return ''

    value = connection.get(field, '')
    if value is None:
        return ''
    return value if isinstance(value, str) else str(value)


def get_core(client, port: Optional[str]) -> str:
    return get_connection_field(client, port, 'core')


def get_device(client, port: Optional[str]) -> str:
    return get_connection_field(client, port, 'device')


def _ascii_char(b: int) -> str:
    return chr(b) if 0x20 <= b <= 0x7E else '.'


def render_hex_row(data: bytes, offset: int, width: int = 16) -> str:
    half = width // 2
    if len(data) > half:
        left = ' '.join(f'{b:02X}' for b in data[:half])
        right = ' '.join(f'{b:02X}' for b in data[half:])
        hex_str = left + '  ' + right
    else:
        hex_str = ' '.join(f'{b:02X}' for b in data)

    padded_width = (half * 3 - 1) + 2 + (half * 3 - 1)
    hex_padded = hex_str.ljust(padded_width)
    ascii_str = ''.join(_ascii_char(b) for b in data).ljust(width)

    return (
        f'[dim]{offset:06X}[/dim]  '
        f'[bright_cyan]{hex_padded}[/bright_cyan]  '
        f'[dim]{ascii_str}[/dim]'
    )


def render_hex_dump(data: bytes, width: int = 16) -> str:
    if not data:
        return '[dim](no data)[/dim]'

    half = width // 2
    header_hex = (
        ' '.join(f'{i:02X}' for i in range(half))
        + '  '
        + ' '.join(f'{i:02X}' for i in range(half, width))
    )
    padded_width = (half * 3 - 1) + 2 + (half * 3 - 1)
    separator_width = 8 + padded_width + 2 + width

    lines = [
        f'[dim]Offset  {header_hex}  ASCII[/dim]',
        '[dim]' + '─' * separator_width + '[/dim]',
    ]
    for offset in range(0, len(data), width):
        lines.append(render_hex_row(data[offset:offset + width], offset, width))
    return '\n'.join(lines)