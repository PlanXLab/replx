import locale
import os
import sys
import socket
import time
import atexit
import tempfile
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
        self._atexit_registered = False

    def connect(self):
        if not self.sock:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(self.TIMEOUT)
        # M7: ensure the agent releases the device port if the CLI process
        # exits abnormally (Ctrl-C, unhandled exception, parent terminated)
        # without its normal teardown path running.
        if not self._atexit_registered and self.device_port:
            try:
                atexit.register(self._atexit_release)
                self._atexit_registered = True
            except Exception:
                pass

    def _atexit_release(self) -> None:
        try:
            if self.sock is None:
                return
            self._release_device_port_safe()
        except Exception:
            pass

    def disconnect(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _release_device_port_safe(self) -> None:
        """Best-effort release of the bound device port on the agent.

        Used when the client detects the device is unresponsive (e.g. cable
        unplugged, board hung) so that subsequent commands won't operate on
        a stale serial connection that the agent still believes is active.
        """
        if not self.device_port:
            return
        try:
            request = AgentProtocol.create_request(
                'disconnect_port',
                ppid=self._ppid,
                port=self.device_port,
            )
            data = AgentProtocol.encode_message(request)
            try:
                self.sock.sendto(data, (AGENT_HOST, self.agent_port))
            except Exception:
                return
            # Drain any matching response briefly; ignore errors.
            deadline = time.time() + 1.0
            seq = request.get('seq')
            try:
                self.sock.settimeout(0.2)
            except Exception:
                pass
            while time.time() < deadline:
                try:
                    pkt, _ = self.sock.recvfrom(MAX_UDP_SIZE)
                    msg = AgentProtocol.decode_message(pkt)
                    if msg and msg.get('seq') == seq and msg.get('type') == 'response':
                        break
                except Exception:
                    break
        except Exception:
            pass

    def send_command(self, command: str, timeout: float = None, max_retries: int = None, **args) -> Dict[str, Any]:
        if not self.sock:
            self.connect()

        effective_timeout = timeout if timeout else self.TIMEOUT
        self.sock.settimeout(effective_timeout)
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

        if max_retries is not None:
            max_attempts = max(1, max_retries)
        else:
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
                        stop_check: Callable[[], bool] = None,
                        ctrl_c_grace_s: float = 3.0) -> Dict[str, Any]:
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

        # UDP can drop ACK packets. Retry start request a few times while
        # waiting for ACK/early stream for this seq.
        self.sock.settimeout(0.2)
        ack_received = False
        error_response = None
        completed_during_handshake = False

        max_attempts = max(1, self.MAX_RETRIES)
        for attempt in range(max_attempts):
            self.sock.sendto(request_data, (AGENT_HOST, self.agent_port))

            attempt_deadline = time.time() + 1.5
            while time.time() < attempt_deadline:
                try:
                    data, addr = self.sock.recvfrom(MAX_UDP_SIZE)
                    msg = AgentProtocol.decode_message(data)
                    if msg and msg.get('seq') == seq:
                        msg_type = msg.get('type')
                        if msg_type == 'ack':
                            ack_received = True
                            break
                        if msg_type == 'response' and msg.get('error'):
                            error_response = msg
                            break
                        # If stream arrives before ACK (ACK loss/reordering),
                        # treat it as a successful start.
                        if msg_type == 'stream':
                            ack_received = True
                            output = msg.get('output', '')
                            if output and output_callback:
                                output_callback(output.encode('utf-8'), 'stdout')
                            if msg.get('completed'):
                                error = msg.get('error')
                                if error and output_callback:
                                    output_callback(error.encode('utf-8'), 'stderr')
                                completed_during_handshake = True
                                # M1: ACK so server stops retransmitting.
                                try:
                                    self.sock.sendto(
                                        AgentProtocol.encode_message(
                                            AgentProtocol.create_stream_ack(seq)
                                        ),
                                        (AGENT_HOST, self.agent_port),
                                    )
                                except Exception:
                                    pass
                            break
                except socket.timeout:
                    continue

            if ack_received or error_response:
                break

        if error_response:
            raise RuntimeError(error_response['error'])

        if not ack_received:
            raise RuntimeError(f"No ACK from agent - run_interactive failed to start (attempts={max_attempts})")

        if completed_during_handshake:
            self.sock.settimeout(self.TIMEOUT)
            return {"run": True, "completed": True}

        self.sock.settimeout(0.01)
        input_interval = 0.001
        last_input_time = 0
        last_stream_time = time.time()
        stream_timeout = 5.0  # 5 seconds without stream = connection lost

        try:
            while True:
                if stop_check and stop_check():
                    if input_provider:
                        for _ in range(16):
                            try:
                                input_data = input_provider()
                                if input_data:
                                    input_msg = AgentProtocol.create_input(seq, input_data, ppid=self._ppid, port=self.device_port)
                                    self.sock.sendto(AgentProtocol.encode_message(input_msg), (AGENT_HOST, self.agent_port))
                                else:
                                    break
                            except Exception:
                                break

                    grace_deadline = time.time() + ctrl_c_grace_s
                    graceful = False
                    while time.time() < grace_deadline:
                        try:
                            data, addr = self.sock.recvfrom(MAX_UDP_SIZE)
                            msg = AgentProtocol.decode_message(data)
                            if msg and msg.get('seq') == seq:
                                if msg.get('type') == 'stream':
                                    last_stream_time = time.time()
                                    output = msg.get('output', '')
                                    if output and output_callback:
                                        output_callback(output.encode('utf-8'), 'stdout')
                                    if msg.get('completed'):
                                        error = msg.get('error')
                                        if error and output_callback:
                                            output_callback(error.encode('utf-8'), 'stderr')
                                        graceful = True
                                        # M1: ACK so server stops retransmitting.
                                        try:
                                            self.sock.sendto(
                                                AgentProtocol.encode_message(
                                                    AgentProtocol.create_stream_ack(seq)
                                                ),
                                                (AGENT_HOST, self.agent_port),
                                            )
                                        except Exception:
                                            pass
                                        break
                        except socket.timeout:
                            pass
                        except Exception:
                            pass

                    if not graceful:
                        try:
                            self.send_command(Cmd.RUN_STOP, timeout=2.0)
                        except Exception:
                            pass
                    break

                now = time.time()

                # Check for stream reception timeout (connection loss detection)
                if now - last_stream_time > stream_timeout:
                    # Release the device port on the agent so subsequent
                    # commands don't operate on a stale serial connection.
                    self._release_device_port_safe()
                    raise RuntimeError("Connection lost - no data from board for {}s".format(stream_timeout))

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

                _pending_error = None
                try:
                    data, addr = self.sock.recvfrom(MAX_UDP_SIZE)
                    msg = AgentProtocol.decode_message(data)

                    if msg and msg.get('seq') == seq:
                        if msg.get('type') == 'response' and msg.get('error'):
                            _pending_error = msg['error']
                        elif msg.get('type') == 'stream':
                            last_stream_time = time.time()  # Update on any stream (even empty)
                            output = msg.get('output', '')
                            if output and output_callback:
                                output_callback(output.encode('utf-8'), 'stdout')

                            if msg.get('completed'):
                                error = msg.get('error')
                                if error and output_callback:
                                    output_callback(error.encode('utf-8'), 'stderr')
                                # M1: ACK so server stops retransmitting.
                                try:
                                    self.sock.sendto(
                                        AgentProtocol.encode_message(
                                            AgentProtocol.create_stream_ack(seq)
                                        ),
                                        (AGENT_HOST, self.agent_port),
                                    )
                                except Exception:
                                    pass
                                break

                except socket.timeout:
                    pass
                except Exception:
                    pass

                if _pending_error:
                    raise RuntimeError(_pending_error)

        except KeyboardInterrupt:
            try:
                self.send_command(Cmd.RUN_STOP, timeout=2.0)
            except Exception:
                pass
            raise

        finally:
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
        agent_port = port or DEFAULT_AGENT_PORT

        cmd = [python_exe, '-m', agent_module]
        if port:
            cmd.append(str(port))

        proc = None
        stderr_file = None
        stderr_path = None

        if background:
            try:
                fd, stderr_path = tempfile.mkstemp(prefix='replx_agent_', suffix='.err')
                os.close(fd)
                stderr_file = open(stderr_path, 'wb')
            except Exception:
                stderr_file = None
                stderr_path = None

            if sys.platform == 'win32':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0

                proc = subprocess.Popen(
                    cmd,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file if stderr_file else subprocess.DEVNULL,
                    startupinfo=startupinfo
                )
            else:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file if stderr_file else subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True
                )

            if stderr_file:
                stderr_file.close()
                stderr_file = None
        else:
            proc = subprocess.Popen(cmd)

        try:
            for _ in range(100):
                time.sleep(0.1)
                if proc is not None and proc.poll() is not None:
                    detail = ''
                    if stderr_path:
                        try:
                            enc = locale.getpreferredencoding(False) or 'utf-8'
                            with open(stderr_path, 'rb') as f:
                                detail = f.read().decode(enc, errors='replace').strip()
                        except Exception:
                            pass
                    msg = f"Failed to start agent (process exited with code {proc.returncode})"
                    if detail:
                        msg += f"\n{detail}"
                    raise RuntimeError(msg)
                if AgentClient.is_agent_running(port=port):
                    return True
        finally:
            if stderr_path:
                try:
                    os.unlink(stderr_path)
                except Exception:
                    pass

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

        start_time = time.time()
        while time.time() - start_time < timeout:
            time.sleep(0.05) 
            if not AgentClient.is_agent_running(port=port):
                return True

        return not AgentClient.is_agent_running(port=port)
