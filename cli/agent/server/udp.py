import asyncio
import sys
import socket
import threading
from typing import Optional

from replx.cli.agent.protocol import AgentProtocol


LOOP_COMMANDS: frozenset[str] = frozenset({
    'ping',
    'status',
    'session_info',
})

FAST_COMMANDS: frozenset[str] = frozenset({
    'repl_read',
})


class AgentDatagramProtocol(asyncio.DatagramProtocol):
    __slots__ = ('_server',)

    def __init__(self, server) -> None:
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

        if msg is not None and command in LOOP_COMMANDS:
            server._handle_direct(msg, addr)
            return

        executor = (
            server._fast_executor
            if command in FAST_COMMANDS
            else server._slow_executor
        )
        executor.submit(server._handle_request, data, addr)

    def error_received(self, exc: Exception) -> None:
        if self._server.running:
            print(f'UDP protocol error: {exc}', file=sys.stderr)

    def connection_lost(self, exc: Exception | None) -> None:
        pass


class UdpReliabilityMixin:
    def _init_udp_reliability(
        self,
        *,
        response_cache_max: int = 512,
        last_seq_max: int = 256,
    ) -> None:
        self.last_seq: dict = {}
        self._last_seq_lock = threading.Lock()
        self._MAX_LAST_SEQ = last_seq_max

        self._send_socket: Optional[socket.socket] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._send_lock = threading.Lock()

        self._response_cache: dict[tuple, bytes] = {}
        self._response_cache_order: list[tuple] = []
        self._response_cache_lock = threading.Lock()
        self._RESPONSE_CACHE_MAX = response_cache_max

        self._stream_ack_events: dict[tuple, threading.Event] = {}
        self._stream_ack_lock = threading.Lock()

    def _safe_send(self, data: bytes, addr: tuple) -> None:
        """Send UDP response via dedicated unbound socket. Thread-safe."""
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
        """Send a completion message and wait for ``stream_ack`` from the client."""
        key = (client_addr, seq)
        event = threading.Event()
        with self._stream_ack_lock:
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

    def _ack_stream_completion(self, client_addr: tuple, seq: int) -> None:
        with self._stream_ack_lock:
            event = self._stream_ack_events.pop((client_addr, seq), None)
        if event is not None:
            event.set()

    def _check_and_record_seq(self, client_addr: tuple, seq: int) -> bool:
        with self._last_seq_lock:
            if client_addr in self.last_seq:
                last = self.last_seq[client_addr]
                if seq == last:
                    return False
            self.last_seq[client_addr] = seq
            if len(self.last_seq) > self._MAX_LAST_SEQ:
                oldest = next(iter(self.last_seq))
                del self.last_seq[oldest]
            return True
