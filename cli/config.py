import os
import socket
import sys
import threading
from dataclasses import dataclass
from typing import Optional, Dict, Any

from replx.utils.constants import AGENT_HOST, DEFAULT_AGENT_PORT, MIN_AGENT_PORT, MAX_AGENT_PORT

_write_port_lock = threading.Lock()


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
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._port = None
        return cls._instance
    
    @property
    def port(self) -> Optional[str]:
        return self._port
    
    @port.setter
    def port(self, value: Optional[str]):
        self._port = value
    
    def set(self, port: str = None):
        self._port = port
    
    def get(self) -> Dict[str, Any]:
        return {
            'port': self._port,
        }
    
    def clear(self):
        self._port = None


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
            'default': None,
            'theme': None,
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
                        elif key == 'THEME':
                            result['theme'] = value
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
    def write(env_path: str, connections: dict, default: Optional[str] = None, theme: Optional[str] = None):
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
            if default:
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
                          theme: str = None,
                          theme_mode: str = None,
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
        
        if set_default:
            env_data['default'] = conn_key

        if theme is not None:
            AgentPortManager._write_registered_theme(theme)
            AgentPortManager._write_registered_theme_mode(theme_mode)
        
        ConfigManager.write(env_path, env_data['connections'], env_data['default'])
    
    @staticmethod
    def get_default(env_path: str) -> Optional[str]:
        env_data = ConfigManager.read(env_path)
        return env_data.get('default')

    @staticmethod
    def get_theme(env_path: str | None = None) -> str:
        theme_mode = AgentPortManager._read_registered_theme_mode()
        if theme_mode:
            return theme_mode

        theme = AgentPortManager._read_registered_theme(legacy_env_path=env_path)
        return theme or 'dark'

    @staticmethod
    def set_theme(env_path: str | None, theme: str, theme_mode: str | None = None):
        AgentPortManager._write_registered_theme(theme)
        AgentPortManager._write_registered_theme_mode(theme_mode)


class AgentPortManager:

    _AGENT_PORT_KEY = 'AGENT_PORT'
    _THEME_KEY = 'THEME'
    _THEME_MODE_KEY = 'THEME_MODE'
    _VSCODE_ROOT_KEY = 'VSCODE_ROOT'
    _PANEL_BOX_KEY = 'PANEL_BOX'
    _PANEL_COLORS_KEY = 'PANEL_COLORS'

    @staticmethod
    def _kill_agent_process_by_port(port: int) -> bool:
        try:
            import psutil
        except Exception:
            return False

        try:
            for conn in psutil.net_connections(kind='udp'):
                if conn.laddr and conn.laddr.port == port and conn.pid:
                    try:
                        psutil.Process(conn.pid).kill()
                        return True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        except Exception:
            return False

        return False

    @staticmethod
    def _pick_primary_port(running_ports: list[int], preferred_port: Optional[int] = None) -> Optional[int]:
        if not running_ports:
            return None

        registered_port = AgentPortManager._read_registered_port()
        for candidate in (preferred_port, registered_port, DEFAULT_AGENT_PORT):
            if isinstance(candidate, int) and candidate in running_ports:
                return candidate

        return min(running_ports)

    _singleton_cache: Optional[int] = None
    _singleton_cache_miss: bool = False

    @staticmethod
    def _ensure_singleton_running_agent(preferred_port: Optional[int] = None) -> Optional[int]:
        from .agent.client import AgentClient

        # Process-level cache: ``_ensure_connected`` calls this twice in the same
        # CLI invocation. The set of running agents cannot change between those
        # back-to-back calls, so reuse the prior result.
        if AgentPortManager._singleton_cache is not None:
            return AgentPortManager._singleton_cache
        if AgentPortManager._singleton_cache_miss:
            return None

        # Fast path: if the preferred / registered / default port already responds
        # to a ping, skip the slow scan-and-cleanup phase. Stale agent processes
        # left from previous sessions can otherwise add ~0.3s per dead candidate
        # (UDP probe timeout) on every CLI invocation. Singleton enforcement and
        # zombie cleanup still happen on cold paths (setup / shutdown) where the
        # priority candidate is unreachable.
        registered_port = AgentPortManager._read_registered_port()
        for candidate in (preferred_port, registered_port, DEFAULT_AGENT_PORT):
            if not isinstance(candidate, int):
                continue
            if AgentClient.is_agent_running(port=candidate):
                AgentPortManager._singleton_cache = candidate
                if registered_port != candidate:
                    AgentPortManager._write_registered_port(candidate)
                return candidate

        running_ports = []
        for port in AgentPortManager._get_candidate_agent_ports(preferred_port=preferred_port):
            if AgentClient.is_agent_running(port=port):
                running_ports.append(port)

        if not running_ports:
            AgentPortManager._singleton_cache_miss = True
            return None

        primary_port = AgentPortManager._pick_primary_port(running_ports, preferred_port=preferred_port)
        if primary_port is None:
            AgentPortManager._singleton_cache_miss = True
            return None

        for port in running_ports:
            if port == primary_port:
                continue
            try:
                AgentClient.stop_agent(port=port)
            except Exception:
                pass
            if AgentClient.is_agent_running(port=port):
                AgentPortManager._kill_agent_process_by_port(port)

        remaining_ports = []
        for port in running_ports:
            if AgentClient.is_agent_running(port=port):
                remaining_ports.append(port)

        extra_ports = [port for port in remaining_ports if port != primary_port]
        if extra_ports:
            print(
                f'[replx] Warning: extra agent(s) on port(s) '
                f'{", ".join(str(p) for p in sorted(extra_ports))} could not be stopped. '
                f'Using port {primary_port}.',
                file=sys.stderr,
            )

        AgentPortManager._write_registered_port(primary_port)
        AgentPortManager._singleton_cache = primary_port
        return primary_port

    @staticmethod
    def _agent_config_file() -> str:
        return os.path.join(os.path.expanduser('~'), '.replx', '.config')

    @staticmethod
    def _legacy_agent_port_file() -> str:
        return os.path.join(os.path.expanduser('~'), '.replx', '.agent_port')

    @staticmethod
    def _read_agent_config() -> dict[str, str]:
        path = AgentPortManager._agent_config_file()
        result: dict[str, str] = {}
        if not os.path.exists(path):
            return result

        try:
            with open(path, 'r', encoding='utf-8') as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    if key:
                        result[key] = value
        except OSError:
            return {}

        return result

    @staticmethod
    def _write_agent_config(entries: dict[str, str]) -> None:
        path = AgentPortManager._agent_config_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f'{path}.tmp'
        with _write_port_lock:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                for key in sorted(entries.keys()):
                    f.write(f'{key}={entries[key]}\n')
            os.replace(tmp_path, path)

    @staticmethod
    def _read_registered_port() -> Optional[int]:
        config_entries = AgentPortManager._read_agent_config()
        value = config_entries.get(AgentPortManager._AGENT_PORT_KEY)

        # Backward compatibility: read legacy ~/.replx/.agent_port once and migrate.
        if value is None:
            legacy_path = AgentPortManager._legacy_agent_port_file()
            if os.path.exists(legacy_path):
                try:
                    with open(legacy_path, 'r', encoding='utf-8') as f:
                        value = f.read().strip()
                except OSError:
                    value = None

                if value:
                    config_entries[AgentPortManager._AGENT_PORT_KEY] = value
                    AgentPortManager._write_agent_config(config_entries)

        try:
            port = int(value) if value is not None else None
        except (ValueError, TypeError):
            return None

        if port is None:
            return None

        if MIN_AGENT_PORT <= port <= MAX_AGENT_PORT:
            return port
        return None

    @staticmethod
    def _write_registered_port(port: int) -> None:
        if not isinstance(port, int) or not (MIN_AGENT_PORT <= port <= MAX_AGENT_PORT):
            return

        entries = AgentPortManager._read_agent_config()
        entries[AgentPortManager._AGENT_PORT_KEY] = str(port)
        AgentPortManager._write_agent_config(entries)

    @staticmethod
    def _read_registered_theme(legacy_env_path: Optional[str] = None) -> Optional[str]:
        config_entries = AgentPortManager._read_agent_config()
        theme = config_entries.get(AgentPortManager._THEME_KEY)
        if theme:
            return theme

        if legacy_env_path:
            legacy_theme = ConfigManager.read(legacy_env_path).get('theme')
            if legacy_theme:
                config_entries[AgentPortManager._THEME_KEY] = legacy_theme
                AgentPortManager._write_agent_config(config_entries)
                return legacy_theme

        return None

    @staticmethod
    def _read_registered_theme_mode() -> Optional[str]:
        config_entries = AgentPortManager._read_agent_config()
        theme_mode = config_entries.get(AgentPortManager._THEME_MODE_KEY)
        if theme_mode:
            return theme_mode.strip() or None
        return None

    @staticmethod
    def _write_registered_theme(theme: Optional[str]) -> None:
        if theme is None:
            return

        normalized = str(theme).strip()
        if not normalized:
            return

        entries = AgentPortManager._read_agent_config()
        entries[AgentPortManager._THEME_KEY] = normalized
        AgentPortManager._write_agent_config(entries)

    @staticmethod
    def _write_registered_theme_mode(theme_mode: Optional[str]) -> None:
        entries = AgentPortManager._read_agent_config()

        if theme_mode is None:
            entries.pop(AgentPortManager._THEME_MODE_KEY, None)
            AgentPortManager._write_agent_config(entries)
            return

        normalized = str(theme_mode).strip()
        if not normalized:
            entries.pop(AgentPortManager._THEME_MODE_KEY, None)
            AgentPortManager._write_agent_config(entries)
            return

        entries[AgentPortManager._THEME_MODE_KEY] = normalized
        AgentPortManager._write_agent_config(entries)

    @staticmethod
    def _read_cached_vscode_root() -> Optional[str]:
        entries = AgentPortManager._read_agent_config()
        root = entries.get(AgentPortManager._VSCODE_ROOT_KEY)
        if not root:
            return None

        settings_path = os.path.join(root, 'data', 'user-data', 'User', 'settings.json')
        if os.path.exists(settings_path):
            return root

        return None

    @staticmethod
    def _write_cached_vscode_root(root: Optional[str]) -> None:
        if root is None:
            return

        normalized = str(root).strip()
        if not normalized:
            return

        settings_path = os.path.join(normalized, 'data', 'user-data', 'User', 'settings.json')
        if not os.path.exists(settings_path):
            return

        entries = AgentPortManager._read_agent_config()
        entries[AgentPortManager._VSCODE_ROOT_KEY] = normalized
        AgentPortManager._write_agent_config(entries)

    @staticmethod
    def read_panel_box() -> str:
        """Return 'rounded' or 'horizontals'."""
        entries = AgentPortManager._read_agent_config()
        val = entries.get(AgentPortManager._PANEL_BOX_KEY, '').strip().lower()
        return val if val in ('rounded', 'horizontals') else 'rounded'

    @staticmethod
    def write_panel_box(style: str) -> None:
        style = style.strip().lower()
        if style not in ('rounded', 'horizontals'):
            raise ValueError(f"Invalid panel box style: {style!r}. Use 'rounded' or 'horizontals'.")
        entries = AgentPortManager._read_agent_config()
        entries[AgentPortManager._PANEL_BOX_KEY] = style
        AgentPortManager._write_agent_config(entries)

    @staticmethod
    def read_panel_colors() -> dict[str, str]:
        """Return category color overrides: {'help': '#ff8800', ...}"""
        entries = AgentPortManager._read_agent_config()
        raw = entries.get(AgentPortManager._PANEL_COLORS_KEY, '').strip()
        if not raw:
            return {}
        try:
            result = json.loads(raw)
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def write_panel_colors(colors: dict[str, str]) -> None:
        entries = AgentPortManager._read_agent_config()
        if colors:
            entries[AgentPortManager._PANEL_COLORS_KEY] = json.dumps(colors, separators=(',', ':'))
        else:
            entries.pop(AgentPortManager._PANEL_COLORS_KEY, None)
        AgentPortManager._write_agent_config(entries)

    @staticmethod
    def _can_bind_port(port: Optional[int]) -> bool:
        if not isinstance(port, int) or not (MIN_AGENT_PORT <= port <= MAX_AGENT_PORT):
            return False

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind((AGENT_HOST, port))
        except OSError:
            return False
        return True

    @staticmethod
    def _normalize_connection_port(port: Optional[str]) -> str:
        if not port:
            return ""
        p = str(port).strip()
        if sys.platform.startswith("win"):
            return p.upper()
        return p

    @staticmethod
    def _discover_running_agent_ports() -> list[int]:
        try:
            import psutil
        except Exception:
            return []

        ports = set()
        for proc in psutil.process_iter(['cmdline']):
            try:
                cmdline = proc.info.get('cmdline') or []
            except Exception:
                continue

            if not cmdline:
                continue

            cmd_lower = [str(part).lower() for part in cmdline]
            module_index = None
            for i in range(len(cmd_lower) - 1):
                if cmd_lower[i] == '-m' and cmd_lower[i + 1] == 'replx.cli.agent.server':
                    module_index = i + 1
                    break

            if module_index is None:
                continue

            port = DEFAULT_AGENT_PORT
            if module_index + 1 < len(cmdline):
                maybe_port = str(cmdline[module_index + 1]).strip()
                if maybe_port.isdigit():
                    port = int(maybe_port)

            ports.add(port)

        return sorted(ports)

    @staticmethod
    def _get_candidate_agent_ports(env_path: str = None, preferred_port: int = None) -> list[int]:
        ports: list[int] = []

        def _add(port: Optional[int]) -> None:
            if isinstance(port, int) and port > 0 and port not in ports:
                ports.append(port)

        _add(preferred_port)
        _add(AgentPortManager._read_registered_port())
        _add(DEFAULT_AGENT_PORT)

        for port in AgentPortManager._discover_running_agent_ports():
            _add(port)

        return ports
    
    @staticmethod
    def find_available_port(env_path: str = None) -> int:
        registered_port = AgentPortManager._read_registered_port()
        running_port = AgentPortManager._ensure_singleton_running_agent(preferred_port=registered_port)
        if running_port is not None:
            return running_port

        if registered_port is not None and AgentPortManager._can_bind_port(registered_port):
            return registered_port

        for port in range(MIN_AGENT_PORT, MAX_AGENT_PORT + 1):
            if not AgentPortManager._can_bind_port(port):
                continue
            AgentPortManager._write_registered_port(port)
            return port

        fallback_port = registered_port or DEFAULT_AGENT_PORT
        AgentPortManager._write_registered_port(fallback_port)
        return fallback_port
    
    @staticmethod
    def find_running_agent(env_path: str = None) -> Optional[int]:
        return AgentPortManager._ensure_singleton_running_agent()

    @staticmethod
    def find_running_agents(env_path: str = None) -> list[int]:
        running_port = AgentPortManager._ensure_singleton_running_agent()
        if running_port is None:
            return []
        return [running_port]

    @staticmethod
    def find_agent_for_connection(port: str, env_path: str = None, preferred_port: int = None) -> Optional[int]:
        from .agent.client import AgentClient

        target_port = AgentPortManager._normalize_connection_port(port)
        if not target_port:
            return None

        agent_port = AgentPortManager._ensure_singleton_running_agent(preferred_port=preferred_port)
        if agent_port is None:
            return None

        try:
            with AgentClient(port=agent_port) as client:
                session_info = client.send_command('session_info', timeout=1.0)
        except Exception:
            return None

        for conn in session_info.get('connections', []):
            conn_port = AgentPortManager._normalize_connection_port(conn.get('port'))
            if conn_port == target_port and conn.get('connected'):
                return agent_port

        return None


class ConnectionResolver:
    
    @staticmethod
    def resolve(global_port: str = None) -> Optional[dict]:
        env_path = ConfigManager.find_env_file()
        
        if global_port:
            return ConnectionResolver._resolve_serial(global_port, env_path)

        result = ConnectionResolver._resolve_from_session(env_path)
        if result:
            return result
        
        return ConnectionResolver._resolve_from_default(env_path)
    
    @staticmethod
    def _resolve_serial(port: str, env_path: str) -> dict:
        conn_key = port
        result = {
            'connection': port,
            'source': 'global',
            'agent_port': AgentPortManager.find_available_port(env_path),
        }
        
        if env_path:
            config = ConfigManager.get_connection(env_path, conn_key)
            if config:
                result['core'] = config.get('core')
                result['device'] = config.get('device')
        
        return result
    
    @staticmethod
    def _resolve_from_session(env_path: str = None) -> Optional[dict]:
        from .agent.client import AgentClient, get_cached_session_id

        ppid = get_cached_session_id()
        agent_port = AgentPortManager.find_running_agent(env_path)
        if agent_port is None:
            return None

        try:
            with AgentClient(port=agent_port) as client:
                session_info = client.send_command('session_info', timeout=1.0)

            for session in session_info.get('sessions', []):
                if session.get('ppid') == ppid and session.get('foreground'):
                    fg_port = session['foreground']

                    for conn in session_info.get('connections', []):
                        if conn.get('port') == fg_port and conn.get('connected'):
                            return {
                                'connection': fg_port,
                                'agent_port': agent_port,
                                'core': conn.get('core'),
                                'device': conn.get('device'),
                                'source': 'session'
                            }
        except Exception:
            return None
        
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
            'agent_port': AgentPortManager.find_available_port(env_path),
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

def _get_theme_config(env_path: str | None = None) -> str:
    return ConfigManager.get_theme(env_path)

def _set_theme_config(env_path: str | None, theme: str, theme_mode: str | None = None):
    return ConfigManager.set_theme(env_path, theme, theme_mode)

def _find_available_agent_port(env_path: str) -> int:
    return AgentPortManager.find_available_port(env_path)

def _resolve_agent_port() -> int:
    return AgentPortManager.find_available_port()

def _get_registered_agent_port() -> Optional[int]:
    return AgentPortManager._read_registered_port()

def _find_running_agent_port(env_path: str) -> Optional[int]:
    return AgentPortManager.find_running_agent(env_path)

def _find_running_agent_ports(env_path: str = None) -> list[int]:
    return AgentPortManager.find_running_agents(env_path)

def _find_agent_for_connection(port: str, env_path: str = None, preferred_port: int = None) -> Optional[int]:
    return AgentPortManager.find_agent_for_connection(port, env_path, preferred_port)

def _resolve_connection(global_port: str = None) -> Optional[dict]:
    return ConnectionResolver.resolve(global_port)

def _set_global_options(port: str = None):
    GLOBAL_OPTIONS.set(port)

def _get_global_options() -> dict:
    return GLOBAL_OPTIONS.get()
