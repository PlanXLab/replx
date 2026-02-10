import gc
import os
import sys
import socket
import threading
import time
from typing import Optional, Dict, Any

from replx.protocol import ReplProtocol, create_storage
from replx.utils import get_root_fs_for_core
from replx.utils.constants import (
    DEFAULT_AGENT_PORT, AGENT_HOST, MAX_CONNECTIONS, MAX_UDP_SIZE,
    AGENT_SOCKET_TIMEOUT, HEARTBEAT_INTERVAL,
    ZOMBIE_CHECK_INTERVAL, IDLE_COMMAND_THRESHOLD, GC_THRESHOLD,
    GC_COLLECT_INTERVAL,
)
from replx.utils.exceptions import TransportError
from replx.commands import CmdGroups
from replx.cli.agent.protocol import AgentProtocol
from .connection_manager import ConnectionManager, BoardConnection, _detect_device_info
from .session_manager import SessionManager, Session
from .command_dispatcher import (
    CommandContext,
    NON_REPL_COMMANDS,
    READ_ONLY_COMMANDS,
    SESSION_COMMANDS,
    PERSISTENT_BUSY_COMMANDS,
    STREAMING_COMMANDS,
)

from .handlers import (
    SessionCommandsMixin,
    ExecCommandsMixin,
    FilesystemCommandsMixin,
    TransferCommandsMixin,
    ReplCommandsMixin,
    DisconnectedError,
)

gc.set_threshold(*GC_THRESHOLD)


class AgentServer(
    SessionCommandsMixin,
    ExecCommandsMixin,
    FilesystemCommandsMixin,
    TransferCommandsMixin,
    ReplCommandsMixin
):
    def __init__(self, port: int = None):
        self.agent_port = port or DEFAULT_AGENT_PORT
        self.server_socket: Optional[socket.socket] = None
        self.running = False
        
        self.connection_manager = ConnectionManager()
        self.session_manager = SessionManager()
        
        self.last_seq = {}
        
        self._heartbeat_thread: Optional[threading.Thread] = None
        
        self._command_in_progress = False
        self._last_command_time = time.time()
        self._command_lock = threading.Lock()
        self._command_handlers = self._build_command_handlers()
    
    @property
    def _default_port(self) -> Optional[str]:
        return self.connection_manager.default_port
    
    @_default_port.setter
    def _default_port(self, value: Optional[str]):
        self.connection_manager.default_port = value
    
    def _to_real_path(self, virtual_path: str, conn: BoardConnection = None) -> str:
        device_root_fs = conn.device_root_fs if conn else "/"
        
        if device_root_fs == '/':
            return virtual_path
        
        if not virtual_path.startswith('/'):
            virtual_path = '/' + virtual_path
        
        if virtual_path == '/':
            return device_root_fs.rstrip('/')
        else:
            return device_root_fs.rstrip('/') + virtual_path
    
    def _to_virtual_path(self, real_path: str, conn: BoardConnection = None) -> str:
        device_root_fs = conn.device_root_fs if conn else "/"
        
        if device_root_fs == '/':
            return real_path
        
        root = device_root_fs.rstrip('/')
        if real_path == root:
            return '/'
        elif real_path.startswith(root + '/'):
            return real_path[len(root):]
        else:
            return real_path
    
    def _get_or_create_session(self, ppid: int) -> Session:
        return self.session_manager.get_or_create_session(ppid)
    
    def _get_session(self, ppid: int) -> Optional[Session]:
        return self.session_manager.get_session(ppid)
    
    def _resolve_connection_for_session(self, ppid: int, explicit_port: str = None) -> Optional[str]:
        return self.session_manager.resolve_port(ppid, explicit_port, self._default_port)
    
    def _cleanup_zombie_sessions(self):
        zombie_ppids = self.session_manager.cleanup_zombie_sessions()
        
        if not zombie_ppids:
            return
        
        orphaned_ports = set()
        all_conns = self.connection_manager.get_all_connections()
        
        for conn in all_conns.values():
            if conn.repl.active and conn.repl.ppid in zombie_ppids:
                conn.repl.stop()
                conn.release()
                orphaned_ports.add(conn.port)
            
            if conn.interactive.active and conn.interactive.ppid in zombie_ppids:
                conn.interactive.stop()
                conn.release()
                orphaned_ports.add(conn.port)
            
            if conn.busy and conn.busy_session in zombie_ppids:
                conn.release()
                orphaned_ports.add(conn.port)
        
        for port in orphaned_ports:
            sessions_using_port = self.session_manager.find_sessions_using_port(port)
            if not sessions_using_port:
                self.connection_manager.disconnect(port)
    
    def _get_session_info(self) -> Dict[str, Any]:
        sessions_info = []
        sessions = self.session_manager.get_all_sessions()
        connections = self.connection_manager.get_all_connections()
        
        for ppid, session in sessions.items():
            session_info = {
                'ppid': ppid,
                'foreground': session.foreground,
                'backgrounds': list(session.backgrounds),
                'last_access': session.last_access,
                'default_port': session.default_port
            }
            sessions_info.append(session_info)
        
        connections_info = []
        any_detached = False
        for port, conn in connections.items():
            referencing_sessions = self.session_manager.find_sessions_using_port(port)
            is_detached_port = conn.is_detached()
            if is_detached_port:
                any_detached = True
            conn_info = {
                'port': port,
                'device': conn.device,
                'core': conn.core,
                'version': conn.version,
                'manufacturer': getattr(conn, 'manufacturer', ''),
                'busy': conn.busy or is_detached_port,
                'busy_command': conn.busy_command or ("detached_script" if is_detached_port else None),
                'connected': conn.is_connected(),
                'sessions': referencing_sessions
            }
            connections_info.append(conn_info)
        
        return {
            'sessions': sessions_info,
            'connections': connections_info,
            'default_port': self._default_port,
            'detached_running': any_detached
        }
    
    def _set_default_port(self, port: str):
        self.connection_manager.default_port = port if port else None
    
    def _get_connection(self, port: str) -> Optional[BoardConnection]:
        return self.connection_manager.get_connection(port)
    
    def _get_active_connection(self, ppid: int = None, explicit_port: str = None) -> Optional[BoardConnection]:
        if explicit_port:
            conn = self.connection_manager.get_connection(explicit_port)
            if conn:
                return conn
        
        if ppid:
            session = self.session_manager.get_session(ppid)
            if session and session.foreground:
                conn = self.connection_manager.get_connection(session.foreground)
                if conn:
                    return conn
        
        return None
    
    def _add_connection(self, port: str, connection: BoardConnection) -> None:
        current_count = self.connection_manager.connection_count()
        if current_count >= MAX_CONNECTIONS:
            raise ValueError(f"Maximum connections ({MAX_CONNECTIONS}) reached. Free some connections first.")
        self.connection_manager.add_connection(port, connection)
    
    def _remove_connection(self, port: str) -> Optional[BoardConnection]:
        return self.connection_manager.remove_connection(port)
    
    def _get_all_connections(self) -> Dict[str, BoardConnection]:
        return self.connection_manager.get_all_connections()
    
    def _create_board_connection(self, port: str, 
                                  core: str = "RP2350", device: str = None,
                                  device_root_fs: str = "/") -> BoardConnection:
        if not port:
            raise ValueError("Port is required")
        
        # Use ConnectionManager's create_serial_connection which properly detects device info
        # before entering raw REPL mode
        board_conn, error = self.connection_manager.create_serial_connection(
            port=port,
            core=core,
            device=device,
            baudrate=115200
        )
        
        if error:
            raise RuntimeError(f"Failed to create connection: {error}")
        
        if not board_conn:
            raise RuntimeError("Failed to create connection: unknown error")
        
        return board_conn
    
    def start(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server_socket.settimeout(AGENT_SOCKET_TIMEOUT)
        self.server_socket.bind((AGENT_HOST, self.agent_port))
        
        self.running = True
        print(f"replx agent started (PID {os.getpid()})")
        print(f"Listening on {AGENT_HOST}:{self.agent_port} (UDP)")
        
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        
        try:
            self._serve()
        finally:
            self.cleanup()
    
    def _serve(self):
        while self.running:
            try:
                data, client_addr = self.server_socket.recvfrom(MAX_UDP_SIZE)
                
                thread = threading.Thread(
                    target=self._handle_request,
                    args=(data, client_addr),
                    daemon=True
                )
                thread.start()
                
            except socket.timeout:
                continue
            except OSError as e:
                if not self.running:
                    break
                print(f"Socket error: {e}", file=sys.stderr)
            except Exception as e:
                if self.running:
                    print(f"Server error: {e}", file=sys.stderr)
    
    def _heartbeat_loop(self):
        zombie_check_counter = 0
        gc_counter = 0
        while self.running:
            time.sleep(HEARTBEAT_INTERVAL)
            zombie_check_counter += 1
            gc_counter += 1
            
            if zombie_check_counter >= ZOMBIE_CHECK_INTERVAL:
                self._cleanup_zombie_sessions()
                zombie_check_counter = 0
            
            if gc_counter >= GC_COLLECT_INTERVAL:
                gc.collect()
                gc_counter = 0
            
            all_conns = self.connection_manager.get_all_connections()
            if not all_conns:
                continue
            
            has_active_session = any(
                conn.interactive.active or conn.repl.active for conn in all_conns.values()
            )
            if has_active_session:
                continue
            
            with self._command_lock:
                elapsed = time.time() - self._last_command_time
                if self._command_in_progress or elapsed < IDLE_COMMAND_THRESHOLD:
                    continue
            
            failed_ports = []
            for port, conn in all_conns.items():
                if not self.connection_manager.has_connection(port):
                    continue
                
                if conn.repl.active or conn.interactive.active:
                    continue
                
                if conn.is_detached() or conn.busy:
                    continue
                
                if not conn.acquire_for_command(session_id=0, command='heartbeat'):
                    continue
                
                try:
                    if not self.connection_manager.has_connection(port):
                        continue
                    if conn.repl.active or conn.interactive.active:
                        continue
                    if conn.repl_protocol:
                        conn.repl_protocol.exec("pass")
                except Exception:
                    failed_ports.append(port)
                finally:
                    conn.release() 
            
            for port in failed_ports:
                self.connection_manager.disconnect(port)
                self.session_manager.remove_connection_from_all_sessions(port)
            
            if not self.connection_manager.get_connected_ports():
                self.running = False
                continue
            
            with self._command_lock:
                self._last_command_time = time.time()
    
    def _start_drain_thread(self, conn: BoardConnection):
        conn.set_detached(True)
        
        def drain_loop():
            tail_buffer = b""
            for _ in range(50):
                if not conn.is_detached():
                    return
                time.sleep(0.01)
            
            while conn.is_detached() and self.running:
                try:
                    if conn.repl_protocol:
                        data = conn.repl_protocol.drain()
                        if data:
                            tail_buffer = (tail_buffer + data)[-50:]
                            
                            if b'\r\n>>> ' in tail_buffer or tail_buffer.endswith(b'>>> '):
                                conn.set_detached(False)
                                conn.release()
                                break
                except Exception:
                    break
                time.sleep(0.01)
        
        drain_thread = threading.Thread(target=drain_loop, daemon=True)
        conn._drain_thread = drain_thread
        drain_thread.start()
    
    def _stop_drain_thread(self, conn: BoardConnection):
        conn.stop_detached()
    
    def _stop_detached_script(self, conn: BoardConnection = None):
        if conn:
            if not conn.is_detached():
                return
            
            conn.stop_detached()
            
            if conn.repl_protocol:
                try:
                    conn.release()
                    conn.repl_protocol.interrupt()
                    time.sleep(0.05)
                    conn.repl_protocol.interrupt()
                    time.sleep(0.1)
                    conn.repl_protocol.drain()
                except Exception:
                    pass
        else:
            all_conns = self.connection_manager.get_all_connections()
            for port, c in all_conns.items():
                if c.is_detached():
                    self._stop_detached_script(c)
    
    def _handle_request(self, data: bytes, client_addr: tuple):
        try:
            msg = AgentProtocol.decode_message(data)
            if not msg:
                print(f"Invalid message from {client_addr}", file=sys.stderr)
                return
            
            seq = msg.get('seq', 0)
            msg_type = msg.get('type', 'request')
            
            if msg_type == 'input':
                ppid = msg.get('ppid')
                port = msg.get('port')
                self._handle_input(msg, client_addr, ppid, port)
                return
            
            if msg_type == 'request':
                if client_addr in self.last_seq and seq <= self.last_seq[client_addr]:
                    return
                
                self.last_seq[client_addr] = seq
            
            ack = AgentProtocol.create_ack(seq)
            ack_data = AgentProtocol.encode_message(ack)
            self.server_socket.sendto(ack_data, client_addr)
            
            response = self._handle_message(msg, client_addr)
            
            if response is not None:
                response_data = AgentProtocol.encode_message(response)
                self.server_socket.sendto(response_data, client_addr)
            
        except Exception as e:
            try:
                error_response = AgentProtocol.create_response(
                    seq=msg.get('seq', 0) if msg else 0,
                    error=str(e)
                )
                error_data = AgentProtocol.encode_message(error_response)
                self.server_socket.sendto(error_data, client_addr)
            except Exception:
                pass
    
    def _build_command_handlers(self) -> dict:
        return {
            'free': (self._cmd_free, False),
            'disconnect_port': (self._cmd_disconnect_port, True),
            'exec': (self._cmd_exec, True),
            'status': (self._cmd_status, False),
            'shutdown': (self._cmd_shutdown, False),
            'ping': (lambda ctx: {"pong": True}, False),
            'reset': (self._cmd_reset, False),
            'run': (self._cmd_run, True),
            'run_stop': (self._cmd_run_stop, False),
            'ls': (self._cmd_ls, True),
            'ls_recursive': (self._cmd_ls_recursive, True),
            'cat': (self._cmd_cat, True),
            'rm': (self._cmd_rm, True),
            'rmdir': (self._cmd_rmdir, True),
            'mkdir': (self._cmd_mkdir, True),
            'is_dir': (self._cmd_is_dir, True),
            'mem': (self._cmd_mem, False),
            'cp': (self._cmd_cp, True),
            'mv': (self._cmd_mv, True),
            'df': (self._cmd_df, False),
            'touch': (self._cmd_touch, True),
            'format': (self._cmd_format, False),
            'get_file': (self._cmd_get_file, True),
            'get_to_local': (self._cmd_get_to_local, True),
            'put_file': (self._cmd_put_file, True),
            'put_from_local': (self._cmd_put_from_local, True),
            'putdir_from_local': (self._cmd_putdir_from_local, True),
            'put_file_batch': (self._cmd_put_file_batch, True),
            'get_file_batch': (self._cmd_get_file_batch, True),
            'stat': (self._cmd_stat, True),
            'repl_enter': (self._cmd_repl_enter, False),
            'repl_exit': (self._cmd_repl_exit, False),
            'repl_write': (self._cmd_repl_write, True),
            'repl_read': (self._cmd_repl_read, False),
            'session_info': (lambda ctx: self._get_session_info(), False),
            'session_setup': (self._cmd_session_setup, True),
            'session_disconnect': (self._cmd_session_disconnect, True),
            'session_switch_fg': (self._cmd_session_switch_fg, False),
            'run_interactive': (self._cmd_run_interactive, True),
            'getdir_to_local': (self._cmd_getdir_to_local_streaming, True),
            'put_from_local_streaming': (self._cmd_put_from_local_streaming, True),
            'putdir_from_local_streaming': (self._cmd_putdir_from_local_streaming, True),
        }

    def _cmd_disconnect_port(self, ctx: CommandContext, port: str = None) -> dict:
        target_port = port or ctx.explicit_port
        if not target_port:
            raise ValueError("Port required")
        
        conn = self.connection_manager.get_connection(target_port)
        if not conn:
            raise ValueError(f"Connection {target_port} not found")
        
        # Disconnect handles stopping detached script and drain thread via stop_detached()
        self.connection_manager.disconnect(target_port)
        self.session_manager.remove_connection_from_all_sessions(target_port)
        
        return {"disconnected": target_port}

    def _update_session_connections(self, ppid: int, explicit_port: str) -> None:
        session = self._get_or_create_session(ppid)
        if explicit_port and explicit_port not in [c for c in session.get_all_connections()]:
            conn_exists = self._get_connection(explicit_port) is not None
            
            if conn_exists:
                if session.foreground is None:
                    if self._default_port and self._default_port != explicit_port:
                        default_conn_exists = self._get_connection(self._default_port) is not None
                        
                        if default_conn_exists:
                            session.add_connection(self._default_port, as_foreground=True)
                            session.add_connection(explicit_port, as_foreground=False)
                        else:
                            session.add_connection(explicit_port, as_foreground=True)
                    else:
                        session.add_connection(explicit_port, as_foreground=True)
                else:
                    session.add_connection(explicit_port, as_foreground=False)

    def _validate_connection(self, ppid: int, explicit_port: str, seq: int) -> tuple:
        active_conn = self._get_active_connection(ppid, explicit_port)
        
        if explicit_port and not active_conn:
            return None, AgentProtocol.create_response(
                seq=seq,
                error=f"Connection {explicit_port} not found. Run 'replx --port {explicit_port} setup' first."
            )
        
        if explicit_port and active_conn and not active_conn.repl_protocol:
            return None, AgentProtocol.create_response(
                seq=seq,
                error=f"Connection {explicit_port} is not properly connected."
            )
        
        return active_conn, None

    def _handle_message(self, msg: dict, client_addr: tuple = None) -> dict:
        command = msg.get('command')
        args = msg.get('args', {})
        seq = msg.get('seq', 0)
        ppid = msg.get('ppid')
        explicit_port = msg.get('port')
        
        if ppid and command not in READ_ONLY_COMMANDS and command not in SESSION_COMMANDS:
            self._update_session_connections(ppid, explicit_port)
        
        if command not in NON_REPL_COMMANDS and command not in CmdGroups.DETACHED_ALLOW:
            target_port = None
            if explicit_port:
                target_port = explicit_port
            elif ppid:
                session = self.session_manager.get_session(ppid)
                if session and session.foreground:
                    target_port = session.foreground
            
            if target_port:
                target_conn = self.connection_manager.get_connection(target_port)
                if target_conn and target_conn.is_detached():
                    return AgentProtocol.create_response(
                        seq=seq,
                        error=f"Connection {target_port} is busy. A detached script is running. Use 'replx reset' to stop it first."
                    )
        
        active_conn = None
        if command not in NON_REPL_COMMANDS and ppid:
            active_conn, error_response = self._validate_connection(ppid, explicit_port, seq)
            if error_response:
                return error_response
        
        ctx = CommandContext(
            connection=active_conn,
            ppid=ppid,
            explicit_port=explicit_port,
            seq=seq,
            client_addr=client_addr
        )
        
        with self._command_lock:
            self._command_in_progress = True
        
        if command not in NON_REPL_COMMANDS and active_conn:
            if active_conn.repl.active and command not in CmdGroups.REPL:
                if not active_conn.repl.is_owner(ppid):
                    with self._command_lock:
                        self._command_in_progress = False
                    return AgentProtocol.create_response(
                        seq=seq,
                        error=f"Connection {active_conn.port} is busy. REPL session is active from another terminal. Exit REPL first with exit() or Ctrl+D."
                    )
        
        if command not in NON_REPL_COMMANDS and active_conn:
            if not active_conn.is_connected():
                with self._command_lock:
                    self._command_in_progress = False
                return AgentProtocol.create_response(
                    seq=seq,
                    error=f"Connection {active_conn.port} was disconnected."
                )
            if not active_conn.acquire_for_command(ppid, command, client_addr):
                with self._command_lock:
                    self._command_in_progress = False
                return AgentProtocol.create_response(
                    seq=seq,
                    error=f"Connection {active_conn.port} is busy. Another command ({active_conn.busy_command}) is currently running. Please wait for it to complete or press Ctrl+C to cancel."
                )
        
        try:
            if command == 'connect':
                connect_port = args.get('port') or explicit_port
                result = self._cmd_session_setup(
                    ctx,
                    port=connect_port,
                    core=args.get('core', 'RP2350'),
                    device=args.get('device'),
                    as_foreground=True
                )
            elif command == 'set_default':
                port_arg = args.get('port') or explicit_port
                result = self._cmd_set_default(ctx, port=port_arg)
            elif command in self._command_handlers:
                handler, use_args = self._command_handlers[command]
                if command in STREAMING_COMMANDS:
                    handler(ctx, **args) if use_args else handler(ctx)
                    return None
                result = handler(ctx, **args) if use_args else handler(ctx)
            else:
                raise ValueError(f"Unknown command: {command}")
            
            return AgentProtocol.create_response(seq=seq, result=result)
        
        except (TransportError, DisconnectedError, ConnectionError, OSError, BrokenPipeError) as e:
            if active_conn:
                failed_port = active_conn.port
                self.connection_manager.disconnect(failed_port)
                self.session_manager.remove_connection_from_all_sessions(failed_port)
                if not self.connection_manager.get_connected_ports():
                    self.running = False
                    return AgentProtocol.create_response(seq=seq, error=f"Connection {failed_port} lost: {str(e)}. No connections remain, agent shutting down.")
                return AgentProtocol.create_response(seq=seq, error=f"Connection {failed_port} lost: {str(e)}. Connection removed.")
            else:
                return AgentProtocol.create_response(seq=seq, error=f"Connection error: {str(e)}")
        
        except Exception as e:
            return AgentProtocol.create_response(seq=seq, error=str(e))
        
        finally:
            if command not in NON_REPL_COMMANDS and command not in PERSISTENT_BUSY_COMMANDS and command not in STREAMING_COMMANDS:
                if active_conn:
                    active_conn.release()
            
            with self._command_lock:
                self._command_in_progress = False
                self._last_command_time = time.time()
    
    def cleanup(self):
        self.running = False
        
        for conn in self.connection_manager.get_all_connections().values():
            if conn.is_detached():
                conn.stop_detached()
        
        for conn in self.connection_manager.get_all_connections().values():
            if conn.repl.active:
                conn.repl.stop()
            if conn.interactive.active:
                conn.interactive.stop()
        
        self.connection_manager.disconnect_all()        
        self.session_manager.clear_all_sessions()
        
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass 
        
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2)
        
        print("replx agent stopped")


def main():
    port = None
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"Invalid port: {sys.argv[1]}", file=sys.stderr)
            sys.exit(1)
    
    server = AgentServer(port=port)
    
    try:
        server.start()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.cleanup()
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
