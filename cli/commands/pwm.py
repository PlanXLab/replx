import json
import math
from typing import Optional

import typer

from ..helpers import OutputHelper
from ..connection import _ensure_connected, _create_agent_client
from ..app import app

_DUTY_MODES = {"percent", "u16", "pulse_us"}
_BOARD_PWM_STATE = "__replx_pwm_state__"


def _parse_gp(token: str) -> tuple[int, str]:
    s = (token or "").strip()
    if len(s) < 3 or s[:2].lower() != "gp" or not s[2:].isdigit():
        raise ValueError(f"Invalid PWM pin: {token!r}. Use GP<num> format, e.g. GP15")
    pin_no = int(s[2:])
    if pin_no < 0:
        raise ValueError(f"Invalid PWM pin: {token!r}")
    return pin_no, f"GP{pin_no}"


def _parse_freq(freq: Optional[int]) -> int:
    if freq is None:
        raise ValueError("--freq is required")
    if freq <= 0:
        raise ValueError("--freq must be > 0")
    return int(freq)


def _parse_duty_mode(token: Optional[str]) -> str:
    if token is None:
        raise ValueError("--duty is required and must be one of: percent, u16, pulse_us")
    mode = token.strip().lower()
    if mode not in _DUTY_MODES:
        raise ValueError("--duty must be one of: percent, u16, pulse_us")
    return mode


def _require_finite(value: float, label: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def _period_us(freq: int) -> float:
    return 1_000_000.0 / float(freq)


def _value_to_duty_u16(mode: str, value: float | int, freq: int) -> int:
    if mode == 'percent':
        percent = _require_finite(float(value), 'Duty percent')
        if percent < 0.0 or percent > 100.0:
            raise ValueError("Duty percent must be in range 0..100")
        return max(0, min(65535, int(round(percent * 65535.0 / 100.0))))

    if mode == 'u16':
        duty_u16 = int(value)
        if duty_u16 < 0 or duty_u16 > 65535:
            raise ValueError("Duty u16 must be in range 0..65535")
        return duty_u16

    if mode == 'pulse_us':
        pulse_us = _require_finite(float(value), 'Pulse width')
        if pulse_us < 0.0:
            raise ValueError("Pulse width must be >= 0")
        period_us = _period_us(freq)
        if pulse_us > period_us:
            raise ValueError(
                f"Pulse width must be <= PWM period ({period_us:.3f} us at {freq} Hz)"
            )
        return max(0, min(65535, int(round(pulse_us * 65535.0 / period_us))))

    raise ValueError(f"Unsupported duty mode: {mode}")


def _duty_percent_from_u16(duty_u16: int) -> float:
    return float(duty_u16) * 100.0 / 65535.0


def _pulse_us_from_u16(duty_u16: int, freq: int) -> float:
    return float(duty_u16) * _period_us(freq) / 65535.0


def _format_number(value: float | int, digits: int = 3) -> str:
    if isinstance(value, int):
        return str(value)
    f = float(value)
    if f.is_integer():
        return str(int(f))
    return f"{f:.{digits}f}".rstrip('0').rstrip('.')


def _pick_write_level(
    duty_percent: Optional[float],
    duty_u16: Optional[int],
    pulse_us: Optional[float],
) -> tuple[str, float | int]:
    choices: list[tuple[str, float | int]] = []
    if duty_percent is not None:
        choices.append(('percent', duty_percent))
    if duty_u16 is not None:
        choices.append(('u16', duty_u16))
    if pulse_us is not None:
        choices.append(('pulse_us', pulse_us))

    if len(choices) != 1:
        raise ValueError(
            "Specify exactly one of: --duty-percent, --duty-u16, --pulse-us"
        )
    return choices[0]


def _parse_seq_value(token: str, mode: str) -> float | int:
    text = (token or '').strip()
    if not text:
        raise ValueError("Empty seq token is not allowed")

    if mode == 'u16':
        if not text.isdigit():
            raise ValueError(f"Invalid duty token: {token!r}. Use a decimal integer for --duty u16")
        return int(text)

    try:
        value = float(text)
    except ValueError:
        label = 'percent' if mode == 'percent' else 'pulse_us'
        raise ValueError(f"Invalid duty token: {token!r}. Use a numeric value for --duty {label}")

    return _require_finite(value, 'Duty token')


def _parse_seq_tokens(tokens: list[str], mode: str) -> list[tuple[str, float | int]]:
    if not tokens:
        raise ValueError("pwm seq requires one or more duty/delay tokens")

    ops: list[tuple[str, float | int]] = []
    write_count = 0

    for token in tokens:
        t = token.strip().lower()
        if len(t) >= 2 and t[0] in ('u', 'm') and t[1:].isdigit():
            ops.append((t[0], int(t[1:])))
            continue

        value = _parse_seq_value(token, mode)
        ops.append(('d', value))
        write_count += 1

    if write_count == 0:
        raise ValueError("pwm seq requires at least one duty token")

    return ops


def _format_seq_ops(ops: list[tuple[str, float | int]]) -> str:
    parts = []
    for kind, value in ops:
        if kind == 'd':
            parts.append(_format_number(value))
        elif kind == 'u':
            parts.append(f"u{int(value)}")
        elif kind == 'm':
            parts.append(f"m{int(value)}")
    return '-'.join(parts) if parts else '-'


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


def _make_write_code(pin_no: int, pin_name: str, freq: int, mode: str, value: float | int, duty_u16: int) -> str:
    pulse_ns_line = ""
    if mode == 'pulse_us':
        pulse_ns_line = (
            f"pulse_ns=int(round(float({float(value)!r})*1000.0))\n"
            "try:\n"
            "    p.duty_ns(pulse_ns)\n"
            "except Exception:\n"
            f"    p.duty_u16({int(duty_u16)})\n"
        )
    else:
        pulse_ns_line = f"p.duty_u16({int(duty_u16)})\n"

    return (
        "from machine import Pin,PWM\n"
        "import json\n"
        f"pin_no={pin_no}\n"
        f"freq={int(freq)}\n"
        f"input_mode={mode!r}\n"
        f"input_value={repr(value)}\n"
        f"duty_u16={int(duty_u16)}\n"
        "period_us=1000000.0/freq\n"
        f"_state=globals().setdefault({_BOARD_PWM_STATE!r}, {{}})\n"
        "p=_state.get(pin_no)\n"
        "if p is None:\n"
        "    p=PWM(Pin(pin_no))\n"
        "    _state[pin_no]=p\n"
        "p.freq(freq)\n"
        + pulse_ns_line +
        "pulse_us=(duty_u16*period_us)/65535.0\n"
        f"print(json.dumps({{'pin':{pin_name!r},'freq':freq,'input_mode':input_mode,'input_value':input_value,'duty_u16':duty_u16,'pulse_us':pulse_us,'active':True}}))"
    )


def _make_seq_code(pin_no: int, pin_name: str, freq: int, mode: str, ops: list[tuple[str, float | int]]) -> str:
    py_ops = repr(ops)
    return (
        "from machine import Pin,PWM\n"
        "import time,json\n"
        f"pin_no={pin_no}\n"
        f"freq={int(freq)}\n"
        f"mode={mode!r}\n"
        f"ops={py_ops}\n"
        "period_us=1000000.0/freq\n"
        f"_state=globals().setdefault({_BOARD_PWM_STATE!r}, {{}})\n"
        "p=_state.get(pin_no)\n"
        "if p is None:\n"
        "    p=PWM(Pin(pin_no))\n"
        "    _state[pin_no]=p\n"
        "p.freq(freq)\n"
        "writes=0\n"
        "last_value=None\n"
        "last_u16=0\n"
        "last_pulse_ns=0\n"
        "def to_u16(v):\n"
        "    if mode=='percent':\n"
        "        if v < 0 or v > 100:\n"
        "            raise ValueError('Duty percent must be in range 0..100')\n"
        "        return max(0, min(65535, int(round(v * 65535.0 / 100.0))))\n"
        "    if mode=='u16':\n"
        "        iv = int(v)\n"
        "        if iv < 0 or iv > 65535:\n"
        "            raise ValueError('Duty u16 must be in range 0..65535')\n"
        "        return iv\n"
        "    if v < 0 or v > period_us:\n"
        "        raise ValueError('Pulse width must be within PWM period')\n"
        "    return max(0, min(65535, int(round(v * 65535.0 / period_us))))\n"
        "for kind,val in ops:\n"
        "    if kind=='d':\n"
        "        last_u16=to_u16(val)\n"
        "        if mode=='pulse_us':\n"
        "            last_pulse_ns=int(round(float(val)*1000.0))\n"
        "            try:\n"
        "                p.duty_ns(last_pulse_ns)\n"
        "            except Exception:\n"
        "                p.duty_u16(last_u16)\n"
        "        else:\n"
        "            p.duty_u16(last_u16)\n"
        "        last_value=val\n"
        "        writes+=1\n"
        "    elif kind=='u':\n"
        "        time.sleep_us(int(val))\n"
        "    elif kind=='m':\n"
        "        time.sleep_ms(int(val))\n"
        "pulse_us=(last_u16*period_us)/65535.0\n"
        f"print(json.dumps({{'pin':{pin_name!r},'freq':freq,'mode':mode,'writes':writes,'final_value':last_value,'duty_u16':last_u16,'pulse_us':pulse_us,'active':True}}))"
    )


def _make_stop_code(pin_no: int, pin_name: str) -> str:
    return (
        "from machine import Pin,PWM\n"
        "import json\n"
        f"pin_no={pin_no}\n"
        f"_state=globals().setdefault({_BOARD_PWM_STATE!r}, {{}})\n"
        "p=_state.pop(pin_no,None)\n"
        "if p is None:\n"
        "    p=PWM(Pin(pin_no))\n"
        "p.deinit()\n"
        "pin=Pin(pin_no,Pin.OUT)\n"
        "pin.value(0)\n"
        f"print(json.dumps({{'pin':{pin_name!r},'stopped':True,'value':pin.value()}}))"
    )


def _print_pwm_help() -> None:
    help_text = """\
PWM write/sequence/stop command for a single pin.

[bold cyan]Usage:[/bold cyan]
  replx pwm [yellow]SUBCOMMAND[/yellow] ...

[bold cyan]Subcommands:[/bold cyan]
  [green]write[/green]  Start/update PWM on one pin.
  [green]seq[/green]    Execute a duty+delay pattern in one board call.
  [green]stop[/green]   Stop PWM on one pin and drive it low.

[bold cyan]Pin Format:[/bold cyan]
  [yellow]GP<num>[/yellow] only, case-insensitive. Example: [cyan]GP15[/cyan]

[bold cyan]Write:[/bold cyan]
  replx pwm write [yellow]GP<num>[/yellow] [green]--freq HZ[/green] [[green]--duty-percent P[/green] | [green]--duty-u16 N[/green] | [green]--pulse-us US[/green]]

[bold cyan]Sequence:[/bold cyan]
  replx pwm seq [yellow]GP<num>[/yellow] [green]--freq HZ[/green] [green]--duty MODE[/green] [cyan]TOKEN[/cyan]...

  [bold]MODE[/bold]
    [cyan]percent[/cyan]   duty tokens are 0..100
    [cyan]u16[/cyan]       duty tokens are 0..65535
    [cyan]pulse_us[/cyan]  duty tokens are pulse width in microseconds

  [bold]TOKEN[/bold]
    [cyan]VALUE[/cyan]   duty value in the selected MODE
    [cyan]u<N>[/cyan]    delay in microseconds
    [cyan]m<N>[/cyan]    delay in milliseconds

[bold cyan]Stop:[/bold cyan]
  replx pwm stop [yellow]GP<num>[/yellow]

[bold cyan]Examples:[/bold cyan]
  replx COM3 pwm write GP15 --freq 50 --pulse-us 1500
  replx COM3 pwm write GP15 --freq 1000 --duty-percent 25
  replx COM3 pwm write GP15 --freq 20000 --duty-u16 32768
  replx COM3 pwm seq GP15 --freq 100 --duty percent 0 m200 25 m200 50 m200 75 m200 100
  replx COM3 pwm seq GP15 --freq 50 --duty pulse_us 1000 m500 1500 m500 2000
  replx COM3 pwm stop GP15

[bold cyan]Notes:[/bold cyan]
  • One PWM pin only.
  • [yellow]write[/yellow] requires [yellow]--freq[/yellow] and exactly one duty option.
  • [yellow]seq[/yellow] requires [yellow]--freq[/yellow], [yellow]--duty[/yellow], and at least one duty token.
  • [yellow]pulse_us[/yellow] values must not exceed the PWM period for the selected frequency."""
    OutputHelper.print_panel(help_text, title="pwm", border_style="dim")


def _subcmd_write(client, pos_args: list[str], freq: Optional[int], duty_percent: Optional[float], duty_u16: Optional[int], pulse_us: Optional[float]) -> None:
    if len(pos_args) != 1:
        raise ValueError(
            "Usage: replx pwm write GP<num> --freq HZ (--duty-percent P | --duty-u16 N | --pulse-us US)"
        )

    pin_no, pin_name = _parse_gp(pos_args[0])
    freq_value = _parse_freq(freq)
    mode, value = _pick_write_level(duty_percent, duty_u16, pulse_us)
    duty_value = _value_to_duty_u16(mode, value, freq_value)
    raw = _exec(client, _make_write_code(pin_no, pin_name, freq_value, mode, value, duty_value), timeout=5.0)
    data = _parse_json_strict(raw)
    actual_u16 = int(data.get('duty_u16', duty_value))
    actual_pulse_us = float(data.get('pulse_us', _pulse_us_from_u16(actual_u16, freq_value)))

    OutputHelper.print_panel(
        f"Pin: [bright_green]{pin_name}[/bright_green]\n"
        f"Freq: [bright_cyan]{freq_value} Hz[/bright_cyan]\n"
        f"Input: [magenta]{mode}[/magenta] = [bright_cyan]{_format_number(value)}[/bright_cyan]\n"
        f"Duty: [bright_cyan]{actual_u16}[/bright_cyan] / 65535\n"
        f"Percent: [bright_cyan]{_format_number(_duty_percent_from_u16(actual_u16))}%[/bright_cyan]\n"
        f"Pulse: [bright_cyan]{_format_number(actual_pulse_us)} us[/bright_cyan]\n"
        f"Active: [bright_cyan]{bool(data.get('active', True))}[/bright_cyan]",
        title="PWM Write",
        border_style="green",
    )


def _subcmd_seq(client, pos_args: list[str], freq: Optional[int], duty_mode: Optional[str]) -> None:
    if len(pos_args) < 2:
        raise ValueError("Usage: replx pwm seq GP<num> --freq HZ --duty MODE TOKEN...")

    pin_no, pin_name = _parse_gp(pos_args[0])
    freq_value = _parse_freq(freq)
    mode = _parse_duty_mode(duty_mode)
    ops = _parse_seq_tokens(pos_args[1:], mode)

    for kind, value in ops:
        if kind == 'd':
            _value_to_duty_u16(mode, value, freq_value)

    timeout = 5.0
    timeout += sum(int(value) / 1000.0 for kind, value in ops if kind == 'm')
    timeout += sum(int(value) / 1_000_000.0 for kind, value in ops if kind == 'u')
    timeout = min(max(timeout, 5.0), 120.0)

    raw = _exec(client, _make_seq_code(pin_no, pin_name, freq_value, mode, ops), timeout=timeout)
    data = _parse_json_strict(raw)
    final_u16 = int(data.get('duty_u16', 0))
    final_pulse_us = float(data.get('pulse_us', _pulse_us_from_u16(final_u16, freq_value)))
    writes = int(data.get('writes', 0))

    OutputHelper.print_panel(
        f"Pin: [bright_green]{pin_name}[/bright_green]\n"
        f"Freq: [bright_cyan]{freq_value} Hz[/bright_cyan]\n"
        f"Basis: [magenta]{mode}[/magenta]\n"
        f"Seq: [bright_cyan]{_format_seq_ops(ops)}[/bright_cyan]\n"
        f"Writes: [bright_cyan]{writes}[/bright_cyan]\n"
        f"Final duty: [bright_cyan]{final_u16}[/bright_cyan] / 65535\n"
        f"Final percent: [bright_cyan]{_format_number(_duty_percent_from_u16(final_u16))}%[/bright_cyan]\n"
        f"Final pulse: [bright_cyan]{_format_number(final_pulse_us)} us[/bright_cyan]\n"
        f"Active: [bright_cyan]{bool(data.get('active', True))}[/bright_cyan]",
        title="PWM Seq",
        border_style="green",
    )


def _subcmd_stop(client, pos_args: list[str]) -> None:
    if len(pos_args) != 1:
        raise ValueError("Usage: replx pwm stop GP<num>")

    pin_no, pin_name = _parse_gp(pos_args[0])
    raw = _exec(client, _make_stop_code(pin_no, pin_name), timeout=5.0)
    data = _parse_json_strict(raw)

    OutputHelper.print_panel(
        f"Pin: [bright_green]{pin_name}[/bright_green]\n"
        f"Stopped: [bright_cyan]{bool(data.get('stopped', True))}[/bright_cyan]\n"
        f"Level: [bright_cyan]{int(data.get('value', 0))}[/bright_cyan]",
        title="PWM Stop",
        border_style="green",
    )


@app.command(name="pwm", rich_help_panel="Hardware")
def pwm_cmd(
    args: Optional[list[str]] = typer.Argument(
        None, help="Subcommand: write  seq  stop"
    ),
    freq: Optional[int] = typer.Option(None, "--freq", metavar="HZ", help="PWM frequency in Hz"),
    duty: Optional[str] = typer.Option(None, "--duty", metavar="MODE", help="Duty basis for pwm seq: percent, u16, pulse_us"),
    duty_percent: Optional[float] = typer.Option(None, "--duty-percent", metavar="P", help="Duty percent for pwm write (0..100)"),
    duty_u16: Optional[int] = typer.Option(None, "--duty-u16", metavar="N", help="Duty u16 for pwm write (0..65535)"),
    pulse_us: Optional[float] = typer.Option(None, "--pulse-us", metavar="US", help="Pulse width in microseconds for pwm write"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True),
):
    if show_help or not args:
        _print_pwm_help()
        raise typer.Exit()

    subcmd = args[0].lower()
    pos_args = args[1:]

    if subcmd not in ('write', 'seq', 'stop'):
        OutputHelper.print_panel(
            f"Unknown subcommand: {subcmd!r}\n\n"
            "Valid subcommands: write  seq  stop",
            title="PWM Error",
            border_style="red",
        )
        raise typer.Exit(1)

    if subcmd != 'seq' and duty is not None:
        OutputHelper.print_panel(
            "--duty is supported only for pwm seq",
            title="PWM Error",
            border_style="red",
        )
        raise typer.Exit(1)

    if subcmd != 'write' and any(v is not None for v in (duty_percent, duty_u16, pulse_us)):
        OutputHelper.print_panel(
            "--duty-percent, --duty-u16, and --pulse-us are supported only for pwm write",
            title="PWM Error",
            border_style="red",
        )
        raise typer.Exit(1)

    if subcmd == 'stop' and freq is not None:
        OutputHelper.print_panel(
            "--freq is not used for pwm stop",
            title="PWM Error",
            border_style="red",
        )
        raise typer.Exit(1)

    _ensure_connected()

    try:
        with _create_agent_client() as client:
            if subcmd == 'write':
                _subcmd_write(client, pos_args, freq, duty_percent, duty_u16, pulse_us)
            elif subcmd == 'seq':
                _subcmd_seq(client, pos_args, freq, duty)
            elif subcmd == 'stop':
                _subcmd_stop(client, pos_args)
    except ValueError as e:
        OutputHelper.print_panel(str(e), title="PWM Error", border_style="red")
        raise typer.Exit(1)
