import os
from typing import Optional
import psutil

def _find_terminal_process() -> Optional[dict]:
    terminal_names = {
        'powershell.exe', 'pwsh.exe', 'cmd.exe', 'bash.exe', 'zsh.exe', 'sh.exe', 'fish.exe',
        'WindowsTerminal.exe', 'ConEmu64.exe', 'ConEmu.exe',
        'Code.exe',
        'pycharm', 'pycharm64.exe', 'idea', 'idea64.exe',
    }

    try:
        current = psutil.Process()
        for level in range(10):
            if current is None:
                break

            if current.name() in terminal_names:
                return {
                    'pid': current.pid,
                    'name': current.name(),
                    'create_time': current.create_time(),
                    'cwd': current.cwd() if hasattr(current, 'cwd') else None,
                    'level': level
                }

            if current.ppid() == 0:
                break

            parent = current.parent()
            if parent is None:
                break
            current = parent

    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass

    return None

def _find_jupyter_kernel() -> Optional[dict]:
    try:
        current = psutil.Process()
        for level in range(10):
            if current is None:
                break

            cmdline = ' '.join(current.cmdline()).lower()

            if any(keyword in cmdline for keyword in ['jupyter', 'ipykernel', 'ipython']):
                return {
                    'pid': current.pid,
                    'name': current.name(),
                    'cmdline': cmdline,
                    'level': level
                }

            if current.ppid() == 0:
                break

            parent = current.parent()
            if parent is None:
                break
            current = parent

    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass

    return None

def _detect_environment() -> str:
    try:
        __IPYTHON__
        return 'ipython'
    except NameError:
        pass

    env = os.environ

    if any(key.startswith('JPY_') or 'JUPYTER' in key for key in env):
        return 'jupyter'

    if env.get('TERM_PROGRAM') == 'vscode':
        return 'vscode_terminal'

    if env.get('SHELL') or env.get('TERM'):
        return 'terminal'

    return 'unknown'

def get_session_id() -> int:
    terminal = _find_terminal_process()
    if terminal:
        return terminal['pid']

    jupyter = _find_jupyter_kernel()
    if jupyter:
        return jupyter['pid']

    ppid = os.getppid()
    if ppid and ppid > 0:
        return ppid

    workspace_hash = abs(hash(os.getcwd())) % (10**8)
    return workspace_hash

_session_id_cache: Optional[int] = None

def get_cached_session_id() -> int:
    global _session_id_cache

    if _session_id_cache is None:
        _session_id_cache = get_session_id()

    return _session_id_cache

def clear_session_cache():
    global _session_id_cache
    _session_id_cache = None
