import asyncio
import contextlib
import gc
import os
import sys
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any

from replx.utils.constants import (
    DEFAULT_AGENT_PORT, AGENT_HOST, MAX_CONNECTIONS, MAX_UDP_SIZE,
    AGENT_SOCKET_TIMEOUT, HEARTBEAT_INTERVAL,
    ZOMBIE_CHECK_INTERVAL, GC_THRESHOLD,
    GC_COLLECT_INTERVAL,
)
from replx.utils.exceptions import TransportError
from replx.commands import CmdGroups
from replx.cli.agent.protocol import AgentProtocol
from .connection_manager import ConnectionManager, BoardConnection
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
    I2cCommandsMixin,
    UartCommandsMixin,
    SpiCommandsMixin,
    DisconnectedError,
)

gc.set_threshold(*GC_THRESHOLD)

_LOOP_COMMANDS: frozenset[str] = frozenset({
    'ping',
    'status',
    'session_info',
})

_FAST_COMMANDS: frozenset[str] = frozenset({
    'repl_read',
})

_FAST_POOL_WORKERS = 4
_SLOW_POOL_WORKERS = 16


class _AgentDatagramProtocol(asyncio.DatagramProtocol):
    __slots__ = ('_server',)

    def __init__(self, server: 'AgentServer') -> None:
        self._server = server

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        server = self._server
        if not server.running:
            return
        try:
            msg = AgentProtocol.decode_message(data)
            command = msg.get('command') if msg else None
        except Exception:
            command = None
            msg = None

        if msg is not None and command in _LOOP_COMMANDS:
            server._handle_direct(msg, addr)
            return

        executor = (
            server._fast_executor
            if command in _FAST_COMMANDS
            else server._slow_executor
        )
        executor.submit(server._handle_request, data, addr)

    def error_received(self, exc: Exception) -> None:
        if self._server.running:
            print(f'UDP protocol error: {exc}', file=sys.stderr)

    def connection_lost(self, exc: Exception | None) -> None:
        pass


class AgentServer(
    SessionCommandsMixin,
    ExecCommandsMixin,
    FilesystemCommandsMixin,
    TransferCommandsMixin,
    ReplCommandsMixin,
    I2cCommandsMixin,
    UartCommandsMixin,
    SpiCommandsMixin,
):
    def __init__(self, port: int = None):
        self.agent_port = port or DEFAULT_AGENT_PORT
        self.running = False

        self.connection_manager = ConnectionManager()
        self.session_manager = SessionManager()
        self._i2c_bus: dict = {}
        self._uart_bus: dict = {}
        self._spi_bus: dict = {}

        self.last_seq: dict = {}
        self._last_seq_lock = threading.Lock()
        self._MAX_LAST_SEQ = 256

        self._command_handlers = self._build_command_handlers()

        self._fast_executor = ThreadPoolExecutor(
            max_workers=_FAST_POOL_WORKERS,
            thread_name_prefix='agent-fast',
        )
        self._slow_executor = ThreadPoolExecutor(
            max_workers=_SLOW_POOL_WORKERS,
            thread_name_prefix='agent-slow',
        )

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._datagram_transport: asyncio.DatagramTransport | None = None
        self._send_socket: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Guards _send_socket lifecycle so a concurrent close cannot null the
        # socket out from under an in-flight sendto in a worker thread (M3 fix).
        self._send_lock = threading.Lock()
        # Cache of recently sent responses keyed by (client_addr, seq) so that
        # a retransmitted request can be answered from cache instead of being
        # re-executed (m1 fix). LRU-evicted to bound memory.
        self._response_cache: "Dict[tuple, bytes]" = {}
        self._response_cache_order: list = []
        self._response_cache_lock = threading.Lock()
        self._RESPONSE_CACHE_MAX = 512
        # Stream completion ACK tracking — M1: handlers that send a final
        # ``stream`` message wait here for the client to acknowledge so that
        # a UDP packet loss is recovered by retransmission. Per-key events
        # support concurrent waiters.
        self._stream_ack_events: "Dict[tuple, threading.Event]" = {}
        self._stream_ack_lock = threading.Lock()
        self._cleaned_up: bool = False
    
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

    def _effective_default_port(self, session=None) -> Optional[str]:
        """Return the effective default port for a session: per-session
        default takes precedence, falling back to the global default (M4).
        """
        if session is not None and getattr(session, 'default_port', None):
            return session.default_port
        return self._default_port
    
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
    
    def start(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._cleaned_up = False
        print(f'replx agent starting (PID {os.getpid()})')
        print(f'UDP {AGENT_HOST}:{self.agent_port}')

        try:
            loop.run_until_complete(self._async_main())
        except (KeyboardInterrupt, SystemExit):
            print('\nShutting down...')
        finally:
            self._do_cleanup_resources()
            if not loop.is_closed():
                loop.close()
            self._loop = None
            self._stop_event = None

    async def _async_main(self) -> None:
        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        transport, _ = await loop.create_datagram_endpoint(
            lambda: _AgentDatagramProtocol(self),
            local_addr=(AGENT_HOST, self.agent_port),
        )
        self._datagram_transport = transport
        self.running = True
        print(f'replx agent started - listening on {AGENT_HOST}:{self.agent_port} (UDP)')

        heartbeat_task = asyncio.create_task(
            self._heartbeat_coro(), name='replx-heartbeat'
        )
        try:
            await self._stop_event.wait()
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(heartbeat_task), timeout=2.0)
            transport.close()
            self._do_cleanup_resources()

    async def _heartbeat_coro(self) -> None:
        loop = asyncio.get_running_loop()
        zombie_counter = 0
        gc_counter = 0
        while self.running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            zombie_counter, gc_counter = await loop.run_in_executor(
                self._slow_executor,
                self._heartbeat_tick,
                zombie_counter,
                gc_counter,
            )

    def _heartbeat_tick(self, zombie_counter: int, gc_counter: int) -> tuple[int, int]:
        all_conns = self.connection_manager.get_all_connections()
        zombie_counter += 1
        gc_counter += 1

        if zombie_counter >= ZOMBIE_CHECK_INTERVAL:
            self._cleanup_zombie_sessions()
            zombie_counter = 0

        if gc_counter >= GC_COLLECT_INTERVAL:
            gc.collect()
            gc_counter = 0

        # Reap stuck busy locks so a long-orphaned command (client crashed
        # mid-flight) cannot block other terminals forever.
        for conn in all_conns.values():
            try:
                conn.force_release_if_stale()
            except Exception:
                pass

        return zombie_counter, gc_counter
    
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
    
    def _safe_send(self, data: bytes, addr: tuple) -> None:
        """Send UDP response via dedicated unbound socket. Thread-safe.

        Holds ``_send_lock`` so a concurrent ``cleanup`` cannot close the
        socket between the truthy check and ``sendto``.
        """
        with self._send_lock:
            sock = self._send_socket
            if sock is None:
                return
            try:
                sock.sendto(data, addr)
            except Exception:
                pass

    def _cache_response(self, client_addr: tuple, seq: int, data: bytes) -> None:
        if data is None:
            return
        key = (client_addr, seq)
        with self._response_cache_lock:
            if key in self._response_cache:
                try:
                    self._response_cache_order.remove(key)
                except ValueError:
                    pass
            else:
                if len(self._response_cache_order) >= self._RESPONSE_CACHE_MAX:
                    oldest = self._response_cache_order.pop(0)
                    self._response_cache.pop(oldest, None)
            self._response_cache[key] = data
            self._response_cache_order.append(key)

    def _lookup_response(self, client_addr: tuple, seq: int) -> Optional[bytes]:
        with self._response_cache_lock:
            return self._response_cache.get((client_addr, seq))

    def send_completion_with_ack(
        self,
        encoded: bytes,
        client_addr: tuple,
        seq: int,
        max_retries: int = 5,
        interval_s: float = 0.1,
    ) -> bool:
        """Send a completion message and wait for ``stream_ack`` from the
        client, retransmitting up to ``max_retries`` times (M1 fix).

        Returns ``True`` if the client acknowledged, ``False`` otherwise.
        """
        key = (client_addr, seq)
        event = threading.Event()
        with self._stream_ack_lock:
            # If somehow a previous waiter still holds an event for the same
            # key, replace it — only the latest waiter is meaningful.
            self._stream_ack_events[key] = event

        try:
            for _ in range(max(1, max_retries)):
                self._safe_send(encoded, client_addr)
                if event.wait(timeout=interval_s):
                    return True
            return False
        finally:
            with self._stream_ack_lock:
                if self._stream_ack_events.get(key) is event:
                    self._stream_ack_events.pop(key, None)

    def _check_and_record_seq(self, client_addr: tuple, seq: int) -> bool:
        with self._last_seq_lock:
            if client_addr in self.last_seq:
                last = self.last_seq[client_addr]
                # Only reject exact duplicates from the same UDP source.
                #
                # Using strict monotonic ordering across client restarts can
                # incorrectly drop valid requests (e.g. process restart, seq
                # wrap, or source-port reuse), which then appears as
                # "No ACK from agent" on the client.
                if seq == last:
                    return False
            self.last_seq[client_addr] = seq
            if len(self.last_seq) > self._MAX_LAST_SEQ:
                oldest = next(iter(self.last_seq))
                del self.last_seq[oldest]
            return True

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
    
    def _encode_response_safe(self, response: dict) -> bytes:
        """Encode a response, truncating overly long error/result strings.

        ``encode_message`` raises ``ValueError`` if the JSON payload exceeds
        ``MAX_PAYLOAD_SIZE``. Long errors (eg. huge tracebacks or recursive
        listings) would otherwise propagate up and silently swallow the
        response (M2 fix).
        """
        from replx.utils.constants import MAX_PAYLOAD_SIZE
        try:
            return AgentProtocol.encode_message(response)
        except ValueError:
            seq = response.get('seq', 0) if isinstance(response, dict) else 0
            if isinstance(response, dict):
                err = response.get('error') or 'response too large'
                err_str = str(err)
                limit = max(256, MAX_PAYLOAD_SIZE - 1024)
                if len(err_str) > limit:
                    err_str = err_str[:limit] + '\n... [truncated]'
            else:
                err_str = 'response too large'
            fallback = AgentProtocol.create_response(seq=seq, error=err_str)
            try:
                return AgentProtocol.encode_message(fallback)
            except Exception:
                return AgentProtocol.encode_message(
                    AgentProtocol.create_response(seq=seq, error='response too large')
                )

    def _encode_ack(self, seq: int) -> bytes:
        return AgentProtocol.encode_message(AgentProtocol.create_ack(seq))

    def _send_duplicate_reply(self, client_addr: tuple, seq: int) -> None:
        """Reply to a duplicate request: replay cached response if we have
        one, otherwise just re-ACK so the client doesn't give up while the
        original handler is still running."""
        cached = self._lookup_response(client_addr, seq)
        if cached is not None:
            self._safe_send(cached, client_addr)
            return
        self._safe_send(self._encode_ack(seq), client_addr)

    def _handle_direct(self, msg: dict, client_addr: tuple) -> None:
        seq = msg.get('seq', 0)
        command = msg.get('command')

        if not self._check_and_record_seq(client_addr, seq):
            self._send_duplicate_reply(client_addr, seq)
            return

        self._safe_send(self._encode_ack(seq), client_addr)

        try:
            ppid = msg.get('ppid')
            explicit_port = msg.get('port')
            args = msg.get('args', {})
            ctx = CommandContext(
                connection=None,
                ppid=ppid,
                explicit_port=explicit_port,
                seq=seq,
                client_addr=client_addr,
            )
            handler, use_args = self._command_handlers[command]
            result = handler(ctx, **args) if use_args else handler(ctx)
            response = AgentProtocol.create_response(seq=seq, result=result)
        except Exception as exc:
            response = AgentProtocol.create_response(seq=seq, error=str(exc))

        encoded = self._encode_response_safe(response)
        self._cache_response(client_addr, seq, encoded)
        self._safe_send(encoded, client_addr)

    def _handle_request(self, data: bytes, client_addr: tuple):
        msg = None
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

            if msg_type == 'stream_ack':
                # Wake any handler waiting on a completion ACK (M1 fix).
                with self._stream_ack_lock:
                    event = self._stream_ack_events.pop((client_addr, seq), None)
                if event is not None:
                    event.set()
                return

            if msg_type == 'request':
                if not self._check_and_record_seq(client_addr, seq):
                    self._send_duplicate_reply(client_addr, seq)
                    return

            self._safe_send(self._encode_ack(seq), client_addr)

            response = self._handle_message(msg, client_addr)

            if response is not None:
                response_data = self._encode_response_safe(response)
                self._cache_response(client_addr, seq, response_data)
                self._safe_send(response_data, client_addr)

        except Exception as e:
            try:
                seq = msg.get('seq', 0) if isinstance(msg, dict) else 0
                error_response = AgentProtocol.create_response(seq=seq, error=str(e))
                error_data = self._encode_response_safe(error_response)
                self._cache_response(client_addr, seq, error_data)
                self._safe_send(error_data, client_addr)
            except Exception:
                pass
    
    def _build_command_handlers(self) -> dict:
        return {
            'release': (self._cmd_release, False),
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
            'i2c_bus_set': (self._cmd_i2c_bus_set, True),
            'i2c_bus_get': (self._cmd_i2c_bus_get, False),
            'i2c_bus_clear': (self._cmd_i2c_bus_clear, False),
            'uart_bus_set': (self._cmd_uart_bus_set, True),
            'uart_bus_get': (self._cmd_uart_bus_get, False),
            'uart_bus_clear': (self._cmd_uart_bus_clear, False),
            'spi_bus_set': (self._cmd_spi_bus_set, True),
            'spi_bus_get': (self._cmd_spi_bus_get, False),
            'spi_bus_clear': (self._cmd_spi_bus_clear, False),
        }

    def _cmd_disconnect_port(self, ctx: CommandContext, port: str = None) -> dict:
        target_port = port or ctx.explicit_port
        if not target_port:
            raise ValueError("Port required")
        
        conn = self.connection_manager.get_connection(target_port)
        if not conn:
            raise ValueError(f"Connection {target_port} not found")
        
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

        if command not in NON_REPL_COMMANDS and active_conn:
            if active_conn.repl.active and command not in CmdGroups.REPL:
                if not active_conn.repl.is_owner(ppid):
                    return AgentProtocol.create_response(
                        seq=seq,
                        error=f"Connection {active_conn.port} is busy. REPL session is active from another terminal. Exit REPL first with exit() or Ctrl+D."
                    )

        if command not in NON_REPL_COMMANDS and active_conn:
            if not active_conn.is_connected():
                return AgentProtocol.create_response(
                    seq=seq,
                    error=f"Connection {active_conn.port} was disconnected."
                )
            allow_when_detached = command in CmdGroups.DETACHED_ALLOW
            if not active_conn.acquire_for_command(
                ppid, command, client_addr,
                allow_when_detached=allow_when_detached,
            ):
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
                return AgentProtocol.create_response(seq=seq, error=f"Connection {failed_port} lost: {str(e)}. Connection removed.")
            else:
                return AgentProtocol.create_response(seq=seq, error=f"Connection error: {str(e)}")
        
        except Exception as e:
            return AgentProtocol.create_response(seq=seq, error=str(e))
        
        finally:
            if command not in NON_REPL_COMMANDS and command not in PERSISTENT_BUSY_COMMANDS and command not in STREAMING_COMMANDS:
                if active_conn:
                    active_conn.release()
    
    def cleanup(self) -> None:
        self.running = False
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and not loop.is_closed() and stop_event is not None:
            loop.call_soon_threadsafe(stop_event.set)
        else:
            self._do_cleanup_resources()

    def _do_cleanup_resources(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True

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

        transport = self._datagram_transport
        if transport is not None:
            try:
                if not transport.is_closing():
                    transport.close()
            except Exception:
                pass

        # Drain executor work BEFORE closing the send socket so that any
        # in-flight ``_safe_send`` from a worker doesn't observe a closed/None
        # socket (M3 fix). ``cancel_futures`` discards queued tasks; the brief
        # wait handles tasks already running.
        try:
            self._fast_executor.shutdown(wait=True, cancel_futures=True)
        except Exception:
            pass
        try:
            self._slow_executor.shutdown(wait=True, cancel_futures=True)
        except Exception:
            pass

        with self._send_lock:
            sock = self._send_socket
            self._send_socket = None
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

        print('replx agent stopped')


def main():
    port = None
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f'Invalid port: {sys.argv[1]}', file=sys.stderr)
            sys.exit(1)

    server = AgentServer(port=port)
    try:
        server.start() 
    except Exception as e:
        print(f'Fatal error: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
