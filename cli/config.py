import os
import sys
from dataclasses import dataclass
from typing import Optional, Dict, Any

from replx.utils.constants import DEFAULT_AGENT_PORT, MAX_AGENT_PORT


@dataclass
class RuntimeState:
    version: str = "?"
    core: str = ""
    device: str = ""
    manufacturer: str = ""
    device_root_fs: str = "/"
    core_path: str = ""
    device_path: str = ""


STATE = RuntimeState()

class GlobalOptions:
    """Global CLI options storage."""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._port = None
            cls._instance._agent_port = None
        return cls._instance
    
    @property
    def port(self) -> Optional[str]:
        return self._port
    
    @port.setter
    def port(self, value: Optional[str]):
        self._port = value
    
    @property
    def agent_port(self) -> Optional[int]:
        return self._agent_port
    
    @agent_port.setter
    def agent_port(self, value: Optional[int]):
        self._agent_port = value
    
    def set(self, port: str = None, agent_port: int = None):
        self._port = port
        self._agent_port = agent_port
    
    def get(self) -> Dict[str, Any]:
        return {
            'port': self._port,
            'agent_port': self._agent_port
        }
    
    def clear(self):
        self._port = None
        self._agent_port = None


GLOBAL_OPTIONS = GlobalOptions()

class ConfigManager:
   
    @staticmethod
    def find_env_file() -> Optional[str]:
        current = os.path.realpath(os.getcwd())
        root = os.path.abspath(os.sep)
        
        visited = set()
        while current not in visited:
            visited.add(current)
            env_path = os.path.join(current, ".vscode", ".replx")
            if os.path.exists(env_path):
                return env_path
            parent = os.path.dirname(current)
            if parent == current or parent == root:
                break
            current = parent
        return None
    
    @staticmethod
    def find_or_create_vscode_dir() -> str:
        current = os.path.realpath(os.getcwd())
        root = os.path.abspath(os.sep)
        
        search_dir = current
        visited = set()
        while search_dir not in visited:
            visited.add(search_dir)
            vscode_dir = os.path.join(search_dir, ".vscode")
            if os.path.isdir(vscode_dir):
                return vscode_dir
            parent = os.path.dirname(search_dir)
            if parent == search_dir or parent == root:
                break
            search_dir = parent
        
        vscode_dir = os.path.join(current, ".vscode")
        os.makedirs(vscode_dir, exist_ok=True)
        return vscode_dir
    
    @staticmethod
    def read(env_path: str) -> dict:
        result = {
            'connections': {},
            'default': None
        }
        
        if not os.path.exists(env_path):
            return result
        
        try:
            with open(env_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            current_section = None
            
            for line in content.splitlines():
                line = line.strip()
                
                if not line or line.startswith('#'):
                    continue
                
                if line.startswith('[') and line.endswith(']'):
                    current_section = line[1:-1].strip()
                    if current_section.upper() != 'DEFAULT':
                        result['connections'][current_section] = {}
                    continue
                
                if '=' in line and current_section:
                    key, value = line.split('=', 1)
                    key = key.strip().upper()
                    value = value.strip()
                    
                    if current_section.upper() == 'DEFAULT':
                        if key == 'CONNECTION':
                            result['default'] = value
                    else:
                        conn = result['connections'][current_section]
                        if key == 'VERSION':
                            conn['version'] = value
                        elif key == 'CORE':
                            conn['core'] = value
                        elif key == 'DEVICE':
                            conn['device'] = value
                        elif key == 'MANUFACTURER':
                            conn['manufacturer'] = value
                        elif key == 'SERIAL_PORT':
                            conn['serial_port'] = value
            
            return result
        except Exception:
            return result
    
    @staticmethod
    def write(env_path: str, connections: dict, default: Optional[str] = None):
        lines = []
        
        for conn_key, conn_data in connections.items():
            lines.append(f'[{conn_key}]')
            if conn_data.get('version'):
                lines.append(f"VERSION={conn_data['version']}")
            if conn_data.get('core'):
                lines.append(f"CORE={conn_data['core']}")
            if conn_data.get('device'):
                lines.append(f"DEVICE={conn_data['device']}")
            if conn_data.get('manufacturer'):
                lines.append(f"MANUFACTURER={conn_data['manufacturer']}")
            if conn_data.get('serial_port'):
                lines.append(f"SERIAL_PORT={conn_data['serial_port']}")
            lines.append('')
        
        if default:
            lines.append('[DEFAULT]')
            lines.append(f'CONNECTION={default}')
            lines.append('')
        
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    
    @staticmethod
    def get_connection(env_path: str, connection: str) -> Optional[dict]:
        env_data = ConfigManager.read(env_path)
        if not connection:
            return None

        if connection in env_data.get('connections', {}):
            return env_data['connections'][connection]

        if sys.platform.startswith("win"):
            needle = str(connection).lower()
            for key, value in env_data.get('connections', {}).items():
                if isinstance(key, str) and key.lower() == needle:
                    return value

        return None
    
    @staticmethod
    def update_connection(env_path: str, connection: str, version: str = None,
                          core: str = None, device: str = None,
                          manufacturer: str = None,
                          serial_port: str = None,
                          agent_port: int = None,
                          set_default: bool = False):

        def _resolve_os_serial_port_name(port: str) -> str:
            if not port:
                return port
            p = str(port).strip()
            if not sys.platform.startswith("win"):
                return p
            try:
                from serial.tools.list_ports import comports as list_ports_comports

                needle = p.lower()
                for info in list_ports_comports():
                    dev = getattr(info, "device", None)
                    if isinstance(dev, str) and dev.lower() == needle:
                        return dev
            except Exception:
                pass
            return p

        desired_key = _resolve_os_serial_port_name(connection)

        env_data = ConfigManager.read(env_path)
        
        existing_key = None
        for key in env_data.get('connections', {}):
            if key == desired_key:
                existing_key = key
                break

        if existing_key is None and sys.platform.startswith("win") and desired_key:
            needle = str(desired_key).lower()
            for key in env_data.get('connections', {}):
                if isinstance(key, str) and key.lower() == needle:
                    existing_key = key
                    break

        conn_key = None
        if existing_key:
            if sys.platform.startswith("win") and desired_key and existing_key != desired_key:
                conns = env_data.get('connections', {})
                if desired_key in conns and desired_key != existing_key:
                    merged = dict(conns[desired_key] or {})
                    for k, v in (conns[existing_key] or {}).items():
                        if k not in merged:
                            merged[k] = v
                    conns[desired_key] = merged
                    try:
                        del conns[existing_key]
                    except KeyError:
                        pass
                else:
                    conns[desired_key] = conns.pop(existing_key)

                if env_data.get('default') == existing_key:
                    env_data['default'] = desired_key

                conn_key = desired_key
            else:
                conn_key = existing_key
        else:
            conn_key = desired_key
            env_data.setdefault('connections', {})[conn_key] = {}
        
        conn = env_data['connections'][conn_key]
        
        if version is not None:
            conn['version'] = version
        if core is not None:
            conn['core'] = core
        if device is not None:
            conn['device'] = device
        if manufacturer is not None:
            conn['manufacturer'] = manufacturer
        if serial_port is not None:
            conn['serial_port'] = serial_port
        if agent_port is not None:
            conn['agent_port'] = agent_port
        
        if set_default:
            env_data['default'] = conn_key
        
        ConfigManager.write(env_path, env_data['connections'], env_data['default'])
    
    @staticmethod
    def get_default(env_path: str) -> Optional[str]:
        env_data = ConfigManager.read(env_path)
        return env_data.get('default')


class AgentPortManager:
    
    @staticmethod
    def find_available_port(env_path: str = None) -> int:
        from .agent.client import AgentClient
        
        env_data = ConfigManager.read(env_path) if env_path and os.path.exists(env_path) else {'connections': {}}
        
        registered_ports = {}
        for conn_key, conn_data in env_data['connections'].items():
            port = conn_data.get('agent_port')
            if port:
                registered_ports[port] = conn_key
        
        for port in range(DEFAULT_AGENT_PORT, MAX_AGENT_PORT):
            if port in registered_ports:
                if AgentClient.is_agent_running(port=port):
                    continue
            return port
        
        return DEFAULT_AGENT_PORT
    
    @staticmethod
    def find_running_agent(env_path: str = None) -> Optional[int]:
        from .agent.client import AgentClient
        
        if not env_path or not os.path.exists(env_path):
            return None
        
        env_data = ConfigManager.read(env_path)
        
        agent_ports = set()
        for conn_key, conn_data in env_data.get('connections', {}).items():
            port = conn_data.get('agent_port')
            if port:
                agent_ports.add(port)
        
        for port in sorted(agent_ports):
            if AgentClient.is_agent_running(port=port):
                return port
        
        return None


class ConnectionResolver:
    
    @staticmethod
    def resolve(global_port: str = None) -> Optional[dict]:
        from .agent.client import AgentClient
        
        env_path = ConfigManager.find_env_file()
        
        if global_port:
            return ConnectionResolver._resolve_serial(global_port, env_path)
        
        if AgentClient.is_agent_running():
            result = ConnectionResolver._resolve_from_session()
            if result:
                return result
        
        return ConnectionResolver._resolve_from_default(env_path)
    
    @staticmethod
    def _resolve_serial(port: str, env_path: str) -> dict:
        conn_key = port
        result = {
            'connection': port,
            'source': 'global'
        }
        
        if env_path:
            config = ConfigManager.get_connection(env_path, conn_key)
            if config:
                result['agent_port'] = config.get('agent_port')
                result['core'] = config.get('core')
                result['device'] = config.get('device')
        
        if not result.get('agent_port'):
            result['agent_port'] = AgentPortManager.find_available_port(env_path)
        
        return result
    
    @staticmethod
    def _resolve_from_session() -> Optional[dict]:
        from .agent.client import AgentClient, get_cached_session_id
        
        try:
            with AgentClient() as client:
                session_info = client.send_command('session_info', timeout=1.0)
                
                ppid = get_cached_session_id()
                for session in session_info.get('sessions', []):
                    if session.get('ppid') == ppid and session.get('foreground'):
                        fg_port = session['foreground']
                        
                        for conn in session_info.get('connections', []):
                            if conn.get('port') == fg_port and conn.get('connected'):
                                return {
                                    'connection': fg_port,
                                    'agent_port': DEFAULT_AGENT_PORT,
                                    'core': conn.get('core'),
                                    'device': conn.get('device'),
                                    'source': 'session'
                                }
        except Exception:
            pass
        
        return None
    
    @staticmethod
    def _resolve_from_default(env_path: str) -> Optional[dict]:
        if not env_path:
            return None
        
        default_conn = ConfigManager.get_default(env_path)
        if not default_conn:
            return None
        
        config = ConfigManager.get_connection(env_path, default_conn)
        if not config:
            return None
        
        return {
            'connection': default_conn,
            'agent_port': config.get('agent_port') or AgentPortManager.find_available_port(env_path),
            'core': config.get('core'),
            'device': config.get('device'),
            'source': 'default'
        }

def _find_env_file() -> Optional[str]:
    return ConfigManager.find_env_file()

def _find_or_create_vscode_dir() -> str:
    return ConfigManager.find_or_create_vscode_dir()

def _read_env_ini(env_path: str) -> dict:
    return ConfigManager.read(env_path)

def _get_connection_config(env_path: str, connection: str) -> Optional[dict]:
    return ConfigManager.get_connection(env_path, connection)

def _update_connection_config(env_path: str, connection: str, **kwargs):
    return ConfigManager.update_connection(env_path, connection, **kwargs)

def _get_default_connection(env_path: str) -> Optional[str]:
    return ConfigManager.get_default(env_path)

def _find_available_agent_port(env_path: str) -> int:
    return AgentPortManager.find_available_port(env_path)

def _find_running_agent_port(env_path: str) -> Optional[int]:
    return AgentPortManager.find_running_agent(env_path)

def _resolve_connection(global_port: str = None) -> Optional[dict]:
    return ConnectionResolver.resolve(global_port)

def _set_global_options(port: str = None, agent_port: int = None):
    GLOBAL_OPTIONS.set(port, agent_port)

def _get_global_options() -> dict:
    return GLOBAL_OPTIONS.get()
