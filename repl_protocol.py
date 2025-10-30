"""
MicroPython REPL Protocol implementation.
Handles low-level serial communication and REPL interaction.
"""
import os
import sys
import time
import struct
import textwrap
import threading
from typing import Callable, Optional, Tuple

import typer
import serial

try:
    import msvcrt
except ImportError:
    msvcrt = None

from .exceptions import ReplxError
from .helpers import DebugHelper
from .terminal import (
    IS_WINDOWS, CR, LF,
    flush_outbuf as _flush_outbuf,
    stdout_write_bytes as _stdout_write_bytes,
    getch, putch,
    utf8_need_follow as _utf8_need_follow,
    _EXTMAP,
    set_active_upy_for_sigint
)

# Global variables referenced by ReplProtocol
_skip_error_output = False
_error_header_buf = b""


class ReplProtocol:
    """
    Handles MicroPython REPL (Read-Eval-Print Loop) communication protocol.
    This class manages low-level serial communication, command execution, and REPL mode interaction.
    Implements the official MicroPython REPL protocol including Raw-Paste Mode for efficient code transfer.
    Reference: https://docs.micropython.org/en/latest/reference/repl.html
    """
    _REPL_BUFSIZE = 2048  # minimum 256
    
    # REPL Control Sequences (Official MicroPython Protocol)
    _CTRL_A = b'\x01'  # Enter raw REPL mode
    _CTRL_B = b'\x02'  # Exit raw REPL mode (friendly REPL)
    _CTRL_C = b'\x03'  # Interrupt/KeyboardInterrupt
    _CTRL_D = b'\x04'  # Soft reset / EOF marker
    _CTRL_E = b'\x05'  # Paste mode toggle / Raw-paste mode entry
    
    _EOF_MARKER = b'\x04'
    _RAW_REPL_PROMPT = b'raw REPL; CTRL-B to exit\r\n>'
    _SOFT_REBOOT_MSG = b'soft reboot\r\n'
    _OK_RESPONSE = b'OK'
    
    # Raw-Paste Mode Protocol (MicroPython v1.13+)
    _RAW_PASTE_INIT = b'\x05A\x01'  # Ctrl-E + 'A' + Ctrl-A to enter raw-paste
    _RAW_PASTE_SUPPORTED = b'R\x01'  # Device supports raw-paste mode
    _RAW_PASTE_NOT_SUPPORTED = b'R\x00'  # Device doesn't support raw-paste
    _RAW_PASTE_FALLBACK = b'raw REPL; CTRL-B'  # Legacy response (no raw-paste)
    _RAW_PASTE_WINDOW_INC = b'\x01'  # Flow control: increase window size
    _RAW_PASTE_END_DATA = b'\x04'  # Flow control: device wants to end reception
    
    def __init__(self, port:str, baudrate:int=115200, core:str="RP2350", device_root_fs:str="/"):
        """
        Initialize the REPL protocol handler.
        :param port: The serial port to connect to.
        :param baudrate: The baud rate for the serial connection (default is 115200).
        :param core: The core type of the device (e.g., "RP2350", "ESP32", "EFR32MG").
        :param device_root_fs: The root filesystem path on the device.
        :raises ReplxError: If the serial port cannot be opened or if the device is not found.
        """
        try:
            self.serial = serial.Serial(port, baudrate, timeout=1.0, write_timeout=1.0)
            self._stop_event = threading.Event()
            self._follow_thread = None
            DebugHelper.log(f"Serial open ok port={port} baud={baudrate}")
        except serial.SerialException as e:
            DebugHelper.log(f"Serial open failed port={port}: {e}")
            raise ReplxError(f"failed to open {port} ({e})")
        except (OSError, IOError): 
            DebugHelper.log(f"Serial open failed port={port}: device not found")
            raise ReplxError(f"failed to open {port} (device not found)")
        
        self._rx_pushback = bytearray()
        self._raw_paste_supported = None  # None=unknown, True=supported, False=not supported
        self._raw_paste_window_size = 128  # Default window size increment
        
        # Device-specific attributes
        self.core = core
        self.device_root_fs = device_root_fs
        self._DEVICE_CHUNK_SIZES = 4096
        self._PUT_BATCH_BYTES = 16 * 1024
        
        self._init_repl()

    def _init_repl(self):
        """
        Initialize the REPL (Read-Eval-Print Loop) for the Replx.
        This function sets up the serial connection and prepares the board for REPL interaction.
        """
        self.serial_reader_running = None
        self.serial_out_put_enable = True
        self.serial_out_put_count = 0
        self._interrupt_requested = False

    def _write(self, data:bytes) -> None:
        """
        Write data to the serial port.
        :param data: The data to write to the serial port.
        """
        n = self.serial.write(data)
        if DebugHelper.enabled():
            DebugHelper.log(f"TX {n}B  head={data[:8]!r}{'...' if len(data)>8 else ''}")

    def _read(self, n:int=1) -> bytes:
        """
        Read a specified number of bytes from the serial port.
        :param n: Number of bytes to read from the serial port.
        :return: The bytes read from the serial port.
        """
        if self._rx_pushback:
            if len(self._rx_pushback) <= n:
                b = bytes(self._rx_pushback)
                self._rx_pushback.clear()
                rem = n - len(b)
                if rem > 0:
                    b += self.serial.read(rem)
            else:
                b = bytes(self._rx_pushback[:n])
                del self._rx_pushback[:n]
        else:
            b = self.serial.read(n)

        if DebugHelper.enabled() and b:
            DebugHelper.log(f"RX {len(b)}B  head={b[:8]!r}{'...' if len(b)>8 else ''}")
        return b
    
    def _read_ex(self, min_num_bytes:int, ending:bytes, timeout:int=5,
                data_consumer:Optional[Callable[[bytes], None]]=None) -> bytes:
        data = bytearray()
        start = time.time()
        deadline = (start + timeout) if timeout > 0 else None

        keep_tail_only = (timeout == 0 and data_consumer is not None)
        tail_keep = max(16, len(ending) + 8)

        pat = ending
        m = len(pat)
        pi = [0] * m
        k = 0
        for i in range(1, m):
            while k > 0 and pat[k] != pat[i]:
                k = pi[k-1]
            if pat[k] == pat[i]:
                k += 1
            pi[i] = k

        matched = 0

        def _feed_chunk(chunk: bytes) -> int:
            nonlocal matched, data

            if not chunk:
                return -1

            end_at = -1
            for idx, b in enumerate(chunk):
                while matched > 0 and pat[matched] != b:
                    matched = pi[matched - 1]
                if pat[matched] == b:
                    matched += 1
                    if matched == m:
                        end_at = idx
                        break

            if end_at >= 0:
                consume_part = chunk[:end_at+1]
                data += consume_part
                if data_consumer:
                    try:
                        data_consumer(consume_part)
                    except Exception:
                        pass
                if keep_tail_only and len(data) > tail_keep:
                    data = data[-tail_keep:]
                return end_at + 1

            data += chunk
            if data_consumer:
                try:
                    data_consumer(chunk)
                except Exception:
                    pass
            if keep_tail_only and len(data) > tail_keep:
                data = data[-tail_keep:]
            return -1

        def _read_some(max_n: int) -> bytes:
            return self._read(max(1, max_n))

        try:
            DebugHelper.log(f"_read_ex start min={min_num_bytes} end={ending!r} timeout={timeout}")

            if min_num_bytes > 0:
                chunk = _read_some(min_num_bytes)
                pos = _feed_chunk(chunk)
                if pos >= 0:
                    tail = chunk[pos:]
                    if tail:
                        self._rx_pushback[:] = tail + self._rx_pushback
                    DebugHelper.log(f"_read_ex done total={len(data)} match=True")
                    return bytes(data)

            last_activity = time.time()

            while True:
                if self._stop_event.is_set():
                    DebugHelper.log("_read_ex stop_event set")
                    break
                
                if deadline is not None and time.time() >= deadline:
                    DebugHelper.log("_read_ex timeout reached")
                    break

                waiting = self.serial.in_waiting
                if waiting <= 0 and len(self._rx_pushback) > 0:
                    waiting = 1

                if waiting > 0:
                    want = 1 if matched > 0 else min(256, waiting)  # 128~512
                    chunk = _read_some(want)
                    if chunk:
                        pos = _feed_chunk(chunk)
                        last_activity = time.time()
                        if pos >= 0:
                            tail = chunk[pos:]
                            if tail:
                                self._rx_pushback[:] = tail + self._rx_pushback
                            DebugHelper.log(f"_read_ex done total={len(data)} match=True")
                            return bytes(data)
                    else:
                        time.sleep(0.001) # 0.002
                else:
                    time.sleep(0.001) # 0.004
                    if timeout > 0 and (time.time() - last_activity) > max(timeout * 2, 10):
                        DebugHelper.log("_read_ex idle-timeout; breaking")
                        break

            DebugHelper.log(f"_read_ex done total={len(data)} match={data.endswith(ending)}")
            return bytes(data)

        except serial.SerialException as e:
            if not self._stop_event.is_set():
                raise ReplxError(f"Serial communication error: {e}")
            return bytes(data)
        except ReplxError:
            raise
        except Exception:
            return bytes(data)


    def _enter_repl(self, soft_reset:bool=True):
        """
        Enter the raw REPL mode of the device.
        This function sends the necessary commands to the device to enter the raw REPL mode.
        :param soft_reset: If True, perform a soft reset before entering the raw REPL.
        """
        DebugHelper.log("enter_repl: start")
        for attempt in (1, 2):
            try:
                self.serial.reset_input_buffer()
            except Exception:
                pass

            self.serial.write(b'\r' + self._CTRL_C + self._CTRL_C)
            time.sleep(0.05)
            try:
                self.serial.reset_input_buffer()
            except Exception:
                pass
            
            self.serial.write(b'\r' + self._CTRL_A)

            try:
                if soft_reset:
                    data = self._read_ex(1, self._RAW_REPL_PROMPT, timeout=3)
                    if not data.endswith(self._RAW_REPL_PROMPT):
                        DebugHelper.log(f"enter_repl[{attempt}]: pre raw-prompt miss")
                        raise ReplxError('could not enter raw repl')

                    self.serial.write(self._CTRL_D)
                    data = self._read_ex(1, self._SOFT_REBOOT_MSG, timeout=3)
                    if not data.endswith(self._SOFT_REBOOT_MSG):
                        DebugHelper.log(f"enter_repl[{attempt}]: soft reboot miss")
                        raise ReplxError('could not enter raw repl')

                data = self._read_ex(1, self._RAW_REPL_PROMPT[:-1], timeout=3)
                if not data.endswith(self._RAW_REPL_PROMPT[:-1]):
                    DebugHelper.log(f"enter_repl[{attempt}]: final raw-prompt miss")
                    raise ReplxError('could not enter raw repl')
                DebugHelper.log("enter_repl: OK")
                return
            except ReplxError:
                DebugHelper.log(f"enter_repl[{attempt}]: retry path")
                try:
                    self.serial.write(b'\r' + self._CTRL_B)  # friendly
                    time.sleep(0.1)
                except Exception:
                    pass
                time.sleep(0.12)
                continue
            
        raise ReplxError('could not enter raw repl')

    def _leave_repl(self):
        """
        Leave the raw REPL mode of the device.
        """
        self.serial.write(b'\r' + self._CTRL_B)  # enter friendly REPL
        DebugHelper.log("_leave_repl")

    def _enter_raw_paste_mode(self) -> bool:
        """
        Attempt to enter raw-paste mode within raw REPL.
        Returns True if raw-paste mode is supported and entered successfully, False otherwise.
        
        Protocol (from MicroPython official docs):
        1. Already in raw REPL mode (via Ctrl-A)
        2. Send b'\x05A\x01' (Ctrl-E + 'A' + Ctrl-A)
        3. Read 2 bytes response:
           - b'R\x01' = raw-paste supported, entered successfully
           - b'R\x00' = device understands but doesn't support
           - b'ra' = old device, doesn't understand (read rest of prompt and discard)
        4. If b'R\x01', read 2 more bytes as little-endian window size increment
        """
        if self._raw_paste_supported is not None:
            return self._raw_paste_supported
        
        DebugHelper.log("enter_raw_paste_mode: attempting to enter")
        
        try:
            # Step 2: Send raw-paste initialization sequence
            self.serial.write(self._RAW_PASTE_INIT)
            DebugHelper.log(f"enter_raw_paste_mode: sent {self._RAW_PASTE_INIT!r}")
            
            # Step 3: Read 2-byte response
            response = self._read(2)
            DebugHelper.log(f"enter_raw_paste_mode: response={response!r}")
            
            if response == self._RAW_PASTE_SUPPORTED:
                # Step 4: Read window size increment (2 bytes, little-endian uint16)
                window_bytes = self._read(2)
                if len(window_bytes) == 2:
                    self._raw_paste_window_size = struct.unpack('<H', window_bytes)[0]
                    DebugHelper.log(f"enter_raw_paste_mode: SUCCESS window_size={self._raw_paste_window_size}")
                    self._raw_paste_supported = True
                    return True
                else:
                    DebugHelper.log("enter_raw_paste_mode: FAIL - couldn't read window size")
                    self._raw_paste_supported = False
                    return False
                    
            elif response == self._RAW_PASTE_NOT_SUPPORTED:
                DebugHelper.log("enter_raw_paste_mode: device responded R\\x00 (understands but not supported)")
                self._raw_paste_supported = False
                return False
                
            elif response.startswith(b'r'):
                # Legacy device - read and discard rest of prompt
                DebugHelper.log("enter_raw_paste_mode: legacy device (no raw-paste support)")
                # Try to read the rest: 'aw REPL; CTRL-B to exit\r\n>'
                try:
                    rest = self._read_ex(1, b'>', timeout=1)
                    DebugHelper.log(f"enter_raw_paste_mode: discarded legacy prompt (len={len(rest)})")
                except Exception:
                    pass
                self._raw_paste_supported = False
                return False
                
            else:
                DebugHelper.log(f"enter_raw_paste_mode: unexpected response {response!r}")
                self._raw_paste_supported = False
                return False
                
        except Exception as e:
            DebugHelper.log(f"enter_raw_paste_mode: exception {e}")
            self._raw_paste_supported = False
            return False

    def _follow_task(self, echo: bool):
        try:
            while not self._stop_event.is_set():
                try:
                    if IS_WINDOWS:
                        if msvcrt.kbhit():
                            w = msvcrt.getwch()
                            if w in ("\x03",):  # Ctrl+C
                                self.request_interrupt()
                                time.sleep(0.1)
                                return
                            if w in ("\x04",):   # Ctrl+D
                                try: 
                                    self.serial.write(self._CTRL_D)
                                except: 
                                    pass
                                time.sleep(0.08)
                                return
                            
                            # Handle extended keys (arrows, Del, etc.) in Windows
                            if w in ("\x00", "\xe0"):  # Extended key prefix
                                ext_key = msvcrt.getwch()
                                ch = _EXTMAP.get(ext_key, b"")
                                if not ch:  # Unknown extended key, skip
                                    time.sleep(0.005)
                                    continue
                                # Echo ON: Display ANSI escape sequence on PC
                                if echo:
                                    putch(ch)
                            else:
                                ch = w.encode("utf-8")
                                # Echo ON: Display typed character on PC
                                if echo:
                                    putch(ch)
                        else:
                            time.sleep(0.005)
                            continue
                    else:
                        # POSIX
                        import select
                        r, _, _ = select.select([sys.stdin], [], [], 0.005)
                        if not r:
                            continue
                        first = os.read(sys.stdin.fileno(), 1)
                        need = _utf8_need_follow(first[0])
                        ch = first + (os.read(sys.stdin.fileno(), need) if need else b"")
                        # Echo ON: Display typed character on PC
                        if echo:
                            putch(ch)

                    self._write(CR if ch == LF else ch)
                except Exception:
                    pass
        finally:
            pass

    def _exec_raw_paste(self, command: bytes, data_consumer: Optional[Callable[[bytes], None]] = None) -> tuple[bytes, bytes]:
        """
        Execute command using raw-paste mode with flow control.
        Returns (stdout_data, stderr_data).
        
        Protocol steps (from MicroPython official docs):
        1. Already in raw REPL and raw-paste mode
        2. Read initial window size (already done in _enter_raw_paste_mode)
        3. Send code with flow control:
           - Track remaining window size
           - When window exhausted, wait for b'\x01' (window increment) or b'\x04' (end)
        4. Send b'\x04' when all code sent
        5. Read b'\x04' acknowledgment
        6. Read execution output until b'\x04'
        7. Read error output until b'\x04'
        8. Read final b'>' prompt
        """
        DebugHelper.log(f"_exec_raw_paste: payload={len(command)}B")
        
        # Step 3-4: Send code with flow control
        remaining_window = self._raw_paste_window_size * 2  # Initial window (2x increment)
        bytes_sent = 0
        command_len = len(command)
        
        while bytes_sent < command_len:
            # Wait for window space
            while remaining_window <= 0:
                fc_byte = self._read(1)
                if fc_byte == self._RAW_PASTE_WINDOW_INC:
                    remaining_window += self._raw_paste_window_size
                    DebugHelper.log(f"_exec_raw_paste: got window increment, now={remaining_window}")
                elif fc_byte == self._RAW_PASTE_END_DATA:
                    DebugHelper.log("_exec_raw_paste: device requested end of data")
                    self.serial.write(self._CTRL_D)
                    break
                else:
                    DebugHelper.log(f"_exec_raw_paste: unexpected flow control byte {fc_byte!r}")
                    raise ReplxError("Raw-paste flow control error")
            
            if fc_byte == self._RAW_PASTE_END_DATA:
                break
            
            # Send chunk
            chunk_size = min(remaining_window, command_len - bytes_sent)
            chunk = command[bytes_sent:bytes_sent + chunk_size]
            self.serial.write(chunk)
            bytes_sent += chunk_size
            remaining_window -= chunk_size
            
            if DebugHelper.enabled() and bytes_sent % 1024 == 0:
                DebugHelper.log(f"_exec_raw_paste: sent {bytes_sent}/{command_len} window={remaining_window}")
            
            # Check for flow control without blocking
            if self.serial.in_waiting > 0:
                fc_byte = self._read(1)
                if fc_byte == self._RAW_PASTE_WINDOW_INC:
                    remaining_window += self._raw_paste_window_size
                elif fc_byte == self._RAW_PASTE_END_DATA:
                    DebugHelper.log("_exec_raw_paste: device requested end of data (early)")
                    self.serial.write(self._CTRL_D)
                    break
        
        # Step 4: Signal end of data
        if bytes_sent == command_len:
            self.serial.write(self._CTRL_D)
            DebugHelper.log(f"_exec_raw_paste: sent EOF after {bytes_sent} bytes")
        
        # Step 5: Read compilation acknowledgment (b'\x04')
        ack = self._read_ex(1, self._EOF_MARKER, timeout=5)
        if not ack.endswith(self._EOF_MARKER):
            DebugHelper.log(f"_exec_raw_paste: missing compilation ACK, got {ack!r}")
            raise ReplxError("Raw-paste compilation acknowledgment timeout")
        DebugHelper.log("_exec_raw_paste: got compilation ACK")
        
        # Step 6: Read stdout until b'\x04'
        stdout_data = self._read_ex(1, self._EOF_MARKER, timeout=0, data_consumer=data_consumer)
        if stdout_data.endswith(self._EOF_MARKER):
            stdout_data = stdout_data[:-1]
        DebugHelper.log(f"_exec_raw_paste: got stdout {len(stdout_data)}B")
        
        # Step 7: Read stderr until b'\x04'
        stderr_data = self._read_ex(1, self._EOF_MARKER, timeout=5)
        if stderr_data.endswith(self._EOF_MARKER):
            stderr_data = stderr_data[:-1]
        DebugHelper.log(f"_exec_raw_paste: got stderr {len(stderr_data)}B")
        
        # Step 8: Read final prompt '>'
        prompt = self._read(1)
        if prompt != b'>':
            DebugHelper.log(f"_exec_raw_paste: expected '>' prompt, got {prompt!r}")
        
        return (stdout_data, stderr_data)

    def _exec(self, command:str|bytes, interactive:bool=False, echo:bool=False, detach:bool=False) -> bytes:
        """
        Execute a command on the device and return the output.
        Automatically uses raw-paste mode if supported for better performance and reliability.
        Falls back to standard raw mode if raw-paste is not available.
        :param command: The command to execute.
        :param interactive: If True, stream the output to stdout.
        :param echo: If True, echo the command to stdout.
        :param detach: If True, return immediately without waiting for output.
        :return: The output of the command as bytes.
        """
        global _skip_error_output, _error_header_buf
                
        self._stop_event.clear()
        _skip_error_output = False
        _error_header_buf = b""
        
        if isinstance(command, str):
            command = command.encode('utf-8')
        
        data_err = b''
        data_consumer = _stdout_write_bytes if interactive else None
        follow_thread = None   

        if interactive:
            set_active_upy_for_sigint(self)

        # Read initial '>' prompt from raw REPL
        data = self._read_ex(1, b'>')
        if not data.endswith(b'>'):
            DebugHelper.log("_exec: missing '>' prompt before paste")
            raise ReplxError('could not enter raw repl')

        command_len = len(command)
        DebugHelper.log(f"_exec: payload={command_len}B interactive={interactive} echo={echo} detach={detach}")

        # Try raw-paste mode first (if not interactive and not already known to be unsupported)
        use_raw_paste = False
        if not interactive and not detach:
            if self._raw_paste_supported is None:
                # First time - try to enter raw-paste mode
                use_raw_paste = self._enter_raw_paste_mode()
            elif self._raw_paste_supported:
                # Known to be supported - enter it again
                use_raw_paste = self._enter_raw_paste_mode()
        
        if use_raw_paste:
            DebugHelper.log("_exec: using RAW-PASTE mode")
            try:
                data, data_err = self._exec_raw_paste(command, data_consumer)
                
                if data_err:
                    if self._interrupt_requested:
                        data_err = b""
                    else:
                        raise ReplxError(data_err.decode('utf-8', errors='replace'))
                
                self._interrupt_requested = False
                return data
            except Exception as e:
                DebugHelper.log(f"_exec: raw-paste failed, falling back to standard mode: {e}")
                # Mark as not supported and fall through to standard mode
                self._raw_paste_supported = False
                # Re-enter raw REPL to recover
                self._leave_repl()
                time.sleep(0.1)
                self._enter_repl(soft_reset=False)
                data = self._read_ex(1, b'>')
                if not data.endswith(b'>'):
                    raise ReplxError('could not recover after raw-paste failure')
        
        # Standard raw mode execution (original implementation)
        DebugHelper.log("_exec: using STANDARD raw mode")

        command_len = len(command)
        DebugHelper.log(f"_exec: payload={command_len}B interactive={interactive} echo={echo}")

        current_buffer_size = 1024
        max_buffer_size = 8192
        bytes_sent = 0
        
        # Adaptive sending with performance monitoring
        start_time = time.time()
        
        while bytes_sent < command_len:
            chunk_start = bytes_sent
            chunk_end = min(bytes_sent + current_buffer_size, command_len)
            chunk = command[chunk_start:chunk_end]
            
            chunk_start_time = time.time()
            self.serial.write(chunk)
            bytes_sent += len(chunk)
            
            chunk_time = time.time() - chunk_start_time
            if chunk_time < 0.01 and current_buffer_size < max_buffer_size:
                current_buffer_size = min(current_buffer_size * 2, max_buffer_size)
            elif chunk_time > 0.05:
                current_buffer_size = max(current_buffer_size // 2, 512)
            if DebugHelper.enabled():
                DebugHelper.log(f"TX chunk {len(chunk)}B  window={current_buffer_size} elapsed={chunk_time:.4f}s sent={bytes_sent}/{command_len}")

            if bytes_sent % 32768 == 0:  # Every 32KB
                time.sleep(0.005)  # 5ms pause
        
        self.serial.write(self._EOF_MARKER)
        
        transfer_time = time.time() - start_time
        timeout = max(5, int(transfer_time * 2))
        DebugHelper.log(f"_exec: transfer_time={transfer_time:.3f}s -> timeout={timeout}s")
        
        data = self._read_ex(1, self._OK_RESPONSE, timeout=timeout)
        if not data.endswith(self._OK_RESPONSE):
            DebugHelper.log("_exec: missing OK after payload")
            raise ReplxError('could not execute command (response: %r)' % data)

        if detach:
            return b''
        
        if interactive:
            self._stop_event.clear()
            self._interrupt_requested = False
            follow_thread = threading.Thread(target=self._follow_task, args=(echo,), daemon=True)
            follow_thread.start()
            
        try:
            # Read first data
            data = self._read_ex(1, self._EOF_MARKER, 0, data_consumer)
            got_first_eof = data.endswith(self._EOF_MARKER)
            if got_first_eof:
                data = data[:-1]
                data_err = self._read_ex(1, self._EOF_MARKER, 0, None)
                pos = data_err.rfind(self._EOF_MARKER)
                if pos == -1:
                    tail = self._read(1)
                    if tail == self._EOF_MARKER:
                        data_err += tail
                        pos = len(data_err) - 1
                if pos == -1:
                    if not self._interrupt_requested:
                        raise ReplxError('timeout waiting for second EOF reception')
                else:
                    data_err = data_err[:pos]
            else:
                if self._interrupt_requested:
                    data_err = b""
                else:
                    raise ReplxError('timeout waiting for first EOF reception')
        finally:            
            def _drain_consumer(b: bytes, _carry=[False]):
                if len(b) == 1 and b == b'>':
                    return
                _stdout_write_bytes(b)
    
            _skip_error_output = False
            if follow_thread and follow_thread.is_alive():
                self._stop_event.set()
                try: 
                    follow_thread.join(timeout=0.05)
                except Exception:
                    pass
                
                self._stop_event.clear()
                
                try:
                    self._read_ex(1, self._EOF_MARKER, timeout=0.1, data_consumer=_drain_consumer)
                except Exception:
                    pass

                try:
                    _flush_outbuf()
                except Exception:
                    pass

                try:
                    if self._interrupt_requested and self.core != "EFR32MG":
                        self.serial.write(b'\r' + self._CTRL_B)
                        time.sleep(0.08)
                        self.serial.write(b'\r' + self._CTRL_A)
                        self._read_ex(1, self._RAW_REPL_PROMPT[:-1], timeout=1)
                    else:
                        self.serial.write(self._CTRL_D)
                        got = self._read_ex(1, self._SOFT_REBOOT_MSG, timeout=2)
                        if not got.endswith(self._SOFT_REBOOT_MSG):
                            self.serial.write(b'\r' + self._CTRL_B)
                            time.sleep(0.08)
                            self.serial.write(b'\r' + self._CTRL_A)
                            self._read_ex(1, self._RAW_REPL_PROMPT[:-1], timeout=1)
                except Exception:
                    pass
            DebugHelper.log("_exec: done; returning stdout bytes")
        
        # Clean up signal handler
        if interactive:
            set_active_upy_for_sigint(None)
        
        if data_err:
            if self._interrupt_requested:
                data_err = b""
            else:
                raise ReplxError(data_err.decode('utf-8', errors='replace'))

        self._interrupt_requested = False
        return data

    def _drain_eof(self, max_ms:int=200):
        """
        Drain the serial input buffer until EOF is received or timeout occurs.
        :param max_ms: Maximum time to wait for EOF in milliseconds.
        """
        deadline = time.time() + max_ms / 1000
        while time.time() < deadline:
            waiting = self.serial.in_waiting
            if waiting:
                _ = self._read(waiting) 
            else:
                time.sleep(0.01)

    def _repl_serial_to_stdout(self):
        """
        Read data from the serial port and write it to stdout.
        """
        try:
            while self.serial_reader_running:
                try:
                    count = self.serial.in_waiting
                except Exception:
                    break

                if not count:
                    time.sleep(0.01)
                    continue

                try:
                    data = self.serial.read(count)
                except Exception:
                    break

                if not data:
                    continue

                if self.serial_out_put_enable and self.serial_out_put_count > 0:
                    if IS_WINDOWS:
                        sys.stdout.buffer.write(data.replace(b"\r", b""))
                    else:
                        sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()

                self.serial_out_put_count += 1
        except KeyboardInterrupt:
            try:
                if self.serial is not None:
                    self.serial.close()
            except Exception:
                pass

    def _reset(self):
        """
        Reset the device by executing a soft reset command. 
        """
        command = f"""
            import machine
            machine.soft_reset()  # Ctrl+D
        """
        self.exec(command)

    def request_interrupt(self):
        self._interrupt_requested = True
        try:
            self.serial.write(self._CTRL_C)
        except Exception:
            pass

    def exec(self, command:str=None):
        """
        Run a command or script on the device.
        :param command: The command to execute.
        """
        self._enter_repl()
        try:
            command = textwrap.dedent(command)
            return self._exec(command)
        finally:
            self._leave_repl()
    
    def run(self, local, interactive:bool=False, echo:bool=False):
        """
        Run a command or script on the device.
        :param local: Path to the script file to execute.
        :param interactive: If True, stream the output to stdout.
        :param echo: If True, echo the command to stdout.
        """
        if not interactive and echo:
            raise typer.BadParameter("Option chaining error: -n and -e can only be used once, not multiple times.")
        
        self._enter_repl()
        try:
            with open(local, "rb") as f:
                data = f.read()
            # non-interactive mode: detach immediately after sending script
            # interactive mode: wait for completion and handle I/O
            self._exec(data, interactive, echo, detach=not interactive)
            if interactive:
                self._drain_eof(max_ms=200)
        finally:
            self._leave_repl()
    
    def put_files_batch(self, file_specs: list[tuple[str, str]], progress_callback: Optional[Callable[[int, int, str], None]] = None):
        """
        Upload multiple files in a single REPL session (optimized batch mode).
        This method minimizes REPL enter/exit overhead by batching multiple file uploads.
        
        :param file_specs: List of (local_path, remote_path) tuples to upload
        :param progress_callback: Optional callback(done, total, filename) for progress tracking
        :raises ReplxError: If any file upload fails
        """
        if not file_specs:
            return
        
        total = len(file_specs)
        DebugHelper.log(f"put_files_batch: uploading {total} files in batch mode")
        
        # Single REPL enter/exit for entire batch
        self._enter_repl()
        try:
            for idx, (local_path, remote_path) in enumerate(file_specs):
                if progress_callback:
                    try:
                        progress_callback(idx, total, os.path.basename(remote_path))
                    except Exception:
                        pass
                
                # Open file on device
                self._exec(f"f = open('{remote_path}', 'wb')")
                
                try:
                    with open(local_path, 'rb') as f_local:
                        file_size = os.fstat(f_local.fileno()).st_size
                        DEVICE_CHUNK = self._DEVICE_CHUNK_SIZES
                        
                        batch_lines = []
                        batch_bytes = 0
                        BATCH_LIMIT = max(8 * 1024, int(self._PUT_BATCH_BYTES))
                        
                        while True:
                            chunk = f_local.read(DEVICE_CHUNK)
                            if not chunk:
                                # Flush final batch
                                if batch_lines:
                                    code = ";\n".join(batch_lines)
                                    self._exec(code)
                                break
                            
                            line = f"f.write({repr(chunk)})"
                            batch_lines.append(line)
                            batch_bytes += len(line)
                            
                            if batch_bytes >= BATCH_LIMIT:
                                code = ";\n".join(batch_lines)
                                self._exec(code)
                                batch_lines = []
                                batch_bytes = 0
                    
                    # Close file on device
                    self._exec("f.close()")
                    
                except Exception as e:
                    # Attempt to close file on device before re-raising
                    try:
                        self._exec("f.close()")
                    except Exception:
                        pass
                    raise ReplxError(f"Failed to upload {local_path}: {e}")
            
            if progress_callback:
                try:
                    progress_callback(total, total, "")
                except Exception:
                    pass
            
            DebugHelper.log(f"put_files_batch: completed {total} files")
        
        finally:
            self._leave_repl()
    
    def close(self):
        """
        Close the serial connection.
        """
        self.serial.close()

    def reset(self):
        self._write(b'\r' + self._CTRL_D)  
 
    def repl(self):
        """
        Enter the REPL mode, allowing interaction with the device.
        """
        self.serial_reader_running = True
        self.serial_out_put_enable = True
        self.serial_out_put_count = 1

        self._reset()
        self._read_ex(1, b'\x3E\x3E\x3E', timeout=1) # read prompt >>>

        repl_thread = threading.Thread(target=self._repl_serial_to_stdout, daemon=True, name='REPL')
        repl_thread.start()

        self.serial.write(b'\r') # Update prompt
        
        while True:
            char = getch()

            if char == b'\x07': 
                self.serial_out_put_enable = False
                continue
            elif char == b'\x0F': 
                self.serial_out_put_enable = True
                self.serial_out_put_count = 0
                continue
            elif char == b'\x00' or not char: # Ignore null characters
                continue
            elif char == self._CTRL_C:  # Ctrl + C to exit repl mode
                break
            
            try:
                self.serial.write(b'\r' if char == b'\n' else char)
            except:
                break
            
        self.serial_reader_running = False
        self._reset()
        print('')
