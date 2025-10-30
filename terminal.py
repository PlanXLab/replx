"""
Terminal I/O utilities for replx.
"""
import os
import sys
import platform
import threading
from typing import Callable


# Platform detection
IS_WINDOWS: bool = platform.system() == "Windows"
CR, LF = b"\r", b"\n"

# Output buffer management
_stdout_lock = threading.Lock()
_buffer = b''
_expected_bytes = 0

_error_header = b"Traceback (most recent call last):"
_error_header_buf = b""
_skip_error_output = False

_OUTBUF_MAX = 8192
_outbuf = bytearray()


def flush_outbuf():
    """Flush the output buffer."""
    global _outbuf
    if _outbuf:
        sys.stdout.buffer.write(_outbuf)
        sys.stdout.buffer.flush()
        _outbuf.clear()


def stdout_write_bytes(b):
    """Write bytes to stdout with buffering and UTF-8 handling."""
    global _buffer, _expected_bytes
    global _error_header_buf, _skip_error_output
    global _outbuf

    if _skip_error_output or not b:
        return

    _error_header_buf = (_error_header_buf + b)[-len(_error_header):]
    if _error_header_buf == _error_header:
        _skip_error_output = True
        return

    if b'\x04' in b:
        b = b.replace(b'\x04', b'')
        if not b:
            return

    with _stdout_lock:
        mv = memoryview(b)
        i = 0
        while i < len(mv):
            ch = mv[i]
            if _expected_bytes:
                take = min(_expected_bytes, len(mv) - i)
                _buffer += mv[i:i+take].tobytes()
                _expected_bytes -= take
                i += take
                if _expected_bytes == 0:
                    _outbuf.extend(_buffer)
                    _buffer = b''
                    if _outbuf.endswith(b'\n') or len(_outbuf) >= _OUTBUF_MAX:
                        flush_outbuf()
                continue

            if ch <= 0x7F:
                j = i + 1
                ln = len(mv)
                while j < ln and mv[j] <= 0x7F:
                    j += 1
                _outbuf.extend(mv[i:j])
                i = j
                if _outbuf.endswith(b'\n') or len(_outbuf) >= _OUTBUF_MAX:
                    flush_outbuf()
                continue

            hdr = ch
            if   (hdr & 0xF8) == 0xF0: need = 3
            elif (hdr & 0xF0) == 0xE0: need = 2
            elif (hdr & 0xE0) == 0xC0: need = 1
            else:
                _outbuf.extend(bytes([hdr]).hex().encode())
                if len(_outbuf) >= _OUTBUF_MAX:
                    flush_outbuf()
                i += 1
                continue

            _buffer = bytes([hdr])
            i += 1
            _expected_bytes = need


_EXTMAP: dict[str, bytes] = {
    "H": b"\x1b[A",   # ↑
    "P": b"\x1b[B",   # ↓
    "M": b"\x1b[C",   # →
    "K": b"\x1b[D",   # ←
    "G": b"\x1b[H",   # Home
    "O": b"\x1b[F",   # End
    "R": b"\x1b[2~",  # Ins
    "S": b"\x1b[3~",  # Del
}


def utf8_need_follow(b0: int) -> int:
    """Calculate number of UTF-8 continuation bytes needed."""
    if b0 & 0b1000_0000 == 0:              # 0xxxxxxx → ASCII
        return 0
    if b0 & 0b1110_0000 == 0b1100_0000:    # 110xxxxx
        return 1
    if b0 & 0b1111_0000 == 0b1110_0000:    # 1110xxxx
        return 2
    if b0 & 0b1111_1000 == 0b1111_0000:    # 11110xxx
        return 3
    return 0


# Active UPY instance for SIGINT handling
_ACTIVE_UPY_FOR_SIGINT = None

if IS_WINDOWS:
    import signal
    import msvcrt

    def win_sigint_handler(signum, frame):
        """Handle SIGINT on Windows."""
        try:
            if _ACTIVE_UPY_FOR_SIGINT is not None:
                _ACTIVE_UPY_FOR_SIGINT.request_interrupt()
        finally:
            pass

    try:
        signal.signal(signal.SIGINT, win_sigint_handler)
    except Exception:
        pass

    def getch() -> bytes:
        """Get a character from stdin (Windows)."""
        w = msvcrt.getwch()
        if w in ("\x00", "\xe0"):  # arrow keys etc.
            return _EXTMAP.get(msvcrt.getwch(), b"")
        return w.encode("utf-8")

    _PUTB: Callable[[bytes], None] = msvcrt.putch
    _PUTW: Callable[[str], None] = msvcrt.putwch

    def write_bytes(data: bytes) -> None:
        """Write bytes to stdout (helper)."""
        sys.stdout.buffer.write(data)
        sys.stdout.flush()

    def putch(data: bytes) -> None:
        """Put character to stdout (Windows)."""
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
    # Unix/Linux/macOS
    import tty
    import termios
    import atexit
    import signal

    _FD = sys.stdin.fileno()
    _OLD = None
    _RAW_MODE_ACTIVE = False

    def initialize_terminal():
        """Initialize terminal settings."""
        global _OLD
        if _OLD is None:
            _OLD = termios.tcgetattr(_FD)

    def raw_mode(on: bool):
        """Set terminal raw mode on/off."""
        global _RAW_MODE_ACTIVE
        try:
            if on:
                initialize_terminal()
                tty.setraw(_FD)
                _RAW_MODE_ACTIVE = True
            else:
                if _OLD is not None:
                    termios.tcsetattr(_FD, termios.TCSADRAIN, _OLD)
                _RAW_MODE_ACTIVE = False
        except Exception:
            pass

    def restore_terminal():
        """Restore terminal to normal mode."""
        if _RAW_MODE_ACTIVE:
            raw_mode(False)

    def signal_handler(signum, frame):
        """Handle signals."""
        restore_terminal()
        sys.exit(0)

    atexit.register(restore_terminal)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    def getch() -> bytes:
        """Get a character from stdin (Unix)."""
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
        """Put character to stdout (Unix)."""
        if data != CR:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()


def set_active_upy_for_sigint(upy_instance):
    """Set the active UPY instance for SIGINT handling."""
    global _ACTIVE_UPY_FOR_SIGINT
    _ACTIVE_UPY_FOR_SIGINT = upy_instance
