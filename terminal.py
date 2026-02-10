import os
import sys
import platform
import threading
from typing import Callable

from .utils.constants import EOF_MARKER

IS_WINDOWS: bool = platform.system() == "Windows"
CR, LF = b"\r", b"\n"

_stdout_lock = threading.Lock()
_thread_local = threading.local()
_OUTBUF_MAX = 8192


def _get_thread_state():
    if not hasattr(_thread_local, 'buffer'):
        _thread_local.buffer = b''
        _thread_local.expected_bytes = 0
        _thread_local.outbuf = bytearray()
    return _thread_local


def flush_outbuf():
    state = _get_thread_state()
    if state.outbuf:
        with _stdout_lock:
            sys.stdout.buffer.write(state.outbuf)
            sys.stdout.buffer.flush()
        state.outbuf.clear()


def stdout_write_bytes(b, skip_error_filter=False):
    if not b:
        return

    if EOF_MARKER in b:
        b = b.replace(EOF_MARKER, b'')
        if not b:
            return

    state = _get_thread_state()

    mv = memoryview(b)
    i = 0
    while i < len(mv):
        ch = mv[i]
        if state.expected_bytes:
            take = min(state.expected_bytes, len(mv) - i)
            state.buffer += mv[i:i+take].tobytes()
            state.expected_bytes -= take
            i += take
            if state.expected_bytes == 0:
                state.outbuf.extend(state.buffer)
                state.buffer = b''
                if state.outbuf.endswith(b'\n') or len(state.outbuf) >= _OUTBUF_MAX:
                    flush_outbuf()
            continue

        if ch <= 0x7F:
            j = i + 1
            ln = len(mv)
            while j < ln and mv[j] <= 0x7F:
                j += 1
            state.outbuf.extend(mv[i:j])
            i = j
            if state.outbuf.endswith(b'\n') or len(state.outbuf) >= _OUTBUF_MAX:
                flush_outbuf()
            continue

        hdr = ch
        if   (hdr & 0xF8) == 0xF0: need = 3
        elif (hdr & 0xF0) == 0xE0: need = 2
        elif (hdr & 0xE0) == 0xC0: need = 1
        else:
            state.outbuf.extend(bytes([hdr]).hex().encode())
            if len(state.outbuf) >= _OUTBUF_MAX:
                flush_outbuf()
            i += 1
            continue

        state.buffer = bytes([hdr])
        i += 1
        state.expected_bytes = need


_EXTMAP: dict[str, bytes] = {
    "H": b"\x1b[A",   # Up
    "P": b"\x1b[B",   # Down
    "M": b"\x1b[C",   # Right
    "K": b"\x1b[D",   # Left
    "G": b"\x1b[H",   # Home
    "O": b"\x1b[F",   # End
    "R": b"\x1b[2~",  # Ins
    "S": b"\x1b[3~",  # Del
}


def utf8_need_follow(b0: int) -> int:
    if b0 & 0b1000_0000 == 0:
        return 0
    if b0 & 0b1110_0000 == 0b1100_0000:
        return 1
    if b0 & 0b1111_0000 == 0b1110_0000:
        return 2
    if b0 & 0b1111_1000 == 0b1111_0000:
        return 3
    return 0


if IS_WINDOWS:
    import msvcrt

    def kbhit() -> bool:
        return msvcrt.kbhit()

    def getch_nonblock() -> bytes | None:
        if not msvcrt.kbhit():
            return None
        w = msvcrt.getwch()
        if w in ("\x00", "\xe0"):
            ext_key = msvcrt.getwch()
            return _EXTMAP.get(ext_key, b"")
        return w.encode("utf-8")

    def getch() -> bytes:
        w = msvcrt.getwch()
        if w in ("\x00", "\xe0"):
            return _EXTMAP.get(msvcrt.getwch(), b"")
        return w.encode("utf-8")

    _PUTB: Callable[[bytes], None] = msvcrt.putch
    _PUTW: Callable[[str], None] = msvcrt.putwch

    def write_bytes(data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()

    def putch(data: bytes) -> None:
        if data == CR:
            _PUTB(LF)
            return

        if len(data) > 1 and data.startswith(b"\x1b["):
            write_bytes(data)
        elif len(data) == 1 and data < b"\x80":
            _PUTB(data)
        else:
            _PUTW(data.decode("utf-8", "strict"))

else:
    import tty
    import termios
    import atexit
    import signal

    _FD = sys.stdin.fileno()

    _terminal_state = threading.local()
    _terminal_lock = threading.Lock()

    def _get_terminal_state():
        if not hasattr(_terminal_state, 'old_settings'):
            _terminal_state.old_settings = None
            _terminal_state.raw_mode_active = False
        return _terminal_state

    def initialize_terminal():
        state = _get_terminal_state()
        if state.old_settings is None:
            try:
                with _terminal_lock:
                    state.old_settings = termios.tcgetattr(_FD)
            except Exception:
                pass

    def raw_mode(on: bool):
        state = _get_terminal_state()
        try:
            if on:
                with _terminal_lock:
                    initialize_terminal()
                    tty.setraw(_FD)
                state.raw_mode_active = True
            else:
                with _terminal_lock:
                    if state.old_settings is not None:
                        termios.tcsetattr(_FD, termios.TCSADRAIN, state.old_settings)
                state.raw_mode_active = False
        except Exception:
            pass

    def restore_terminal():
        state = _get_terminal_state()
        if state.raw_mode_active:
            raw_mode(False)

    def signal_handler(signum, frame):
        restore_terminal()
        raise KeyboardInterrupt()

    atexit.register(restore_terminal)
    signal.signal(signal.SIGTERM, signal_handler)

    def kbhit() -> bool:
        import select
        r, _, _ = select.select([sys.stdin], [], [], 0)
        return bool(r)

    def getch_nonblock() -> bytes | None:
        """Non-blocking keyboard input for Unix."""
        import select
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            return None
        try:
            raw_mode(True)
            first = os.read(_FD, 1)
            if not first:
                return None
            need = utf8_need_follow(first[0])
            return first + (os.read(_FD, need) if need else b"")
        except (OSError, IOError):
            return None
        finally:
            raw_mode(False)

    def getch() -> bytes:
        try:
            raw_mode(True)
            first = os.read(_FD, 1)
            need = utf8_need_follow(first[0])
            return first + (os.read(_FD, need) if need else b"")
        except Exception:
            return b""
        finally:
            raw_mode(False)

    def putch(data: bytes) -> None:
        if data != CR:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()



