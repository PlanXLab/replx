import json
import struct
import time
import base64
from typing import Dict, Any, Optional, List

from replx.utils.constants import MAX_UDP_SIZE, MAX_PAYLOAD_SIZE

# Protocol constants
_MAGIC = b'RPLX'
_VERSION = 1


class AgentProtocol:
    """UDP protocol for agent client-server communication."""

    @staticmethod
    def encode_message(msg: Dict[str, Any]) -> bytes:
        payload = json.dumps(msg).encode('utf-8')

        if len(payload) > MAX_PAYLOAD_SIZE:
            raise ValueError(f"Message too large: {len(payload)} bytes")

        header = (
            _MAGIC +
            struct.pack('!B', _VERSION) +
            struct.pack('!I', len(payload))
        )

        return header + payload

    @staticmethod
    def decode_message(data: bytes) -> Optional[Dict[str, Any]]:
        if len(data) < 9:
            return None

        if data[:4] != _MAGIC:
            return None

        version = struct.unpack('!B', data[4:5])[0]
        if version != _VERSION:
            return None

        length = struct.unpack('!I', data[5:9])[0]

        if len(data) < 9 + length:
            return None

        payload = data[9:9+length]
        return json.loads(payload.decode('utf-8'))

    @staticmethod
    def create_request(command: str, seq: int = None, ppid: int = None, port: str = None, **args) -> Dict[str, Any]:
        if seq is None:
            seq = int(time.time() * 1000000) % 0xFFFFFFFF

        request = {
            "seq": seq,
            "type": "request",
            "command": command,
            "args": args
        }

        if ppid is not None:
            request["ppid"] = ppid
        if port is not None:
            request["port"] = port

        return request

    @staticmethod
    def create_response(seq: int, result: Any = None, error: str = None) -> Dict[str, Any]:
        return {
            "seq": seq,
            "type": "response",
            "result": result,
            "error": error
        }

    @staticmethod
    def create_ack(seq: int) -> Dict[str, Any]:
        return {
            "seq": seq,
            "type": "ack"
        }

    @staticmethod
    def create_stream(seq: int, data: bytes, stream_type: str = "stdout") -> Dict[str, Any]:
        return {
            "seq": seq,
            "type": "stream",
            "data": base64.b64encode(data).decode('ascii'),
            "stream_type": stream_type
        }

    @staticmethod
    def create_progress_stream(seq: int, progress_data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "seq": seq,
            "type": "stream",
            "data": progress_data,
            "stream_type": "progress"
        }

    @staticmethod
    def create_input(seq: int, data: bytes, ppid: int = None, port: str = None) -> Dict[str, Any]:
        msg = {
            "seq": seq,
            "type": "input",
            "data": base64.b64encode(data).decode('ascii')
        }
        if ppid is not None:
            msg["ppid"] = ppid
        if port is not None:
            msg["port"] = port
        return msg

    @staticmethod
    def decode_stream_data(msg: Dict[str, Any]) -> bytes:
        data_b64 = msg.get('data', '')
        return base64.b64decode(data_b64) if data_b64 else b''

    @staticmethod
    def chunk_large_data(data: str, max_size: int = MAX_PAYLOAD_SIZE) -> List[str]:
        chunks = []
        data_bytes = data.encode('utf-8')

        chunk_size = max_size - 100

        for i in range(0, len(data_bytes), chunk_size):
            chunk = data_bytes[i:i+chunk_size].decode('utf-8', errors='ignore')
            chunks.append(chunk)

        return chunks
