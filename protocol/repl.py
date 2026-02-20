import sys
import time
import struct
import textwrap
import threading
from contextlib import contextmanager
from typing import Callable, Optional

import typer

from replx.utils.exceptions import ProtocolError, TransportError
from replx.utils.constants import (
    CTRL_A, CTRL_B, CTRL_C, CTRL_D, CTRL_E,
    EOF_MARKER,
    RAW_REPL_PROMPT, SOFT_REBOOT_MSG, OK_RESPONSE,
    RAW_PASTE_INIT, RAW_PASTE_SUPPORTED, RAW_PASTE_NOT_SUPPORTED,
    RAW_PASTE_WINDOW_INC, RAW_PASTE_END_DATA,
    RAW_PASTE_DEFAULT_WINDOW_SIZE,
    ERROR_HEADER,
    DEVICE_CHUNK_SIZE_DEFAULT, DEVICE_CHUNK_SIZE_EFR32MG,
    PUT_BATCH_BYTES_DEFAULT, PUT_BATCH_BYTES_EFR32MG,
    RAW_MODE_DELAY_DEFAULT, RAW_MODE_DELAY_EFR32MG,
)
from replx.transport import create_transport
from replx.terminal import (
    IS_WINDOWS, CR, LF,
    flush_outbuf as _flush_outbuf,
    stdout_write_bytes as _stdout_write_bytes,
    getch, putch, getch_nonblock,
)


class ReplProtocol:
    _active_instance = None
    _sigint_handler_installed = False
        
    def interrupt(self) -> None:
        self.transport.write(CTRL_C)
    
    def send_eof(self) -> None:
        self.transport.write(CTRL_D)
    
    def send_raw(self, data: bytes) -> None:
        self.transport.write(data)
    
    def drain(self) -> bytes:
        return self.transport.read_available()
    
    def enter_paste_mode(self) -> None:
        self.transport.write(CTRL_E)
    
    def exit_paste_mode(self) -> None:
        self.transport.write(CTRL_B)
    
    def in_waiting(self) -> int:
        return self.transport.in_waiting()
    
    def read_bytes(self, n: int) -> bytes:
        return self.transport.read(n)
        
    def __init__(self, connection_string, baudrate:int=115200, core:str="RP2350", device_root_fs:str="/"):
        from replx.transport import Transport
        
        try:
            if isinstance(connection_string, Transport):
                self.transport = connection_string
            else:
                self.transport = create_transport(connection_string, baudrate=baudrate)
            self._stop_event = threading.Event()
            self._follow_thread = None
        except Exception as e:
            raise ProtocolError(f"failed to open {connection_string} ({e})")
        
        self._rx_pushback = bytearray()
        self._raw_paste_supported = None
        self._raw_paste_window_size = RAW_PASTE_DEFAULT_WINDOW_SIZE
        
        self._skip_error_output = False
        self._error_header_buf = b""
        
        self.core = core
        self.device_root_fs = device_root_fs
        
        if core == "EFR32MG":
            self._DEVICE_CHUNK_SIZES = DEVICE_CHUNK_SIZE_EFR32MG
            self._RAW_MODE_DELAY = RAW_MODE_DELAY_EFR32MG
            self._PUT_BATCH_BYTES = PUT_BATCH_BYTES_EFR32MG
        else:
            self._DEVICE_CHUNK_SIZES = DEVICE_CHUNK_SIZE_DEFAULT
            self._RAW_MODE_DELAY = RAW_MODE_DELAY_DEFAULT
            self._PUT_BATCH_BYTES = PUT_BATCH_BYTES_DEFAULT
        
        self._init_repl()
        self._install_sigint_handler()

    @classmethod
    def _install_sigint_handler(cls):
        if IS_WINDOWS and not cls._sigint_handler_installed:
            import signal
            
            def win_sigint_handler(signum, frame):
                if cls._active_instance is not None:
                    try:
                        cls._active_instance.request_interrupt()
                    except Exception:
                        pass
            
            try:
                signal.signal(signal.SIGINT, win_sigint_handler)
                cls._sigint_handler_installed = True
            except Exception:
                pass
    
    def _reset_error_filter(self):
        self._skip_error_output = False
        self._error_header_buf = b""

    def _init_repl(self):
        self._interrupt_requested = False
        self._session_depth = 0
        self._in_raw_repl = False
        self._repl_prompt_detected = False

    def _read(self, n:int=1) -> bytes:
        if self._rx_pushback:
            if len(self._rx_pushback) <= n:
                b = bytes(self._rx_pushback)
                self._rx_pushback.clear()
                rem = n - len(b)
                if rem > 0:
                    b += self.transport.read(rem)
            else:
                b = bytes(self._rx_pushback[:n])
                del self._rx_pushback[:n]
        else:
            b = self.transport.read(n)

        return b
    
    def _read_ex(self, min_num_bytes:int, ending:bytes, timeout:int=5,
                data_consumer:Optional[Callable[[bytes], None]]=None) -> bytes:
        start = time.time()
        deadline = (start + timeout) if timeout > 0 else None
        
        streaming_mode = (timeout == 0 and data_consumer is not None)
        
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
        
        tail_size = m + 8
        tail_buffer = bytearray(tail_size)
        tail_len = 0 
        data = bytearray()
        
        def _call_consumer(chunk: bytes):
            if data_consumer and chunk:
                try:
                    data_consumer(chunk)
                except Exception:
                    pass

        def _update_tail(chunk: bytes):
            nonlocal tail_len
            chunk_len = len(chunk)
            if chunk_len >= tail_size:
                tail_buffer[:] = chunk[-tail_size:]
                tail_len = tail_size
            elif tail_len + chunk_len <= tail_size:
                tail_buffer[tail_len:tail_len + chunk_len] = chunk
                tail_len += chunk_len
            else:
                keep = tail_size - chunk_len
                tail_buffer[:keep] = tail_buffer[tail_len - keep:tail_len]
                tail_buffer[keep:keep + chunk_len] = chunk
                tail_len = tail_size

        def _feed_chunk(chunk: bytes) -> int:
            nonlocal matched

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

            if streaming_mode:
                chunk_to_process = chunk[:end_at+1] if end_at >= 0 else chunk
                _call_consumer(chunk_to_process)
                _update_tail(chunk_to_process)
                return end_at + 1 if end_at >= 0 else -1
            else:
                if end_at >= 0:
                    data.extend(chunk[:end_at+1])
                    _call_consumer(chunk[:end_at+1])
                    return end_at + 1
                
                data.extend(chunk)
                _call_consumer(chunk)
                return -1

        def _read_some(max_n: int) -> bytes:
            if self._rx_pushback:
                return self._read(max(1, max_n))
            try:
                avail = self.transport.read_available()
                if avail:
                    return avail
            except Exception:
                pass

            if streaming_mode:
                return b''
            return self._read(1)

        def _get_result() -> bytes:
            if streaming_mode:
                return bytes(tail_buffer[:tail_len])
            else:
                return bytes(data)

        try:
            if min_num_bytes > 0:
                chunk = _read_some(min_num_bytes)
                if chunk:
                    pos = _feed_chunk(chunk)
                    if pos >= 0:
                        tail = chunk[pos:]
                        if tail:
                            self._rx_pushback[:] = tail + self._rx_pushback
                        return _get_result()

            last_activity = time.time()
            last_keepalive = time.time()
            keepalive_interval = 30.0

            while True:
                if self._stop_event.is_set():
                    break
                
                if deadline is not None and time.time() >= deadline:
                    break
                
                now = time.time()
                if streaming_mode and (now - last_keepalive) >= keepalive_interval:
                    try:
                        if hasattr(self.transport, 'keep_alive'):
                            self.transport.keep_alive()
                        last_keepalive = now
                    except Exception:
                        pass

                waiting = self.transport.in_waiting()
                if waiting <= 0 and len(self._rx_pushback) > 0:
                    waiting = 1

                if waiting > 0:
                    want = min(4096, max(256, waiting))
                    chunk = _read_some(want)
                    if chunk:
                        pos = _feed_chunk(chunk)
                        last_activity = time.time()
                        if pos >= 0:
                            tail = chunk[pos:]
                            if tail:
                                self._rx_pushback[:] = tail + self._rx_pushback
                            return _get_result()
                    else:
                        time.sleep(0.001)
                else:
                    time.sleep(0.001)

                    # Idle timeout only for non-streaming mode
                    if timeout > 0:
                        idle_limit = max(timeout * 2, 10)
                        if (time.time() - last_activity) > idle_limit:
                            break
                    elif data_consumer is None:
                        # No consumer and no timeout: use short idle limit
                        if (time.time() - last_activity) > 5:
                            break
                    # streaming mode (data_consumer is not None): no idle timeout

            return _get_result()

        except TransportError as e:
            if not self._stop_event.is_set():
                raise ProtocolError(f"Transport communication error: {e}")
            return _get_result()
        except ProtocolError:
            raise
        except Exception:
            return _get_result()


    def _enter_repl(self):
        delay = 0.1  # Increased from 0.05
        timeout = 5  # Increased from 3
        
        for attempt in (1, 2, 3, 4):  # Added 4th attempt
            try:
                # More aggressive buffer clearing
                self.transport.reset_input_buffer()
                time.sleep(0.05)
                self.transport.reset_output_buffer()
                time.sleep(0.05)
            except Exception:
                pass

            # Send CTRL+C twice to interrupt any running code
            self.transport.write(b'\r' + CTRL_C + CTRL_C)
            time.sleep(delay)
            
            try:
                # Clear any responses from CTRL+C
                self.transport.reset_input_buffer()
                time.sleep(0.05)
            except Exception:
                pass
            
            # Enter raw REPL mode
            self.transport.write(b'\r' + CTRL_A)

            try:
                data = self._read_ex(1, RAW_REPL_PROMPT[:-1], timeout=timeout)
                if not data.endswith(RAW_REPL_PROMPT[:-1]):
                    raise ProtocolError('could not enter raw repl')
                self._in_raw_repl = True
                return
            except ProtocolError:
                # Try to exit to friendly REPL before next attempt
                try:
                    self.transport.write(b'\r' + CTRL_B)  # friendly
                    time.sleep(0.15)
                    # Clear buffer again
                    try:
                        self.transport.reset_input_buffer()
                    except Exception:
                        pass
                except Exception:
                    pass
                
                # Wait longer between attempts
                time.sleep(0.2 * attempt)  # Progressive backoff
                continue
            
        raise ProtocolError('could not enter raw repl')

    def _soft_reset(self):
        timeout = 3
        try:
            self.transport.write(CTRL_C)
            time.sleep(0.1)
            self.transport.write(CTRL_C)
            time.sleep(0.1)
            self.transport.reset_input_buffer()
            
            self.transport.write(CTRL_A)
            time.sleep(0.1)
            self.transport.reset_input_buffer()
            
            self.transport.write(CTRL_D)
            data = self._read_ex(1, SOFT_REBOOT_MSG, timeout=timeout)
            if not data.endswith(SOFT_REBOOT_MSG):
                raise ProtocolError('soft reset failed')
            
            data = self._read_ex(1, RAW_REPL_PROMPT[:-1], timeout=timeout)
            if not data.endswith(RAW_REPL_PROMPT[:-1]):
                raise ProtocolError('soft reset failed')
        except Exception:
            raise

    def _resync_repl(self):
        try:
            self.transport.write(b'\r' + CTRL_B)
            time.sleep(0.04)
            self.transport.reset_input_buffer()
        except Exception:
            pass

        try:
            self.transport.write(b'\r' + CTRL_A)
            got = self._read_ex(1, RAW_REPL_PROMPT[:-1], timeout=1)
            if got.endswith(RAW_REPL_PROMPT[:-1]):
                return True
        except Exception:
            pass
        return False

    def _leave_repl(self):
        self.transport.write(b'\r' + CTRL_B) 
        self._in_raw_repl = False

    @contextmanager
    def session(self):
        need_enter = self._session_depth == 0
        if need_enter:
            self._enter_repl()
            # Reset raw_paste state for each new session
            self._raw_paste_supported = None
        self._session_depth += 1
        try:
            yield
        finally:
            self._session_depth = max(0, self._session_depth - 1)
            if need_enter:
                self._leave_repl()

    def _enter_raw_paste_mode(self) -> bool:
        if self._raw_paste_supported is not None:
            return self._raw_paste_supported
        
        try:
            self.transport.write(RAW_PASTE_INIT)
            
            start = time.time()
            timeout = 0.5
            response = b''
            while len(response) < 2 and (time.time() - start) < timeout:
                waiting = self.transport.in_waiting()
                if waiting > 0:
                    response += self._read(min(2 - len(response), waiting))
                else:
                    time.sleep(0.01)
            
            if len(response) < 2:
                self._raw_paste_supported = False
                return False
            
            if response == RAW_PASTE_SUPPORTED:
                init_bytes = self._read(3)
                if len(init_bytes) >= 2:
                    self._raw_paste_window_size = struct.unpack('<H', init_bytes[:2])[0]
                    self._raw_paste_supported = True
                    return True
                else:
                    self._raw_paste_supported = False
                    return False
                    
            elif response == RAW_PASTE_NOT_SUPPORTED:
                self._raw_paste_supported = False
                return False
                
            elif response.startswith(b'r'):
                try:
                    self._read_ex(1, b'>', timeout=1)  # Consume remaining response
                except Exception:
                    pass
                self._raw_paste_supported = False
                return False
                
            else:
                self._raw_paste_supported = False
                return False
                
        except Exception:
            self._raw_paste_supported = False
            return False

    def _follow_task(self, echo: bool):
        try:
            while not self._stop_event.is_set():
                try:
                    ch = getch_nonblock()
                    if ch is None:
                        time.sleep(0.005)
                        continue
                    
                    if not ch:
                        continue
                    
                    if ch == CTRL_C:
                        self.request_interrupt()
                        time.sleep(0.1)
                        return
                    
                    if ch == CTRL_D:
                        try:
                            self.transport.write(CTRL_D)
                        except Exception:
                            pass
                        time.sleep(0.08)
                        return
                    
                    if echo:
                        putch(ch)
                    
                    self.transport.write(CR if ch == LF else ch)
                except Exception:
                    pass
        finally:
            pass

    def _exec_raw_paste(self, command: bytes, data_consumer: Optional[Callable[[bytes], None]] = None) -> tuple[bytes, bytes]:
        remaining_window = self._raw_paste_window_size * 2
        bytes_sent = 0
        command_len = len(command)
        aborted = False
        
        while bytes_sent < command_len:
            while remaining_window <= 0:
                fc_byte = self._read(1)
                if fc_byte == RAW_PASTE_WINDOW_INC:
                    remaining_window += self._raw_paste_window_size
                elif fc_byte == RAW_PASTE_END_DATA:
                    self.transport.write(CTRL_D)
                    aborted = True
                    break
            
            if aborted:
                break
            
            chunk_size = min(remaining_window, command_len - bytes_sent)
            chunk = command[bytes_sent:bytes_sent + chunk_size]
            self.transport.write(chunk)
            bytes_sent += chunk_size
            remaining_window -= chunk_size
            
            while self.transport.in_waiting() > 0:
                fc_byte = self._read(1)
                if fc_byte == RAW_PASTE_WINDOW_INC:
                    remaining_window += self._raw_paste_window_size
                elif fc_byte == RAW_PASTE_END_DATA:
                    self.transport.write(CTRL_D)
                    aborted = True
                    break
            
            if aborted:
                break
        
        if not aborted and bytes_sent == command_len:
            self.transport.write(CTRL_D)
        
        ack = self._read_ex(1, EOF_MARKER, timeout=5)
        if not ack.endswith(EOF_MARKER):
            raise ProtocolError("Raw-paste compilation acknowledgment timeout")
        
        stdout_data = self._read_ex(1, EOF_MARKER, timeout=0, data_consumer=data_consumer)
        if stdout_data.endswith(EOF_MARKER):
            stdout_data = stdout_data[:-1]
        
        stderr_data = self._read_ex(1, EOF_MARKER, timeout=5)
        if stderr_data.endswith(EOF_MARKER):
            stderr_data = stderr_data[:-1]
        
        self._read(1)  # Consume prompt
        
        return (stdout_data, stderr_data)

    def _exec(self, command:str|bytes, interactive:bool=False, echo:bool=False, detach:bool=False, 
              data_consumer:Optional[Callable[[bytes], None]]=None,
              force_raw_paste:bool=False) -> bytes:
        self._stop_event.clear()
        self._reset_error_filter()
        
        if isinstance(command, str):
            command = command.encode('utf-8')
        
        # Handle empty command (empty file)
        if not command or len(command) == 0:
            return b''
        
        data_err = b''
        if data_consumer is None and interactive:
            data_consumer = self._create_data_consumer()
        follow_thread = None   

        if interactive:
            ReplProtocol._active_instance = self

        data = self._read_ex(1, b'>')
        if not data.endswith(b'>'):
            raise ProtocolError('could not enter raw repl')

        command_len = len(command)

        # raw_paste only for put operations (force_raw_paste=True), not for general commands
        use_raw_paste = force_raw_paste and self._enter_raw_paste_mode()
        
        if use_raw_paste:
            try:
                data, data_err = self._exec_raw_paste(command, data_consumer)
                
                if data_err:
                    if self._interrupt_requested:
                        data_err = b""
                    else:
                        raise ProtocolError(data_err.decode('utf-8', errors='replace'))
                
                self._interrupt_requested = False
                return data
            except Exception:
                self._raw_paste_supported = False
                if not self._resync_repl():
                    self._leave_repl()
                    time.sleep(0.05)
                    self._enter_repl()
                    data = self._read_ex(1, b'>')
                    if not data.endswith(b'>'):
                        raise ProtocolError('could not recover after raw-paste failure')
        
        current_buffer_size = self._DEVICE_CHUNK_SIZES
        bytes_sent = 0
        
        start_time = time.time()
        
        while bytes_sent < command_len:
            chunk_end = min(bytes_sent + current_buffer_size, command_len)
            chunk = command[bytes_sent:chunk_end]
            
            self.transport.write(chunk)
            bytes_sent += len(chunk)
            
            if self._RAW_MODE_DELAY > 0 and bytes_sent < command_len:
                time.sleep(self._RAW_MODE_DELAY)
        
        self.transport.write(EOF_MARKER)
        
        transfer_time = time.time() - start_time
        timeout = max(5, int(transfer_time * 2))
        
        data = self._read_ex(1, OK_RESPONSE, timeout=timeout)
        if not data.endswith(OK_RESPONSE):
            raise ProtocolError('could not execute command (response: %r)' % data)

        if detach:
            return b''
        
        if interactive:
            self._stop_event.clear()
            self._interrupt_requested = False
            follow_thread = threading.Thread(target=self._follow_task, args=(echo,), daemon=True)
            follow_thread.start()
            
        try:
            data = self._read_ex(1, EOF_MARKER, 0, data_consumer)
            if data.endswith(EOF_MARKER):
                data = data[:-1]
            
            data_err = self._read_ex(1, EOF_MARKER, 0, None)
            if data_err.endswith(EOF_MARKER):
                data_err = data_err[:-1]
            elif data_err and not self._interrupt_requested:
                raise ProtocolError(data_err.decode('utf-8', errors='replace'))
        finally:            
            def _drain_consumer(b: bytes, _carry=[False]):
                if len(b) == 1 and b == b'>':
                    return
                if data_consumer:
                    data_consumer(b)
                else:
                    _stdout_write_bytes(b)
    
            if follow_thread and follow_thread.is_alive():
                self._stop_event.set()
                try: 
                    follow_thread.join(timeout=0.05)
                except Exception:
                    pass
                
                self._stop_event.clear()
                
                try:
                    self._read_ex(1, EOF_MARKER, timeout=0.1, data_consumer=_drain_consumer)
                except Exception:
                    pass

                if not data_consumer:
                    try:
                        _flush_outbuf()
                    except Exception:
                        pass

                try:
                    if self._interrupt_requested and self.core != "EFR32MG":
                        self.transport.write(b'\r' + CTRL_B)
                        time.sleep(0.08)
                        self.transport.write(b'\r' + CTRL_A)
                        self._read_ex(1, RAW_REPL_PROMPT[:-1], timeout=1)
                    else:
                        self.transport.write(CTRL_D)
                        got = self._read_ex(1, SOFT_REBOOT_MSG, timeout=2)
                        if not got.endswith(SOFT_REBOOT_MSG):
                            self.transport.write(b'\r' + CTRL_B)
                            time.sleep(0.08)
                            self.transport.write(b'\r' + CTRL_A)
                            self._read_ex(1, RAW_REPL_PROMPT[:-1], timeout=1)
                except Exception:
                    pass
        
        if interactive:
            ReplProtocol._active_instance = None
        
        if data_err:
            if self._interrupt_requested:
                data_err = b""
            else:
                raise ProtocolError(data_err.decode('utf-8', errors='replace'))

        self._interrupt_requested = False
        return data
    
    def _create_data_consumer(self):
        def data_consumer(chunk):
            if not chunk:
                return
            
            self._error_header_buf = (self._error_header_buf + chunk)[-len(ERROR_HEADER):]
            if self._error_header_buf == ERROR_HEADER:
                self._skip_error_output = True
                return
            
            if not self._skip_error_output:
                _stdout_write_bytes(chunk)
                _flush_outbuf()
        
        return data_consumer

    def _drain_eof(self, max_ms:int=200):
        deadline = time.time() + max_ms / 1000
        while time.time() < deadline:
            waiting = self.transport.in_waiting()
            if waiting:
                _ = self._read(waiting) 
            else:
                time.sleep(0.01)

    def _repl_serial_to_stdout(self):
        PROMPT_COLOR = b"\033[92m"
        CONT_COLOR = b"\033[93m"
        RESET_COLOR = b"\033[0m"
        
        try:
            while self.serial_reader_running:
                try:
                    count = self.transport.in_waiting()
                except Exception:
                    break

                if not count:
                    time.sleep(0.01)
                    continue

                try:
                    data = self.transport.read(count)
                except Exception:
                    break

                if not data:
                    continue

                if b">>> " in data:
                    self._repl_prompt_detected = True

                if self.serial_out_put_enable and self.serial_out_put_count > 0:
                    data = data.replace(b">>> ", PROMPT_COLOR + b">>> " + RESET_COLOR)
                    data = data.replace(b"... ", CONT_COLOR + b"... " + RESET_COLOR)
                    
                    if IS_WINDOWS:
                        sys.stdout.buffer.write(data.replace(b"\r", b""))
                    else:
                        sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()

                self.serial_out_put_count += 1
        except KeyboardInterrupt:
            try:
                self.transport.close()
            except Exception:
                pass

    def _reset(self):
        command = """
            import machine
            machine.soft_reset()  # Ctrl+D
        """
        self.exec(command)

    def request_interrupt(self):
        self._interrupt_requested = True
        try:
            self.transport.write(CTRL_C)
        except Exception:
            pass

    def exec(self, command:str=None):
        if not self._in_raw_repl:
            self._enter_repl()
        
        try:
            command = textwrap.dedent(command)
            return self._exec(command)
        except ProtocolError:
            raise
        finally:
            pass
    
    def run(self, local, interactive:bool=False, echo:bool=False):
        if not interactive and echo:
            raise typer.BadParameter("Option chaining error: -n and -e can only be used once, not multiple times.")
        
        # Read file first to check if empty
        with open(local, "rb") as f:
            data = f.read()
        
        # Handle empty file: exit quietly without entering REPL
        if not data or len(data) == 0:
            return
        
        if not self._in_raw_repl:
            self._enter_repl()
        
        try:
            self._soft_reset()
            self._exec(data, interactive, echo, detach=not interactive)
            if interactive:
                self._drain_eof(max_ms=200)
        except Exception:
            self._leave_repl()
            raise
            raise
    

    
    def close(self):
        self.transport.close()

    def reset(self):
        # Always force re-entry to raw REPL to ensure clean state
        # _in_raw_repl flag may be out of sync with actual board state
        self._in_raw_repl = False
        self._enter_repl()
        
        try:
            self._soft_reset()
        except Exception:
            self._leave_repl()
            raise
 
    def repl(self):
        import time
        
        self.serial_reader_running = True
        self.serial_out_put_enable = False 
        self.serial_out_put_count = 0
        self._repl_prompt_detected = False

        repl_thread = threading.Thread(target=self._repl_serial_to_stdout, daemon=True, name='REPL')
        repl_thread.start()
        
        time.sleep(0.05)

        self.transport.write(CTRL_B)
        time.sleep(0.05)
        self.transport.write(CTRL_C)
        time.sleep(0.1)
        
        drain_timeout = time.time() + 0.5
        while time.time() < drain_timeout:
            if self.transport.in_waiting() > 0:
                self.transport.read(self.transport.in_waiting())
                time.sleep(0.02)
            else:
                time.sleep(0.05)
                if self.transport.in_waiting() == 0:
                    break
        
        self.serial_out_put_enable = True
        self.serial_out_put_count = 1
        self.transport.write(b'\r')
        
        prompt_timeout = time.time() + 1.0
        while time.time() < prompt_timeout:
            time.sleep(0.05)
            if self._repl_prompt_detected:
                break
        
        recent_chars = bytearray(6)
        recent_len = 0
        
        while True:
            char = getch()

            if char == b'\x07': 
                self.serial_out_put_enable = False
                continue
            elif char == b'\x0F': 
                self.serial_out_put_enable = True
                self.serial_out_put_count = 0
                continue
            elif char == b'\x00' or not char:
                continue
            
            if char == b'\r' or char == b'\n':
                if recent_len >= 4:
                    cmd = bytes(recent_chars[:recent_len]).strip().lower()
                    if cmd in (b'exit', b'exit()'):
                        break
                recent_len = 0
            elif char == b'\x7f' or char == b'\x08':
                if recent_len > 0:
                    recent_len -= 1
            elif char >= b' ' and len(char) == 1: 
                if recent_len < 6:
                    recent_chars[recent_len] = char[0]
                    recent_len += 1
                else:
                    recent_chars[:-1] = recent_chars[1:]
                    recent_chars[5] = char[0]
            
            try:
                self.transport.write(b'\r' if char == b'\n' else char)
            except (OSError, Exception):
                break
            
        self.serial_reader_running = False
        print('')
