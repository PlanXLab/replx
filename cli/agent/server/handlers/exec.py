import os
import time
import threading
import sys
from typing import Optional

from replx.utils.constants import CTRL_C, CTRL_D, EOF_MARKER, MAX_PAYLOAD_SIZE
from replx.utils.exceptions import ProtocolError
from replx.cli.agent.protocol import AgentProtocol
from ..command_dispatcher import CommandContext
from ..connection_manager import BoardConnection


def _normalize_port(port: str) -> str:
    """Normalize port name for comparison.
    
    Windows: case-insensitive (COM10 == com10)
    Linux/macOS: case-sensitive (/dev/ttyACM0 != /dev/TTYACM0)
    """
    if sys.platform.startswith("win"):
        return port.upper()
    else:
        return port


def _find_connection_by_port(connection_manager, port: str) -> Optional[BoardConnection]:
    """Find a connection by port, honoring Windows' case-insensitive semantics."""
    if not port:
        return None

    # Try exact match first (preserve original port case where possible)
    conn = connection_manager.get_connection(port)
    if conn:
        return conn

    # Windows: try normalized key, then case-insensitive scan
    if sys.platform.startswith("win"):
        normalized = _normalize_port(port)
        conn = connection_manager.get_connection(normalized)
        if conn:
            return conn

        port_lower = port.lower()
        for key, candidate in connection_manager.get_all_connections().items():
            if isinstance(key, str) and key.lower() == port_lower:
                return candidate

    return None


class ExecCommandsMixin:
    def _cmd_exec(self, ctx: CommandContext, code: str, interactive: bool = False) -> dict:
        conn = ctx.connection
        if not conn or not conn.repl_protocol:
            raise RuntimeError("Not connected")
        
        result = conn.repl_protocol.exec(code)
        result_str = result.decode('utf-8', errors='replace') if isinstance(result, bytes) else result
        
        if len(result_str) > MAX_PAYLOAD_SIZE - 1000:
            result_str = result_str[:MAX_PAYLOAD_SIZE - 1100] + "\n... [output truncated]"
        
        return {"output": result_str}
    
    def _cmd_status(self, ctx: CommandContext) -> dict:
        conn = None
        if ctx.explicit_port:
            conn = _find_connection_by_port(self.connection_manager, ctx.explicit_port)
        elif ctx.ppid:
            conn = self._get_active_connection(ctx.ppid)
        
        # Check if any connection is running a detached script
        all_connections = self.connection_manager.get_all_connections()
        any_detached = any(c.is_detached() for c in all_connections.values())
        
        if conn and conn.repl_protocol:
            is_this_detached = conn.is_detached()
            return {
                "running": True,
                "connected": True,
                "port": conn.port,
                "device": conn.device,
                "core": conn.core,
                "manufacturer": conn.manufacturer,
                "version": conn.version,
                "device_root_fs": conn.device_root_fs,
                "in_raw_repl": conn.repl_protocol._in_raw_repl if conn.repl_protocol else False,
                "pid": os.getpid(),
                "busy": conn.busy or is_this_detached,
                "busy_command": conn.busy_command or ("detached_script" if is_this_detached else None),
                "detached_running": any_detached,
                "board_id": conn.board_id
            }
        
        # If explicit port was requested but not found, do not fall back
        if ctx.explicit_port:
            return {
                "running": True,
                "connected": False,
                "port": ctx.explicit_port,
                "device": "",
                "core": "",
                "manufacturer": "",
                "version": "",
                "in_raw_repl": False,
                "pid": os.getpid(),
                "busy": False,
                "busy_command": None,
                "detached_running": any_detached
            }

        if all_connections:
            first_port, first_conn = next(iter(all_connections.items()))
            is_this_detached = first_conn.is_detached()
            return {
                "running": True,
                "connected": True,
                "port": first_conn.port,
                "device": first_conn.device,
                "core": first_conn.core,
                "manufacturer": first_conn.manufacturer,
                "version": first_conn.version,
                "device_root_fs": first_conn.device_root_fs,
                "in_raw_repl": first_conn.repl_protocol._in_raw_repl if first_conn.repl_protocol else False,
                "pid": os.getpid(),
                "busy": first_conn.busy or is_this_detached,
                "busy_command": first_conn.busy_command or ("detached_script" if is_this_detached else None),
                "detached_running": any_detached,
                "board_id": first_conn.board_id
            }
        
        return {
            "running": True,
            "connected": False,
            "port": "",
            "device": "",
            "core": "",
            "manufacturer": "",
            "version": "",
            "in_raw_repl": False,
            "pid": os.getpid(),
            "busy": False,
            "busy_command": None,
            "detached_running": False
        }
    
    def _cmd_shutdown(self, ctx: CommandContext = None) -> dict:
        self.running = False
        self.connection_manager.disconnect_all()        
        self.session_manager.clear_all_sessions()
        
        # Close socket to unblock _serve loop immediately
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
        
        return {"shutdown": True}
    
    def _cmd_reset(self, ctx: CommandContext) -> dict:
        conn = ctx.connection
        if not conn or not conn.repl_protocol:
            raise RuntimeError("Not connected")
        
        # Stop any detached script on this connection before reset
        if conn.is_detached():
            self._stop_detached_script(conn)
        
        conn.repl_protocol.reset()
        return {"reset": True}
    
    def _handle_input(self, msg: dict, client_addr: tuple, ppid: int = None, port: str = None):
        conn = None
        if port:
            conn = self.connection_manager.get_connection(port)
        else:
            for c in self.connection_manager.get_all_connections().values():
                if c.interactive.active and c.interactive.client_addr == client_addr:
                    conn = c
                    break
            if not conn and self._default_port:
                conn = self.connection_manager.get_connection(self._default_port)
        
        if not conn:
            return  
        
        with conn.interactive.lock:
            if not conn.interactive.active:
                return  
            
            if conn.interactive.client_addr != client_addr:
                return
            
            input_data = AgentProtocol.decode_stream_data(msg)
            if input_data:
                conn.interactive.input_queue.append(input_data)
    
    def _cmd_run_interactive(self, ctx: CommandContext, script_path: str = None, 
                              script_content: str = None, echo: bool = False):
        conn = ctx.connection
        seq = ctx.seq
        client_addr = ctx.client_addr
        
        def send_error(msg: str):
            error_response = AgentProtocol.create_response(seq=seq, error=msg)
            self.server_socket.sendto(AgentProtocol.encode_message(error_response), client_addr)
        
        if not conn or not conn.repl_protocol:
            send_error("Not connected")
            return
        
        with conn.interactive.lock:
            if conn.interactive.active:
                send_error("Interactive session already active on this connection")
                return
        
        # Reset board before running to ensure clean state
        repl = conn.repl_protocol
        if conn.is_detached():
            self._stop_detached_script(conn)
        repl.reset()
        
        if script_path:
            if not os.path.exists(script_path):
                send_error(f"Script not found: {script_path}")
                return
            with open(script_path, 'rb') as f:
                script_data = f.read()
        elif script_content:
            script_data = script_content.encode('utf-8') if isinstance(script_content, str) else script_content
        else:
            send_error("Either script_path or script_content required")
            return
        
        conn.interactive.start(ctx.ppid, seq, client_addr, echo)
        
        thread = threading.Thread(
            target=self._run_interactive_thread,
            args=(conn, script_data),
            daemon=True,
            name=f'Interactive-{conn.port}'
        )
        conn.interactive.thread = thread
        thread.start()
    
    def _safe_reset_repl(self, repl):
        """Reset REPL to clean state after execution."""
        try:
            repl.interrupt()
            time.sleep(0.05)
            repl.interrupt()
            time.sleep(0.1)
            repl._enter_repl()
        except Exception:
            pass
            
    def _run_interactive_thread(self, conn: BoardConnection, script_data: bytes):
        sock = self.server_socket
        client_addr = conn.interactive.client_addr
        seq = conn.interactive.seq
        
        if client_addr is None:
            return
        
        output_buffer = bytearray()
        buffer_lock = threading.Lock()
        last_flush_time = [time.time()]
        flush_timer_running = [True]
        BUFFER_FLUSH_SIZE = 4096
        FLUSH_INTERVAL = 0.05
        
        def send_stream(output: str = '', completed: bool = False, error: str = None):
            try:
                msg = {'type': 'stream', 'seq': seq, 'output': output}
                if completed:
                    msg['completed'] = True
                    msg['error'] = error
                sock.sendto(AgentProtocol.encode_message(msg), client_addr)
            except Exception:
                pass
        
        def flush_buffer():
            with buffer_lock:
                if not output_buffer:
                    return
                data_to_send = bytes(output_buffer)
                output_buffer.clear()
                last_flush_time[0] = time.time()
            send_stream(data_to_send.decode('utf-8', errors='replace'))
        
        first_chunk = [True]
        
        def data_consumer(chunk: bytes):
            if not chunk:
                return
            # Filter control chars and raw REPL prompt
            filtered = chunk.replace(EOF_MARKER, b'').replace(b'\r', b'')
            # Remove leading '>' (raw REPL prompt) from first chunk only
            if first_chunk[0] and filtered.startswith(b'>'):
                filtered = filtered[1:]
                first_chunk[0] = False
            elif first_chunk[0]:
                first_chunk[0] = False
            # Remove trailing '>' (raw REPL prompt after execution)
            if filtered.endswith(b'>'):
                filtered = filtered[:-1]
            if not filtered:
                return
            with buffer_lock:
                output_buffer.extend(filtered)
                should_flush = len(output_buffer) >= BUFFER_FLUSH_SIZE or \
                               time.time() - last_flush_time[0] >= FLUSH_INTERVAL
            if should_flush:
                flush_buffer()
        
        def flush_timer():
            while flush_timer_running[0]:
                time.sleep(FLUSH_INTERVAL)
                flush_buffer()
        
        flush_thread = threading.Thread(target=flush_timer, daemon=True)
        flush_thread.start()
        
        try:
            if not conn or not conn.repl_protocol:
                raise RuntimeError("Connection lost")
            repl = conn.repl_protocol
            
            input_thread_running = [True]
            
            def input_handler():
                while input_thread_running[0] and not conn.interactive.stop_requested:
                    input_data = None
                    with conn.interactive.lock:
                        if conn.interactive.input_queue:
                            input_data = conn.interactive.input_queue.pop(0)
                    
                    if input_data:
                        try:
                            if input_data == CTRL_C:
                                repl.interrupt()
                                repl._interrupt_requested = True
                            elif input_data == CTRL_D:
                                repl.send_eof()
                            elif input_data in (b'\n', b'\r'):
                                repl.send_raw(b'\r')
                            else:
                                repl.send_raw(input_data)
                        except Exception:
                            pass
                    time.sleep(0.01)
            
            input_thread = threading.Thread(target=input_handler, daemon=True)
            input_thread.start()
            
            try:
                if not repl._in_raw_repl:
                    repl._enter_repl()
                repl._exec(script_data, interactive=False, echo=False, detach=False, 
                          data_consumer=data_consumer)
            except ProtocolError as e:
                # Store error but don't duplicate - will be sent via completed message
                conn.interactive.error = str(e)
            finally:
                input_thread_running[0] = False
                flush_timer_running[0] = False
                flush_buffer()
            
            conn.interactive.completed = True
            # Send completion with error (error will be shown in panel, not duplicated in output)
            send_stream(completed=True, error=conn.interactive.error)
            self._safe_reset_repl(repl)
                
        except Exception as e:
            input_thread_running[0] = False
            flush_timer_running[0] = False
            conn.interactive.error = str(e)
            conn.interactive.completed = True
            send_stream(completed=True, error=str(e))
            if conn and conn.repl_protocol:
                self._safe_reset_repl(conn.repl_protocol)
        finally:
            input_thread_running[0] = False
            flush_timer_running[0] = False
            # Wait for threads to finish
            if input_thread.is_alive():
                input_thread.join(timeout=0.5)
            conn.interactive.stop()
            conn.release()
            with self._command_lock:
                self._command_in_progress = False
                self._last_command_time = time.time()
    
    def _cmd_run_stop(self, ctx: CommandContext) -> dict:
        conn = ctx.connection
        if not conn:
            return {"stopped": False, "reason": "Not connected"}
        
        with conn.interactive.lock:
            if not conn.interactive.active:
                return {"stopped": False, "reason": "No active session on this connection"}
            
            if not conn.interactive.is_owner(ctx.ppid):
                return {"stopped": False, "reason": "Not the session owner"}
            
            conn.interactive.stop_requested = True
            
            if conn.repl_protocol:
                try:
                    conn.repl_protocol.interrupt()
                    time.sleep(0.05)
                    conn.repl_protocol.interrupt()
                except Exception:
                    pass
        
        for _ in range(20):
            time.sleep(0.05)
            with conn.interactive.lock:
                if not conn.interactive.active:
                    return {"stopped": True}
        
        conn.interactive.stop()
        conn.release()
        
        return {"stopped": True}
    
    def _cmd_run(self, ctx: CommandContext, script_path: str = None, script_content: str = None, detach: bool = False) -> dict:
        conn = ctx.connection
        if not conn or not conn.repl_protocol:
            raise RuntimeError("Not connected")
        
        repl = conn.repl_protocol
        
        # Reset board before running to ensure clean state (unlike exec which preserves state)
        # Stop any detached script first
        if conn.is_detached():
            self._stop_detached_script(conn)
        
        # Perform soft reset
        repl.reset()
        
        # Wait for board to stabilize after reset
        time.sleep(0.1)
        
        # Load script
        if script_path:
            if not os.path.exists(script_path):
                raise FileNotFoundError(f"Script not found: {script_path}")
            with open(script_path, 'rb') as f:
                script_data = f.read()
            display_name = script_path
        elif script_content:
            script_data = script_content.encode('utf-8') if isinstance(script_content, str) else script_content
            display_name = "<inline>"
        else:
            raise RuntimeError("Either script_path or script_content required")
        
        if detach:
            # Non-interactive mode: send script and return immediately
            conn.busy = True
            conn.busy_command = 'detached_script'
            
            try:
                # Ensure we're in normal REPL mode
                try:
                    repl._leave_repl()
                except Exception:
                    pass
                repl._in_raw_repl = False
                
                # Clear any pending state
                repl.interrupt()
                time.sleep(0.05)
                repl.interrupt()
                time.sleep(0.05)
                repl.exit_paste_mode()
                time.sleep(0.1)
                repl.drain()
                
                # Enter paste mode and send script
                repl.enter_paste_mode()
                time.sleep(0.2)
                repl.drain()
                
                script_str = script_data.decode('utf-8', errors='replace')
                for line in script_str.split('\n'):
                    repl.send_raw(line.encode('utf-8') + b'\r')
                    time.sleep(0.01)
                
                time.sleep(0.1)
                repl.send_eof()
                
                self._start_drain_thread(conn)
                return {"run": True, "script": display_name, "detached": True}
                
            except Exception as e:
                conn.set_detached(False)
                conn.release()
                raise RuntimeError(f"Script send failed: {e}")
        else:
            # Blocking mode: execute and wait for result
            try:
                script_code = script_data.decode('utf-8', errors='replace')
                output = repl.exec(script_code)
                if isinstance(output, bytes):
                    output = output.decode('utf-8', errors='replace')
                return {"run": True, "script": display_name, "output": output}
            except Exception as e:
                raise RuntimeError(f"Script execution failed: {e}")
