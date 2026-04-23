import re
import sys
import time
import threading
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Tuple

from replx.protocol import ReplProtocol, create_storage
from replx.utils import parse_device_banner
from replx.utils.constants import CTRL_B, CTRL_C
from replx.utils.exceptions import TransportError


def _detect_device_info(transport, core: str, device: str = None) -> Tuple[str, str, str, str]:
    delay1 = 0.05 if sys.platform != "win32" else 0.1
    delay2 = 0.1 if sys.platform != "win32" else 0.2
    delay3 = 0.1 if sys.platform != "win32" else 0.2
    
    try:
        transport.write(b'\r' + CTRL_C)
        time.sleep(delay1)
        transport.reset_input_buffer()

        transport.write(b'\r' + CTRL_B)
        time.sleep(delay2)

        res = transport.read_available()
        if res:
            res_str = res.decode('utf-8', errors='replace') if isinstance(res, bytes) else res
            result = parse_device_banner(res_str)
            if result:
                return result

        transport.write(b'import sys; print(sys.version.split()[0])\r\n')
        time.sleep(delay3)
        ver_res = transport.read_available()
        version = "?"
        if ver_res:
            ver_str = ver_res.decode('utf-8', errors='replace') if isinstance(ver_res, bytes) else ver_res
            match = re.search(r'(\d+\.\d+(?:\.\d+)?)', ver_str)
            if match:
                version = match.group(1)

        return (version, core, core, "Unknown")

    except TransportError as e:
        print(f"Transport error during device detection: {e}", file=sys.stderr)
        return ("?", core, core, "Unknown")
    except (OSError, IOError) as e:
        print(f"I/O error during device detection: {e}", file=sys.stderr)
        return ("?", core, core, "Unknown")

@dataclass
class InteractiveSessionState:
    active: bool = False
    ppid: Optional[int] = None
    seq: int = 0
    client_addr: Optional[tuple] = None
    echo: bool = True
    input_queue: List[Any] = field(default_factory=list)
    stop_requested: bool = False
    thread: Optional[threading.Thread] = None
    completed: bool = False
    error: Optional[str] = None
    detached: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self, ppid: int, seq: int, client_addr: tuple, echo: bool = True):
        with self.lock:
            self.active = True
            self.ppid = ppid
            self.seq = seq
            self.client_addr = client_addr
            self.echo = echo
            self.input_queue = []
            self.stop_requested = False
            self.completed = False
            self.error = None
            self.detached = False

    def stop(self):
        thread_to_join = None
        with self.lock:
            self.active = False
            self.ppid = None
            self.seq = 0
            self.client_addr = None
            self.input_queue = []
            self.stop_requested = False
            thread_to_join = self.thread
            self.thread = None

        if thread_to_join and thread_to_join.is_alive() and thread_to_join is not threading.current_thread():
            thread_to_join.join(timeout=1)

    def is_owner(self, ppid: int) -> bool:
        with self.lock:
            if ppid is None:
                return False
            if self.ppid is None:
                return True
            return self.ppid == ppid

@dataclass
class ReplSessionState:
    active: bool = False
    ppid: Optional[int] = None
    reader_future: Optional[ConcurrentFuture] = None
    reader_thread: Optional[threading.Thread] = None
    output_buffer: bytes = b""
    buffer_lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self, ppid: int):
        self.active = True
        self.ppid = ppid
        with self.buffer_lock:
            self.output_buffer = b""

    def stop(self):
        self.active = False
        self.ppid = None
        if self.reader_future is not None:
            self.reader_future.cancel()
            self.reader_future = None
        if self.reader_thread:
            self.reader_thread.join(timeout=1)
            self.reader_thread = None
        with self.buffer_lock:
            self.output_buffer = b""

    def is_owner(self, ppid: int) -> bool:
        if ppid is None:
            return False
        if self.ppid is None:
            return True
        return self.ppid == ppid

    def append_output(self, data: bytes):
        with self.buffer_lock:
            self.output_buffer += data

    def read_output(self) -> bytes:
        with self.buffer_lock:
            data = self.output_buffer
            self.output_buffer = b""
            return data

@dataclass
class BoardConnection:
    port: str
    repl_protocol: Optional[ReplProtocol] = None
    file_system: Any = None

    core: str = ""
    device: str = ""
    manufacturer: str = "?"
    version: str = "?"
    device_root_fs: str = "/"
    
    board_id: Optional[str] = None

    busy: bool = False
    busy_command: Optional[str] = None
    busy_session: Optional[int] = None
    busy_client: Optional[tuple] = None
    last_command_time: float = field(default_factory=time.time)
    _busy_lock: threading.Lock = field(default_factory=threading.Lock)
    
    detached_running: bool = False
    _detached_lock: threading.Lock = field(default_factory=threading.Lock)
    _drain_thread: Optional[threading.Thread] = field(default=None, repr=False)

    interactive: InteractiveSessionState = field(default_factory=InteractiveSessionState)
    repl: ReplSessionState = field(default_factory=ReplSessionState)

    def is_connected(self) -> bool:
        return self.repl_protocol is not None
    
    def is_detached(self) -> bool:
        with self._detached_lock:
            return self.detached_running
    
    def set_detached(self, running: bool):
        with self._detached_lock:
            self.detached_running = running

    def acquire_for_command(self, session_id: int, command: str, client_addr: tuple = None) -> bool:
        with self._busy_lock:
            if self.busy:
                if self.busy_session == session_id:
                    return True
                return False
            self.busy = True
            self.busy_session = session_id
            self.busy_command = command
            self.busy_client = client_addr
            self.last_command_time = time.time()
            return True

    def release(self):
        with self._busy_lock:
            self.busy = False
            self.busy_session = None
            self.busy_command = None
            self.busy_client = None
    
    def stop_detached(self):
        with self._detached_lock:
            self.detached_running = False
        
        if self._drain_thread and self._drain_thread.is_alive():
            self._drain_thread.join(timeout=2.0)
        self._drain_thread = None

class ConnectionManager:
    def __init__(self):
        self._connections: Dict[str, BoardConnection] = {}
        self._connections_lock = threading.RLock()
        # Per-port events used to serialize concurrent create_serial_connection
        # calls for the same port (prevents double serial-open race).
        self._port_creating: Dict[str, threading.Event] = {}
        self._default_port: Optional[str] = None

    @property
    def default_port(self) -> Optional[str]:
        return self._default_port

    @default_port.setter
    def default_port(self, value: Optional[str]):
        self._default_port = self._canon_port(value) if value else None

    @staticmethod
    def _is_windows() -> bool:
        return sys.platform == "win32" or sys.platform.startswith("win")

    @staticmethod
    def _canon_port(port: Optional[str]) -> Optional[str]:
        if port is None:
            return None
        p = str(port).strip()
        if not p:
            return ""
        if ConnectionManager._is_windows() and re.match(r"(?i)^com\d+$", p):
            return p.upper()
        return p

    def _resolve_existing_key(self, port: str) -> Optional[str]:
        if port is None:
            return None
        p = self._canon_port(port)
        if p in self._connections:
            return p
        if self._is_windows() and re.match(r"(?i)^com\d+$", p or ""):
            needle = (p or "").lower()
            for k in self._connections.keys():
                if isinstance(k, str) and k.lower() == needle:
                    return k
        return None

    def get_connection(self, port: str) -> Optional[BoardConnection]:
        with self._connections_lock:
            key = self._resolve_existing_key(port)
            return self._connections.get(key) if key else None

    def get_all_connections(self) -> Dict[str, BoardConnection]:
        with self._connections_lock:
            return dict(self._connections)

    def get_connected_ports(self) -> List[str]:
        with self._connections_lock:
            return list(self._connections.keys())

    def has_connection(self, port: str) -> bool:
        with self._connections_lock:
            return self._resolve_existing_key(port) is not None

    def add_connection(self, port: str, connection: BoardConnection) -> None:
        with self._connections_lock:
            key = self._canon_port(port)
            connection.port = key
            self._connections[key] = connection

    def remove_connection(self, port: str) -> Optional[BoardConnection]:
        with self._connections_lock:
            key = self._resolve_existing_key(port)
            return self._connections.pop(key, None) if key else None

    def connection_count(self) -> int:
        with self._connections_lock:
            return len(self._connections)

    def create_serial_connection(
        self,
        port: str,
        core: str = "RP2350",
        device: str = None,
        baudrate: int = 115200
    ) -> Tuple[BoardConnection, Optional[str]]:
        original_port = str(port).strip() if port is not None else ""
        port_key = self._canon_port(original_port)

        # --- Phase 1: fast check + race serialisation ---
        with self._connections_lock:
            if port_key in self._connections:
                conn = self._connections[port_key]
                if conn.is_connected():
                    return conn, None
            # If another thread is already opening this port, get its event so
            # we can wait for it instead of opening the same serial port twice.
            if port_key in self._port_creating:
                wait_event = self._port_creating[port_key]
                our_event = None
            else:
                our_event = threading.Event()
                self._port_creating[port_key] = our_event
                wait_event = None

        if wait_event is not None:
            # Another thread is opening this port; wait then return its result.
            wait_event.wait(timeout=30)
            with self._connections_lock:
                conn = self._connections.get(port_key)
                if conn and conn.is_connected():
                    return conn, None
            return None, "Connection creation timed out (concurrent attempt)"

        # --- Phase 2: we are the creator ---
        try:
            from replx.transport import create_transport

            transport = create_transport(f"serial:{original_port}", baudrate=baudrate)

            version, detected_core, detected_device, manufacturer = _detect_device_info(
                transport, core, device
            )

            repl_protocol = ReplProtocol(transport)

            device_root_fs = "/"
            if version != "?":
                try:
                    result = repl_protocol.exec("import os; print(os.getcwd())")
                    if result:
                        if isinstance(result, bytes):
                            result = result.decode('utf-8', errors='ignore')
                        cwd = result.strip() if result else "/"
                    else:
                        cwd = "/"
                    if cwd.startswith('/flash'):
                        device_root_fs = '/flash/'
                    elif cwd.startswith('/'):
                        parts = cwd.split('/')
                        if len(parts) > 1 and parts[1]:
                            device_root_fs = f'/{parts[1]}/'
                except Exception:
                    pass

            file_system = create_storage(
                repl_protocol,
                core=detected_core,
                device=detected_device,
                device_root_fs=device_root_fs
            )

            conn = BoardConnection(
                port=port_key,
                repl_protocol=repl_protocol,
                file_system=file_system,
                core=detected_core,
                device=detected_device,
                manufacturer=manufacturer,
                version=version,
                device_root_fs=device_root_fs
            )

            with self._connections_lock:
                self._connections[port_key] = conn
                self._port_creating.pop(port_key, None)

            our_event.set()  # wake any threads that raced with us
            return conn, None

        except Exception as e:
            with self._connections_lock:
                self._port_creating.pop(port_key, None)
            our_event.set()  # unblock waiting threads so they get the error
            return None, str(e)

    def disconnect(self, port: str) -> bool:
        key = None
        with self._connections_lock:
            key = self._resolve_existing_key(port)
            if not key:
                return False

            conn = self._connections.pop(key)

        was_detached = conn.is_detached()
        conn.stop_detached()

        if conn.repl.active:
            conn.repl.stop()
        if conn.interactive.active:
            conn.interactive.stop()
        
        conn.release()

        if conn.repl_protocol:
            try:
                transport = conn.repl_protocol.transport
                if transport:
                    try:
                        if not was_detached:
                            transport.write(CTRL_B)
                            time.sleep(0.1 if sys.platform == 'win32' else 0.05)
                    except Exception:
                        pass
                    transport.close()
            except Exception:
                pass
            finally:
                conn.repl_protocol = None

        return True

    def disconnect_all(self):
        with self._connections_lock:
            ports = list(self._connections.keys())

        for port in ports:
            self.disconnect(port)

    def get_connection_info(self, port: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection(port)
        if not conn:
            return None

        return {
            'port': conn.port,
            'connected': conn.is_connected(),
            'core': conn.core,
            'device': conn.device,
            'manufacturer': conn.manufacturer,
            'version': conn.version,
            'device_root_fs': conn.device_root_fs,
            'busy': conn.busy,
            'busy_command': conn.busy_command,
            'board_id': conn.board_id
        }

    def get_all_connection_info(self) -> List[Dict[str, Any]]:
        result = []
        with self._connections_lock:
            for port in self._connections:
                info = self.get_connection_info(port)
                if info:
                    result.append(info)
        return result

    def set_busy(self, port: str, busy: bool, command: str = None, session: int = None):
        conn = self.get_connection(port)
        if conn:
            conn.busy = busy
            conn.busy_command = command if busy else None
            conn.busy_session = session if busy else None
            if busy:
                conn.last_command_time = time.time()

    def is_busy(self, port: str) -> bool:
        conn = self.get_connection(port)
        return conn.busy if conn else False

    def check_health(self, port: str) -> bool:
        conn = self.get_connection(port)
        if not conn or not conn.repl_protocol:
            return False

        try:
            transport = conn.repl_protocol.transport
            if not transport:
                return False
            
            if hasattr(transport, 'check_connection'):
                return transport.check_connection()
            
            return transport.is_open
        except TransportError:
            return False
        except Exception:
            return False
