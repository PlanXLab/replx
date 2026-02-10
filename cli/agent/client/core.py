import os
import sys
import socket
import time
from typing import Dict, Any, Optional, Callable

from replx.utils.constants import DEFAULT_AGENT_PORT, AGENT_HOST, MAX_UDP_SIZE
from replx.commands import Cmd
from replx.cli.agent.protocol import AgentProtocol
from .session import get_cached_session_id

LOCAL_PATH_PARAMS = frozenset({'local_path', 'local'})


class AgentClient:    
    TIMEOUT = 5.0
    MAX_RETRIES = 3

    def __init__(self, port: int = None, device_port: str = None):
        self.agent_port = port or DEFAULT_AGENT_PORT
        self.device_port = device_port
        self.sock: Optional[socket.socket] = None

        self._ppid = get_cached_session_id()

    def connect(self):
        if not self.sock:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(self.TIMEOUT)

    def disconnect(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def send_command(self, command: str, timeout: float = None, **args) -> Dict[str, Any]:
        if not self.sock:
            self.connect()

        effective_timeout = timeout if timeout else self.TIMEOUT

        if timeout:
            self.sock.settimeout(timeout)

        port_to_use = args.pop('port', None) or self.device_port

        for param in LOCAL_PATH_PARAMS:
            if param in args and args[param]:
                args[param] = os.path.abspath(args[param])

        request = AgentProtocol.create_request(
            command,
            ppid=self._ppid,
            port=port_to_use,
            **args
        )
        seq = request['seq']
        request_data = AgentProtocol.encode_message(request)

        response = None
        
        # For short timeouts (< 1s), don't retry to fail fast (e.g., ping/agent check)
        max_attempts = 1 if effective_timeout < 1.0 else self.MAX_RETRIES

        for attempt in range(max_attempts):
            try:
                self.sock.sendto(request_data, (AGENT_HOST, self.agent_port))

                start_time = time.time()
                while time.time() - start_time < effective_timeout:
                    try:
                        data, addr = self.sock.recvfrom(MAX_UDP_SIZE)

                        msg = AgentProtocol.decode_message(data)
                        if not msg or msg.get('seq') != seq:
                            continue

                        if msg.get('type') == 'ack':
                            continue

                        if msg.get('type') == 'response':
                            response = msg
                            break

                    except socket.timeout:
                        break

                if response:
                    break

                if attempt < max_attempts - 1 and effective_timeout >= 1.0:
                    time.sleep(0.1 * (attempt + 1))

            except socket.timeout:
                if attempt < max_attempts - 1:
                    continue
                raise RuntimeError(f"Agent timeout after {max_attempts} attempts")

        if not response:
            raise RuntimeError("No response from agent")

        if response.get('error'):
            raise RuntimeError(response['error'])

        return response.get('result', {})

    def run_interactive(self, script_path: str = None, script_content: str = None,
                        echo: bool = False,
                        output_callback: Callable[[bytes, str], None] = None,
                        input_provider: Callable[[], Optional[bytes]] = None,
                        stop_check: Callable[[], bool] = None) -> Dict[str, Any]:
        if not self.sock:
            self.connect()

        if script_path:
            script_path = os.path.abspath(script_path)

        request = AgentProtocol.create_request(
            'run_interactive',
            ppid=self._ppid,
            port=self.device_port,
            script_path=script_path,
            script_content=script_content,
            echo=echo
        )
        seq = request['seq']
        request_data = AgentProtocol.encode_message(request)

        self.sock.sendto(request_data, (AGENT_HOST, self.agent_port))

        self.sock.settimeout(5.0)
        ack_received = False
        error_response = None

        while True:
            try:
                data, addr = self.sock.recvfrom(MAX_UDP_SIZE)
                msg = AgentProtocol.decode_message(data)
                if msg and msg.get('seq') == seq:
                    if msg.get('type') == 'ack':
                        ack_received = True
                        break
                    elif msg.get('type') == 'response' and msg.get('error'):
                        error_response = msg
                        break
            except socket.timeout:
                break

        if error_response:
            raise RuntimeError(error_response['error'])

        if not ack_received:
            raise RuntimeError("No ACK from agent - run_interactive failed to start")

        self.sock.settimeout(0.01)
        input_interval = 0.001
        last_input_time = 0
        error_check_until = time.time() + 0.1

        try:
            while True:
                if stop_check and stop_check():
                    try:
                        self.send_command(Cmd.RUN_STOP, timeout=0.5)
                    except Exception:
                        pass
                    break

                now = time.time()

                if now - last_input_time >= input_interval:
                    last_input_time = now
                    if input_provider:
                        try:
                            input_data = input_provider()
                            if input_data:
                                input_msg = AgentProtocol.create_input(seq, input_data, ppid=self._ppid, port=self.device_port)
                                input_data_encoded = AgentProtocol.encode_message(input_msg)
                                self.sock.sendto(input_data_encoded, (AGENT_HOST, self.agent_port))
                        except Exception:
                            pass

                try:
                    data, addr = self.sock.recvfrom(MAX_UDP_SIZE)
                    msg = AgentProtocol.decode_message(data)

                    if msg and msg.get('seq') == seq:
                        if now < error_check_until and msg.get('type') == 'response' and msg.get('error'):
                            raise RuntimeError(msg['error'])

                        if msg.get('type') == 'stream':
                            output = msg.get('output', '')
                            if output and output_callback:
                                output_callback(output.encode('utf-8'), 'stdout')

                            if msg.get('completed'):
                                error = msg.get('error')
                                if error and output_callback:
                                    output_callback(error.encode('utf-8'), 'stderr')
                                break

                except socket.timeout:
                    pass
                except Exception:
                    pass

        except KeyboardInterrupt:
            try:
                self.send_command(Cmd.RUN_STOP, timeout=0.5)
            except Exception:
                pass
            raise

        self.sock.settimeout(self.TIMEOUT)

        return {"run": True, "completed": True}

    def send_command_streaming(self, command: str, timeout: float = None,
                                progress_callback: Callable[[Dict[str, Any]], None] = None,
                                **args) -> Dict[str, Any]:
        if not self.sock:
            self.connect()

        effective_timeout = timeout if timeout else 60.0

        port_to_use = args.pop('port', None) or self.device_port

        for param in LOCAL_PATH_PARAMS:
            if param in args and args[param]:
                args[param] = os.path.abspath(args[param])

        request = AgentProtocol.create_request(
            command,
            ppid=self._ppid,
            port=port_to_use,
            **args
        )
        seq = request['seq']
        request_data = AgentProtocol.encode_message(request)

        self.sock.sendto(request_data, (AGENT_HOST, self.agent_port))

        self.sock.settimeout(0.1)

        ack_received = False
        response = None
        start_time = time.time()

        while time.time() - start_time < effective_timeout:
            try:
                data, addr = self.sock.recvfrom(MAX_UDP_SIZE)
                msg = AgentProtocol.decode_message(data)

                if not msg or msg.get('seq') != seq:
                    continue

                msg_type = msg.get('type')

                if msg_type == 'ack':
                    ack_received = True
                    continue

                elif msg_type == 'stream':
                    if progress_callback:
                        stream_data = msg.get('data', {})
                        progress_callback(stream_data)
                    continue

                elif msg_type == 'response':
                    response = msg
                    break

            except socket.timeout:
                if not ack_received and time.time() - start_time > 5.0:
                    raise RuntimeError("No response from agent")
                continue
            except Exception as e:
                raise RuntimeError(f"Communication error: {e}")

        self.sock.settimeout(self.TIMEOUT)

        if not response:
            raise RuntimeError("No response from agent (timeout)")

        if response.get('error'):
            raise RuntimeError(response['error'])

        return response.get('result', {})

    def ping(self) -> bool:
        try:
            result = self.send_command(Cmd.PING, timeout=0.3)
            return result.get('pong', False)
        except Exception:
            return False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        self.disconnect()

    @staticmethod
    def is_agent_running(port: int = None) -> bool:
        try:
            client = AgentClient(port=port)
            return client.ping()
        except Exception:
            return False

    @staticmethod
    def start_agent(port: int = None, background: bool = True) -> bool:
        if AgentClient.is_agent_running(port=port):
            return False

        import subprocess
        python_exe = sys.executable
        agent_module = 'replx.cli.agent.server'

        cmd = [python_exe, '-m', agent_module]
        if port:
            cmd.append(str(port))

        if background:
            if sys.platform == 'win32':
                # Windows: Use DETACHED_PROCESS to run without console
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0

                subprocess.Popen(
                    cmd,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    startupinfo=startupinfo
                )
            else:
                # Unix: Use start_new_session for proper daemon behavior
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True
                )
        else:
            subprocess.Popen(cmd)

        for i in range(30):
            time.sleep(0.1)
            if AgentClient.is_agent_running(port=port):
                return True

        raise RuntimeError("Failed to start agent (timeout)")

    @staticmethod
    def stop_agent(port: int = None, timeout: float = 1.5) -> bool:
        if not AgentClient.is_agent_running(port=port):
            return False

        try:
            client = AgentClient(port=port)
            client.send_command(Cmd.SHUTDOWN, timeout=0.5)
        except Exception:
            pass

        # Wait for agent to stop, but with faster polling
        start_time = time.time()
        while time.time() - start_time < timeout:
            time.sleep(0.05)  # Shorter sleep for faster response
            if not AgentClient.is_agent_running(port=port):
                return True

        return not AgentClient.is_agent_running(port=port)
