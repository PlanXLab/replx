"""
Configuration management for replx CLI.

Handles:
- .replx INI file reading/writing
- Connection configuration management
- Agent port allocation
- Connection resolution (global option -> session -> default)
"""

import os
import sys
from dataclasses import dataclass
from typing import Optional, Dict, Any

from replx.utils.constants import DEFAULT_AGENT_PORT, MAX_AGENT_PORT


# ============================================================================
# Runtime State
# ============================================================================

@dataclass
class RuntimeState:
    """Global runtime state for the current session."""
    version: str = "?"
    core: str = ""
    device: str = ""
    manufacturer: str = ""
    device_root_fs: str = "/"
    core_path: str = ""
    device_path: str = ""


# Singleton instance
STATE = RuntimeState()


# ============================================================================
# Global Options (set by CLI callback)
# ============================================================================

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
        """Set global options."""
        self._port = port
        self._agent_port = agent_port
    
    def get(self) -> Dict[str, Any]:
        """Get all global options as dict."""
        return {
            'port': self._port,
            'agent_port': self._agent_port
        }
    
    def clear(self):
        """Clear all global options."""
        self._port = None
        self._agent_port = None


# Singleton instance
GLOBAL_OPTIONS = GlobalOptions()


# ============================================================================
# Environment File Management
# ============================================================================

class ConfigManager:
    """
    Manages .replx configuration file (INI format).
    
    File format:
        [COM3]
        VERSION=1.24.1
        CORE=RP2350
        DEVICE=ticle
        MANUFACTURER=Raspberry Pi
        
        [DEFAULT]
        CONNECTION=COM3
    """
    
    @staticmethod
    def find_env_file() -> Optional[str]:
        """Find .vscode/.replx file by searching up from current directory.
        
        Handles symlinks properly on all platforms.
        """
        # Use realpath to resolve symlinks
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
        """Find existing .vscode directory or create one in current directory.
        
        Handles symlinks properly on all platforms.
        """
        # Use realpath to resolve symlinks
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
        
        # Not found, create in current directory
        vscode_dir = os.path.join(current, ".vscode")
        os.makedirs(vscode_dir, exist_ok=True)
        return vscode_dir
    
    @staticmethod
    def read(env_path: str) -> dict:
        """
        Read INI-style .replx file.
        
        Returns:
            dict with structure:
            {
                'connections': {
                    'COM3': {'version': '...', 'core': '...', ...},
                    ...
                },
                'default': 'COM3'
            }
        """
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
                
                # Skip empty lines and comments
                if not line or line.startswith('#'):
                    continue
                
                # Section header
                if line.startswith('[') and line.endswith(']'):
                    current_section = line[1:-1].strip()
                    if current_section.upper() != 'DEFAULT':
                        result['connections'][current_section] = {}
                    continue
                
                # Key=Value pairs
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
        """
        Write INI-style .replx file.
        
        Args:
            env_path: Path to .replx file
            connections: Dict of connection configs
            default: Default connection key
        """
        lines = []
        
        # Write connection sections
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
        
        # Write DEFAULT section
        if default:
            lines.append('[DEFAULT]')
            lines.append(f'CONNECTION={default}')
            lines.append('')
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    
    @staticmethod
    def get_connection(env_path: str, connection: str) -> Optional[dict]:
        """Get configuration for a specific connection."""
        env_data = ConfigManager.read(env_path)
        # Platform-aware lookup for serial ports
        norm_connection = connection
        for key, value in env_data['connections'].items():
            if key == norm_connection:
                return value
        return None
    
    @staticmethod
    def update_connection(env_path: str, connection: str, version: str = None,
                          core: str = None, device: str = None,
                          manufacturer: str = None,
                          serial_port: str = None,
                          agent_port: int = None,
                          set_default: bool = False):
        """Update or add a connection configuration."""
        env_data = ConfigManager.read(env_path)
        
        # Find existing connection
        existing_key = None
        for key in env_data['connections']:
            if key == connection:
                existing_key = key
                break
        
        # Use existing key to preserve original case, or use original connection name for new entry
        if existing_key:
            conn_key = existing_key
        else:
            # For new connections, preserve original case on all platforms
            conn_key = connection
            env_data['connections'][conn_key] = {}
        
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
        
        # Set default using the actual key (preserves case on all platforms)
        if set_default:
            env_data['default'] = conn_key
        
        ConfigManager.write(env_path, env_data['connections'], env_data['default'])
    
    @staticmethod
    def get_default(env_path: str) -> Optional[str]:
        """Get the default connection from .replx file."""
        env_data = ConfigManager.read(env_path)
        return env_data.get('default')


# ============================================================================
# Agent Port Allocation
# ============================================================================

class AgentPortManager:
    """Manages agent port allocation."""
    
    @staticmethod
    def find_available_port(env_path: str = None) -> int:
        """
        Find an available agent port.
        
        Rules:
        1. Start from DEFAULT_AGENT_PORT (49152)
        2. Skip ports that are registered and have responding agents
        """
        from .agent.client import AgentClient
        
        env_data = ConfigManager.read(env_path) if env_path and os.path.exists(env_path) else {'connections': {}}
        
        # Collect registered ports
        registered_ports = {}
        for conn_key, conn_data in env_data['connections'].items():
            port = conn_data.get('agent_port')
            if port:
                registered_ports[port] = conn_key
        
        # Find first available port
        for port in range(DEFAULT_AGENT_PORT, MAX_AGENT_PORT):
            if port in registered_ports:
                # Check if agent is actually running
                if AgentClient.is_agent_running(port=port):
                    continue
            return port
        
        return DEFAULT_AGENT_PORT
    
    @staticmethod
    def find_running_agent(env_path: str = None) -> Optional[int]:
        """
        Find an already running agent from .replx connections.
        
        Returns:
            The agent port if a running agent is found, None otherwise.
        """
        from .agent.client import AgentClient
        
        if not env_path or not os.path.exists(env_path):
            return None
        
        env_data = ConfigManager.read(env_path)
        
        # Collect all registered agent ports
        agent_ports = set()
        for conn_key, conn_data in env_data.get('connections', {}).items():
            port = conn_data.get('agent_port')
            if port:
                agent_ports.add(port)
        
        # Check if any agent is running
        for port in sorted(agent_ports):
            if AgentClient.is_agent_running(port=port):
                return port
        
        return None


# ============================================================================
# Connection Resolution
# ============================================================================

class ConnectionResolver:
    """
    Resolves which connection to use based on priority:
    1. Global option (--port)
    2. Agent's session foreground (based on PPID)
    3. .replx DEFAULT
    """
    
    @staticmethod
    def resolve(global_port: str = None) -> Optional[dict]:
        """
        Resolve connection configuration.
        
        Returns:
            dict with:
            {
                'connection': 'COM3',
                'agent_port': 49152,
                'core': 'RP2350',
                'device': 'ticle',
                'source': 'global' | 'session' | 'default'
            }
            or None if no connection can be resolved
        """
        from .agent.client import AgentClient
        
        env_path = ConfigManager.find_env_file()
        
        # 1. Global option (--port)
        if global_port:
            return ConnectionResolver._resolve_serial(global_port, env_path)
        
        # 2. Query Agent for session's foreground connection
        if AgentClient.is_agent_running():
            result = ConnectionResolver._resolve_from_session()
            if result:
                return result
        
        # 3. DEFAULT from .replx
        return ConnectionResolver._resolve_from_default(env_path)
    
    @staticmethod
    def _resolve_serial(port: str, env_path: str) -> dict:
        """Resolve serial port connection."""
        conn_key = port
        result = {
            'connection': port,
            'source': 'global'
        }
        
        # Try to get existing config (use normalized for lookup)
        if env_path:
            config = ConfigManager.get_connection(env_path, conn_key)
            if config:
                result['agent_port'] = config.get('agent_port')
                result['core'] = config.get('core')
                result['device'] = config.get('device')
        
        # Assign agent port if not found
        if not result.get('agent_port'):
            result['agent_port'] = AgentPortManager.find_available_port(env_path)
        
        return result
    
    @staticmethod
    def _resolve_from_session() -> Optional[dict]:
        """Resolve from current agent session."""
        from .agent.client import AgentClient
        
        try:
            with AgentClient() as client:
                session_info = client.send_command('session_info', timeout=1.0)
                
                ppid = os.getppid()
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
        """Resolve from .replx DEFAULT."""
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


# ============================================================================
# Backward compatibility aliases (for gradual migration)
# ============================================================================

def _find_env_file() -> Optional[str]:
    """Alias for ConfigManager.find_env_file()."""
    return ConfigManager.find_env_file()

def _find_or_create_vscode_dir() -> str:
    """Alias for ConfigManager.find_or_create_vscode_dir()."""
    return ConfigManager.find_or_create_vscode_dir()

def _read_env_ini(env_path: str) -> dict:
    """Alias for ConfigManager.read()."""
    return ConfigManager.read(env_path)

def _write_env_ini(env_path: str, connections: dict, default: Optional[str] = None):
    """Alias for ConfigManager.write()."""
    return ConfigManager.write(env_path, connections, default)

def _get_connection_config(env_path: str, connection: str) -> Optional[dict]:
    """Alias for ConfigManager.get_connection()."""
    return ConfigManager.get_connection(env_path, connection)

def _update_connection_config(env_path: str, connection: str, **kwargs):
    """Alias for ConfigManager.update_connection()."""
    return ConfigManager.update_connection(env_path, connection, **kwargs)

def _get_default_connection(env_path: str) -> Optional[str]:
    """Alias for ConfigManager.get_default()."""
    return ConfigManager.get_default(env_path)

def _find_available_agent_port(env_path: str) -> int:
    """Alias for AgentPortManager.find_available_port()."""
    return AgentPortManager.find_available_port(env_path)

def _find_running_agent_port(env_path: str) -> Optional[int]:
    """Alias for AgentPortManager.find_running_agent()."""
    return AgentPortManager.find_running_agent(env_path)

def _resolve_connection(global_port: str = None) -> Optional[dict]:
    """Alias for ConnectionResolver.resolve()."""
    return ConnectionResolver.resolve(global_port)

def _set_global_options(port: str = None, agent_port: int = None):
    """Alias for GLOBAL_OPTIONS.set()."""
    GLOBAL_OPTIONS.set(port, agent_port)

def _get_global_options() -> dict:
    """Alias for GLOBAL_OPTIONS.get()."""
    return GLOBAL_OPTIONS.get()
