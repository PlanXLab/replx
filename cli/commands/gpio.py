import ast
import builtins
import json
import signal
import sys
from typing import Optional

import typer

from replx.utils.constants import CTRL_C
from ..helpers import OutputHelper
from ..connection import _ensure_connected, _create_agent_client
from ..app import app


_READ_ACTIONS = {"read", "wait_h", "wait_l", "pulse_h", "pulse_l"}
_EDGE_MODES = {"rising", "falling", "both"}
_EXPR_FUNCS = {
    'abs': abs,
    'round': round,
    'min': min,
    'max': max,
}


def _parse_gp(token: str) -> tuple[int, str]:
    s = (token or "").strip()
    if len(s) < 3 or s[:2].lower() != "gp" or not s[2:].isdigit():
        raise ValueError(f"Invalid GPIO pin: {token!r}. Use GP<num> format, e.g. GP1")
    pin_no = int(s[2:])
    if pin_no < 0:
        raise ValueError(f"Invalid GPIO pin: {token!r}")
    return pin_no, f"GP{pin_no}"


def _parse_logic(token: str) -> int:
    s = (token or "").strip()
    if s not in ("0", "1"):
        raise ValueError(f"Invalid logic value: {token!r}. Use 0 or 1")
    return int(s)


def _parse_edge(token: Optional[str]) -> Optional[str]:
    if token is None:
        return None
    edge = token.strip().lower()
    if edge not in _EDGE_MODES:
        raise ValueError("--edge must be one of: rising, falling, both")
    return edge


def _is_repeat_explicit() -> bool:
    return '--repeat' in sys.argv or '-n' in sys.argv


def _validate_expr_ast(node: ast.AST, allowed_names: set[str]) -> None:
    allowed_nodes = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Call,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.UAdd,
        ast.USub,
    )

    for child in ast.walk(node):
        if not isinstance(child, allowed_nodes):
            raise ValueError("--expr supports arithmetic expressions only")
        if isinstance(child, ast.Name) and child.id not in allowed_names:
            raise ValueError(f"Unknown name in --expr: {child.id}")
        if isinstance(child, ast.Call):
            if not isinstance(child.func, ast.Name) or child.func.id not in _EXPR_FUNCS:
                raise ValueError("--expr only allows these functions: abs, round, min, max")


def _eval_expr(expr: str, data: dict) -> object:
    values = {
        'value': int(data.get('value', 0)),
        'writes': int(data.get('writes', 0)),
    }
    if 'pulse_us' in data:
        values['pulse_us'] = int(data['pulse_us'])
    if 'wait_ms' in data:
        values['wait_ms'] = int(data['wait_ms'])

    try:
        tree = ast.parse(expr, mode='eval')
    except SyntaxError:
        raise ValueError("Invalid --expr syntax")

    _validate_expr_ast(tree, set(values) | set(_EXPR_FUNCS))

    try:
        return builtins.eval(
            builtins.compile(tree, '<gpio-expr>', 'eval'),
            {'__builtins__': {}},
            {**_EXPR_FUNCS, **values},
        )
    except Exception as e:
        raise ValueError(f"Failed to evaluate --expr: {e}")


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


def _run_interactive_script(client, code: str, *, live_output: bool = False) -> str:
    stop_requested = False
    pending_input: list[bytes] = []
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    original_sigint = signal.getsignal(signal.SIGINT)

    def sigint_handler(signum, frame):
        nonlocal stop_requested
        stop_requested = True
        pending_input.append(CTRL_C)

    def output_callback(data: bytes, stream_type: str = "stdout"):
        text = data.decode('utf-8', errors='replace')
        if stream_type == 'stderr':
            stderr_parts.append(text)
            return
        stdout_parts.append(text)
        if live_output and text:
            sys.stdout.write(text)
            sys.stdout.flush()

    def input_provider() -> bytes:
        if pending_input:
            return pending_input.pop(0)
        return b''

    def stop_check() -> bool:
        return stop_requested

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

    if stderr_parts:
        raise RuntimeError(''.join(stderr_parts).strip())

    raw = ''.join(stdout_parts)
    if stop_requested and not raw.strip():
        raise typer.Exit(130)
    return raw.strip()


def _format_values(values: list[int]) -> str:
    chunks = []
    for i in range(0, len(values), 32):
        chunks.append(' '.join(str(v) for v in values[i:i + 32]))
    return '\n'.join(chunks) if chunks else '-'


def _format_seq_ops(ops: list[tuple[str, int]]) -> str:
    parts = []
    for kind, value in ops:
        if kind == 'w':
            parts.append(str(int(value)))
        elif kind == 'u':
            parts.append(f"u{int(value)}")
        elif kind == 'm':
            parts.append(f"m{int(value)}")
    return '-'.join(parts) if parts else '-'


def _parse_seq_tokens(tokens: list[str], write_pin_name: str) -> tuple[list[tuple[str, int]], Optional[int], Optional[str], str]:
    if not tokens:
        raise ValueError("gpio seq requires write tokens")

    read_pin_no = None
    read_pin_name = write_pin_name
    read_action = None

    if len(tokens) >= 2:
        maybe_action = tokens[-1].lower()
        maybe_pin = tokens[-2]
        if maybe_action in _READ_ACTIONS:
            try:
                pin_no, pin_name = _parse_gp(maybe_pin)
                read_pin_no = pin_no
                read_pin_name = pin_name
                read_action = maybe_action
                tokens = tokens[:-2]
            except ValueError:
                pass

    if read_action is None and tokens and tokens[-1].lower() in _READ_ACTIONS:
        read_action = tokens[-1].lower()
        tokens = tokens[:-1]

    if not tokens:
        raise ValueError("gpio seq requires one or more write tokens before read_action")

    ops: list[tuple[str, int]] = []
    write_count = 0

    for token in tokens:
        t = token.strip().lower()
        if t in ('0', '1'):
            ops.append(('w', int(t)))
            write_count += 1
            continue
        if len(t) >= 2 and t[0] in ('u', 'm') and t[1:].isdigit():
            value = int(t[1:])
            ops.append((t[0], value))
            continue
        raise ValueError(
            f"Invalid seq token: {token!r}. Use 0, 1, u<N>, m<N> or a final read_action"
        )

    if write_count == 0:
        raise ValueError("gpio seq requires at least one write token (0 or 1)")

    return ops, read_pin_no, read_action, read_pin_name


def _make_read_code(pin_no: int, pin_name: str, repeat: int) -> str:
    return (
        "from machine import Pin\n"
        "import json\n"
        f"p=Pin({pin_no},Pin.IN)\n"
        "vals=[]\n"
        f"for _ in range({repeat}):\n"
        "    vals.append(p.value())\n"
        f"print(json.dumps({{'pin':{pin_name!r},'values':vals}}))"
    )


def _make_watch_code(pin_no: int, edge: str) -> str:
    return (
        "from machine import Pin\n"
        "import time\n"
        f"p=Pin({pin_no},Pin.IN)\n"
        f"edge={edge!r}\n"
        "last=p.value()\n"
        "while True:\n"
        "    v=p.value()\n"
        "    if v!=last:\n"
        "        e='rising' if v else 'falling'\n"
        "        if edge=='both' or edge==e:\n"
        "            print(str(time.ticks_ms())+' '+e)\n"
        "        last=v\n"
        "    time.sleep_ms(1)"
    )


def _make_write_code(pin_no: int, pin_name: str, value: int) -> str:
    return (
        "from machine import Pin\n"
        "import json\n"
        f"p=Pin({pin_no},Pin.OUT)\n"
        f"p.value({value})\n"
        f"print(json.dumps({{'pin':{pin_name!r},'value':p.value()}}))"
    )


def _make_seq_code(write_pin_no: int, write_pin_name: str, ops: list[tuple[str, int]], read_pin_no: Optional[int], read_pin_name: str, read_action: Optional[str], timeout_ms: int) -> str:
    py_ops = repr(ops)
    action_expr = repr(read_action)
    read_pin_expr = 'None' if read_pin_no is None else str(read_pin_no)
    return (
        "from machine import Pin\n"
        "import time,json\n"
        f"wp=Pin({write_pin_no},Pin.OUT)\n"
        f"ops={py_ops}\n"
        "writes=0\n"
        "for kind,val in ops:\n"
        "    if kind=='w':\n"
        "        wp.value(val)\n"
        "        writes+=1\n"
        "    elif kind=='u':\n"
        "        time.sleep_us(val)\n"
        "    elif kind=='m':\n"
        "        time.sleep_ms(val)\n"
        f"action={action_expr}\n"
        f"read_pin_no={read_pin_expr}\n"
        f"timeout_ms={int(timeout_ms)}\n"
        "rp=Pin(read_pin_no,Pin.IN) if read_pin_no is not None else None\n"
        f"res={{'write_pin':{write_pin_name!r},'writes':writes,'value':wp.value()}}\n"
        "def _expired(t0):\n"
        "    return timeout_ms > 0 and time.ticks_diff(time.ticks_ms(), t0) >= timeout_ms\n"
        "if action=='read':\n"
        "    res['read_action']='read'\n"
        f"    res['read_pin']={read_pin_name!r}\n"
        "    if rp is None:\n"
        f"        rp=Pin({write_pin_no},Pin.IN)\n"
        "    res['value']=rp.value()\n"
        "elif action=='wait_h':\n"
        "    if rp is None:\n"
        f"        rp=Pin({write_pin_no},Pin.IN)\n"
        "    t0=time.ticks_ms()\n"
        "    while rp.value()==0:\n"
        "        if _expired(t0):\n"
        "            res['read_action']='wait_h'\n"
        f"            res['read_pin']={read_pin_name!r}\n"
        "            res['timeout']=True\n"
        "            res['timeout_ms']=timeout_ms\n"
        "            res['value']=rp.value()\n"
        "            print(json.dumps(res))\n"
        "            raise SystemExit\n"
        "        time.sleep_us(50)\n"
        "    res['read_action']='wait_h'\n"
        f"    res['read_pin']={read_pin_name!r}\n"
        "    res['wait_ms']=time.ticks_diff(time.ticks_ms(),t0)\n"
        "    res['value']=rp.value()\n"
        "elif action=='wait_l':\n"
        "    if rp is None:\n"
        f"        rp=Pin({write_pin_no},Pin.IN)\n"
        "    t0=time.ticks_ms()\n"
        "    while rp.value()==1:\n"
        "        if _expired(t0):\n"
        "            res['read_action']='wait_l'\n"
        f"            res['read_pin']={read_pin_name!r}\n"
        "            res['timeout']=True\n"
        "            res['timeout_ms']=timeout_ms\n"
        "            res['value']=rp.value()\n"
        "            print(json.dumps(res))\n"
        "            raise SystemExit\n"
        "        time.sleep_us(50)\n"
        "    res['read_action']='wait_l'\n"
        f"    res['read_pin']={read_pin_name!r}\n"
        "    res['wait_ms']=time.ticks_diff(time.ticks_ms(),t0)\n"
        "    res['value']=rp.value()\n"
        "elif action=='pulse_h':\n"
        "    if rp is None:\n"
        f"        rp=Pin({write_pin_no},Pin.IN)\n"
        "    t0=time.ticks_ms()\n"
        "    while rp.value()==0:\n"
        "        if _expired(t0):\n"
        "            res['read_action']='pulse_h'\n"
        f"            res['read_pin']={read_pin_name!r}\n"
        "            res['timeout']=True\n"
        "            res['timeout_ms']=timeout_ms\n"
        "            res['value']=rp.value()\n"
        "            print(json.dumps(res))\n"
        "            raise SystemExit\n"
        "        time.sleep_us(50)\n"
        "    t1=time.ticks_us()\n"
        "    while rp.value()==1:\n"
        "        if _expired(t0):\n"
        "            res['read_action']='pulse_h'\n"
        f"            res['read_pin']={read_pin_name!r}\n"
        "            res['timeout']=True\n"
        "            res['timeout_ms']=timeout_ms\n"
        "            res['value']=rp.value()\n"
        "            print(json.dumps(res))\n"
        "            raise SystemExit\n"
        "        time.sleep_us(50)\n"
        "    res['read_action']='pulse_h'\n"
        f"    res['read_pin']={read_pin_name!r}\n"
        "    res['pulse_us']=time.ticks_diff(time.ticks_us(),t1)\n"
        "    res['value']=rp.value()\n"
        "elif action=='pulse_l':\n"
        "    if rp is None:\n"
        f"        rp=Pin({write_pin_no},Pin.IN)\n"
        "    t0=time.ticks_ms()\n"
        "    while rp.value()==1:\n"
        "        if _expired(t0):\n"
        "            res['read_action']='pulse_l'\n"
        f"            res['read_pin']={read_pin_name!r}\n"
        "            res['timeout']=True\n"
        "            res['timeout_ms']=timeout_ms\n"
        "            res['value']=rp.value()\n"
        "            print(json.dumps(res))\n"
        "            raise SystemExit\n"
        "        time.sleep_us(50)\n"
        "    t1=time.ticks_us()\n"
        "    while rp.value()==0:\n"
        "        if _expired(t0):\n"
        "            res['read_action']='pulse_l'\n"
        f"            res['read_pin']={read_pin_name!r}\n"
        "            res['timeout']=True\n"
        "            res['timeout_ms']=timeout_ms\n"
        "            res['value']=rp.value()\n"
        "            print(json.dumps(res))\n"
        "            raise SystemExit\n"
        "        time.sleep_us(50)\n"
        "    res['read_action']='pulse_l'\n"
        f"    res['read_pin']={read_pin_name!r}\n"
        "    res['pulse_us']=time.ticks_diff(time.ticks_us(),t1)\n"
        "    res['value']=rp.value()\n"
        "print(json.dumps(res))"
    )


def _print_gpio_help() -> None:
    help_text = """\
GPIO read/write/sequence command for a single pin.

[bold cyan]Usage:[/bold cyan]
  replx gpio [yellow]SUBCOMMAND[/yellow] ...

[bold cyan]Subcommands:[/bold cyan]
  [green]read[/green]   Read one GPIO in input mode.
  [green]write[/green]  Drive one GPIO in output mode.
  [green]seq[/green]    Execute a write+delay sequence with an optional read_action.

[bold cyan]Pin Format:[/bold cyan]
  [yellow]GP<num>[/yellow] only, case-insensitive. Example: [cyan]GP1[/cyan], [cyan]gp26[/cyan]

[bold cyan]Read:[/bold cyan]
  replx gpio read [yellow]GP<num>[/yellow] [green]--repeat N[/green] [green]--edge MODE[/green]
  [dim]--repeat=1[/dim]   read once [dim](default)[/dim]
  [dim]--repeat>1[/dim]   read N times in one board call
  [dim]--repeat=0[/dim]   continuous watch until Ctrl+C [dim](prints only on state change)[/dim]
  [dim]--edge MODE[/dim]  edge watch filter: [cyan]rising[/cyan], [cyan]falling[/cyan], [cyan]both[/cyan]
               [dim]When --edge is used, repeat defaults to 0 and any nonzero repeat is invalid.[/dim]

[bold cyan]Write:[/bold cyan]
  replx gpio write [yellow]GP<num>[/yellow] [yellow]0|1[/yellow]

[bold cyan]Sequence:[/bold cyan]
  replx gpio seq [yellow]GP<num>[/yellow] [cyan]TOKEN[/cyan]... [[magenta]read_action[/magenta]]
  replx gpio seq [yellow]WRITE_GP[/yellow] [cyan]TOKEN[/cyan]... [[yellow]READ_GP[/yellow] [magenta]read_action[/magenta]]
                             [[green]--expr EXPR[/green]] [[green]--timeout MS[/green]]

  [bold]Write Tokens[/bold]
    [cyan]0[/cyan] [cyan]1[/cyan]      drive low/high
    [cyan]u<N>[/cyan]     delay in microseconds
    [cyan]m<N>[/cyan]     delay in milliseconds

  [bold]read_action[/bold] [dim](optional, final token only)[/dim]
    [magenta]read[/magenta]     read final pin value
    [magenta]wait_h[/magenta]   wait until value becomes 1
    [magenta]wait_l[/magenta]   wait until value becomes 0
    [magenta]pulse_h[/magenta]  wait for high pulse and report width
    [magenta]pulse_l[/magenta]  wait for low pulse and report width

    [dim]If only read_action is given, the write pin is also used as the read pin.[/dim]
    [dim]If READ_GP read_action is given, the write pin and read pin can be different.[/dim]
    [dim]wait_* and pulse_* honor --timeout in ms. Use 0 for infinite wait.[/dim]
    [dim]--expr can use: value, writes, pulse_us, wait_ms and functions abs, round, min, max.[/dim]

[bold cyan]Examples:[/bold cyan]
  replx COM3 gpio read GP1
  replx COM3 gpio read GP1 --repeat 8
  replx COM3 gpio read GP1 --repeat 0
  replx COM3 gpio read GP1 --edge rising
  replx COM3 gpio read GP1 --edge both
  replx COM3 gpio write GP1 1
  replx COM3 gpio seq GP20 1 u10 0 pulse_h
  replx COM3 gpio seq GP20 0 u5 1 u10 0 GP21 pulse_h
    replx COM3 gpio seq GP20 0 u5 1 u10 0 GP21 pulse_h --timeout 100
  replx COM3 gpio seq GP20 0 u5 1 u10 0 GP21 pulse_h --expr "pulse_us/58"
  replx COM3 gpio seq GP1 1 u50 0 m1 1 read
  replx COM3 gpio seq GP1 1 u10 0 wait_h
  replx COM3 gpio seq GP1 0 u10 1 pulse_l

[bold cyan]Notes:[/bold cyan]
  • One GPIO only.
  • No GPIO scope mode.
  • [yellow]gpio read[/yellow] supports only [yellow]--repeat[/yellow] and [yellow]--edge[/yellow].
  • [yellow]seq[/yellow] requires at least one write token."""
    OutputHelper.print_panel(help_text, title="gpio", border_style="dim")


def _subcmd_read(client, pos_args: list[str], repeat: int, edge: Optional[str]) -> None:
    if len(pos_args) != 1:
        raise ValueError("Usage: replx gpio read GP<num> [--repeat N] [--edge MODE]")

    if repeat < 0:
        raise ValueError("--repeat must be >= 0")

    pin_no, pin_name = _parse_gp(pos_args[0])

    if edge is not None and repeat != 0:
        raise ValueError("--edge requires --repeat 0. When --edge is specified, nonzero repeat is not allowed")

    if repeat == 0:
        if edge is None:
            edge = 'both'
        OutputHelper.print_panel(
            f"Watching [bright_green]{pin_name}[/bright_green]. Press Ctrl+C to stop.\n"
            f"Only matching edges are printed as: [cyan]<ticks_ms> {edge}[/cyan]",
            title="GPIO Watch",
            border_style="green",
        )
        code = _make_watch_code(pin_no, edge)
        raw = _run_interactive_script(client, code, live_output=True)
        if raw and not raw.endswith('\n'):
            print()
        return

    raw = _exec(client, _make_read_code(pin_no, pin_name, repeat), timeout=min(30.0, 2.0 + repeat * 0.01))
    data = _parse_json_strict(raw)
    values = [int(v) for v in data.get('values', [])]

    if repeat == 1:
        OutputHelper.print_panel(
            f"[bright_green]{pin_name}[/bright_green] = [bright_cyan]{values[0]}[/bright_cyan]",
            title="GPIO Read",
            border_style="green",
        )
        return

    OutputHelper.print_panel(
        f"Pin: [bright_green]{pin_name}[/bright_green]\n"
        f"Samples: [bright_cyan]{len(values)}[/bright_cyan]\n"
        f"Values:\n{_format_values(values)}",
        title="GPIO Read",
        border_style="green",
    )


def _subcmd_write(client, pos_args: list[str]) -> None:
    if len(pos_args) != 2:
        raise ValueError("Usage: replx gpio write GP<num> 0|1")

    pin_no, pin_name = _parse_gp(pos_args[0])
    value = _parse_logic(pos_args[1])
    raw = _exec(client, _make_write_code(pin_no, pin_name, value), timeout=5.0)
    data = _parse_json_strict(raw)

    OutputHelper.print_panel(
        f"[bright_green]{pin_name}[/bright_green] <= [bright_cyan]{int(data.get('value', value))}[/bright_cyan]",
        title="GPIO Write",
        border_style="green",
    )


def _subcmd_seq(client, pos_args: list[str], expr: Optional[str], timeout_ms: int) -> None:
    if len(pos_args) < 2:
        raise ValueError("Usage: replx gpio seq GP<num> TOKEN... [read_action]")
    if timeout_ms < 0:
        raise ValueError("--timeout must be >= 0")

    write_pin_no, write_pin_name = _parse_gp(pos_args[0])
    ops, read_pin_no, read_action, read_pin_name = _parse_seq_tokens(pos_args[1:], write_pin_name)
    code = _make_seq_code(write_pin_no, write_pin_name, ops, read_pin_no, read_pin_name, read_action, timeout_ms)

    if read_action in {'wait_h', 'wait_l', 'pulse_h', 'pulse_l'}:
        raw = _run_interactive_script(client, code, live_output=False)
    else:
        timeout = 10.0 + sum(value / 1000.0 for kind, value in ops if kind == 'm') + sum(value / 1_000_000.0 for kind, value in ops if kind == 'u')
        raw = _exec(client, code, timeout=min(max(timeout, 5.0), 120.0))

    data = _parse_json_strict(raw)
    lines = [
        f"Write pin: [bright_green]{data.get('write_pin', write_pin_name)}[/bright_green]",
        f"Seq: [bright_cyan]{_format_seq_ops(ops)}[/bright_cyan]",
    ]
    if read_action is None:
        lines.append(f"Final value: [bright_cyan]{int(data.get('value', 0))}[/bright_cyan]")
    if read_action:
        lines.append(f"Read pin: [bright_green]{data.get('read_pin', read_pin_name)}[/bright_green]")
        lines.append(f"Action: [magenta]{read_action}[/magenta]")
    if data.get('timeout'):
        lines.append(f"Timeout: [bright_red]{int(data.get('timeout_ms', timeout_ms))} ms[/bright_red]")
    if 'wait_ms' in data:
        lines.append(f"Wait: [bright_cyan]{int(data['wait_ms'])} ms[/bright_cyan]")
    if 'pulse_us' in data:
        lines.append(f"Pulse: [bright_cyan]{int(data['pulse_us'])} us[/bright_cyan]")
    if expr is not None and not data.get('timeout'):
        result = _eval_expr(expr, data)
        lines.append(f"Expr: [magenta]{expr}[/magenta] = [bright_cyan]{result}[/bright_cyan]")

    OutputHelper.print_panel('\n'.join(lines), title="GPIO Seq", border_style="green")


@app.command(name="gpio", rich_help_panel="Hardware")
def gpio_cmd(
    args: Optional[list[str]] = typer.Argument(
        None, help="Subcommand: read  write  seq"
    ),
    repeat: int = typer.Option(1, "--repeat", "-n", metavar="N", help="Repeat count for gpio read (0=infinite)"),
    edge: Optional[str] = typer.Option(None, "--edge", metavar="MODE", help="Edge watch mode for gpio read: rising, falling, both"),
    expr: Optional[str] = typer.Option(None, "--expr", metavar="EXPR", help="Post-process gpio seq result with an arithmetic expression"),
    timeout: int = typer.Option(100, "--timeout", metavar="MS", help="Timeout in ms for gpio seq wait/pulse actions (0=infinite)"),
    show_help: bool = typer.Option(False, "--help", "-h", is_eager=True, hidden=True),
):
    if show_help or not args:
        _print_gpio_help()
        raise typer.Exit()

    subcmd = args[0].lower()
    pos_args = args[1:]

    if subcmd not in ('read', 'write', 'seq'):
        OutputHelper.print_panel(
            f"Unknown subcommand: {subcmd!r}\n\n"
            "Valid subcommands: read  write  seq",
            title="GPIO Error",
            border_style="red",
        )
        raise typer.Exit(1)

    if subcmd != 'read' and repeat != 1:
        OutputHelper.print_panel(
            "--repeat is supported only for gpio read",
            title="GPIO Error",
            border_style="red",
        )
        raise typer.Exit(1)

    if subcmd != 'read' and edge is not None:
        OutputHelper.print_panel(
            "--edge is supported only for gpio read",
            title="GPIO Error",
            border_style="red",
        )
        raise typer.Exit(1)

    if subcmd != 'seq' and expr is not None:
        OutputHelper.print_panel(
            "--expr is supported only for gpio seq",
            title="GPIO Error",
            border_style="red",
        )
        raise typer.Exit(1)

    if subcmd != 'seq' and timeout != 100:
        OutputHelper.print_panel(
            "--timeout is supported only for gpio seq",
            title="GPIO Error",
            border_style="red",
        )
        raise typer.Exit(1)

    _ensure_connected()

    try:
        edge = _parse_edge(edge)
        if subcmd == 'read' and edge is not None and not _is_repeat_explicit():
            repeat = 0

        with _create_agent_client() as client:
            if subcmd == 'read':
                _subcmd_read(client, pos_args, repeat, edge)
            elif subcmd == 'write':
                _subcmd_write(client, pos_args)
            elif subcmd == 'seq':
                _subcmd_seq(client, pos_args, expr, timeout)
    except ValueError as e:
        OutputHelper.print_panel(str(e), title="GPIO Error", border_style="red")
        raise typer.Exit(1)
