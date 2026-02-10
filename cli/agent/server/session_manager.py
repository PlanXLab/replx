import os
import sys
import time
import threading
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Set, Tuple


@dataclass
class Session:
    ppid: int
    foreground: Optional[str] = None
    backgrounds: Set[str] = field(default_factory=set)
    last_access: float = field(default_factory=time.time)
    default_port: Optional[str] = None  # This session's workspace default

    def add_connection(self, port: str, as_foreground: bool = False):
        # Store original port
        if as_foreground:
            if self.foreground and self.foreground != port:
                self.backgrounds.add(self.foreground)
            self.foreground = port
            # Remove from backgrounds
            for bg in list(self.backgrounds):
                if bg == port:
                    self.backgrounds.discard(bg)
        else:
            if not self.foreground or self.foreground != port:
                self.backgrounds.add(port)
        self.last_access = time.time()

    def remove_connection(self, port: str) -> bool:
        was_foreground = self.foreground and self.foreground == port
        if was_foreground:
            self.foreground = None
            if self.backgrounds:
                # Pop any item
                self.foreground = self.backgrounds.pop()
        else:
            # Remove from backgrounds
            for bg in list(self.backgrounds):
                if bg == port:
                    self.backgrounds.discard(bg)
                    break
        self.last_access = time.time()
        return was_foreground

    def get_all_connections(self) -> List[str]:
        result = list(self.backgrounds)
        if self.foreground:
            result.insert(0, self.foreground)
        return result

    def has_connection(self, port: str) -> bool:
        if self.foreground and self.foreground == port:
            return True
        return any(bg == port for bg in self.backgrounds)

    def is_empty(self) -> bool:
        return self.foreground is None and not self.backgrounds

    def switch_foreground(self, port: str) -> bool:
        if self.foreground and self.foreground == port:
            return True

        # Check if port is in backgrounds
        found_bg = None
        for bg in self.backgrounds:
            if bg == port:
                found_bg = bg
                break
        
        if not found_bg:
            return False

        if self.foreground:
            self.backgrounds.add(self.foreground)

        self.backgrounds.discard(found_bg)
        self.foreground = found_bg  # Use original case from backgrounds
        self.last_access = time.time()

        return True

class SessionManager:
    def __init__(self):
        self._sessions: Dict[int, Session] = {}
        self._sessions_lock = threading.RLock()
        self._last_zombie_check = time.time()
        self._zombie_check_interval = 60

    def get_session(self, ppid: int) -> Optional[Session]:
        with self._sessions_lock:
            return self._sessions.get(ppid)

    def get_or_create_session(self, ppid: int) -> Session:
        with self._sessions_lock:
            if ppid not in self._sessions:
                self._sessions[ppid] = Session(ppid=ppid)
            session = self._sessions[ppid]
            session.last_access = time.time()
            return session

    def get_all_sessions(self) -> Dict[int, Session]:
        with self._sessions_lock:
            return dict(self._sessions)

    def remove_session(self, ppid: int) -> Optional[Session]:
        with self._sessions_lock:
            return self._sessions.pop(ppid, None)

    def add_connection_to_session(
        self,
        ppid: int,
        port: str,
        as_foreground: bool = False,
        default_port: str = None
    ) -> Session:
        # Keep original port case
        
        with self._sessions_lock:
            # Get or create session inside the lock to avoid double-locking
            if ppid not in self._sessions:
                self._sessions[ppid] = Session(ppid=ppid)
            session = self._sessions[ppid]
            session.last_access = time.time()
            
            if as_foreground:
                session.add_connection(port, as_foreground=True)
            elif session.foreground is None:
                if default_port and default_port != port:
                    session.add_connection(default_port, as_foreground=True)
                    session.add_connection(port, as_foreground=False)
                else:
                    session.add_connection(port, as_foreground=True)
            else:
                session.add_connection(port, as_foreground=False)

        return session

    def remove_connection_from_session(self, ppid: int, port: str) -> Tuple[bool, bool]:
        session = self.get_session(ppid)
        if not session:
            return False, False

        with self._sessions_lock:
            was_foreground = session.remove_connection(port)
            return True, was_foreground

    def remove_connection_from_all_sessions(self, port: str) -> List[int]:
        # Keep original port case for comparison
        affected_sessions = []

        with self._sessions_lock:
            for ppid, session in self._sessions.items():
                if session.has_connection(port):
                    session.remove_connection(port)
                    affected_sessions.append(ppid)

        return affected_sessions

    def get_foreground(self, ppid: int) -> Optional[str]:
        session = self.get_session(ppid)
        return session.foreground if session else None

    def switch_foreground(self, ppid: int, port: str) -> bool:
        session = self.get_session(ppid)
        if not session:
            return False

        with self._sessions_lock:
            return session.switch_foreground(port)

    def resolve_port(self, ppid: int, explicit_port: str = None, default_port: str = None) -> Optional[str]:
        """Resolve port for ConnectionManager lookup."""
        if explicit_port:
            return explicit_port

        session = self.get_session(ppid)
        if session and session.foreground:
            return session.foreground

        if default_port:
            return default_port

        return None

    def is_process_alive(self, pid: int) -> bool:
        """Check if a process is alive across platforms."""
        try:
            if sys.platform == 'win32':
                import ctypes
                kernel32 = ctypes.windll.kernel32
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                STILL_ACTIVE = 259
                
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if handle:
                    try:
                        exit_code = ctypes.c_ulong()
                        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                            return exit_code.value == STILL_ACTIVE
                        return False
                    finally:
                        kernel32.CloseHandle(handle)
                return False
            else:
                # Unix: Use kill with signal 0 to test if process exists
                os.kill(pid, 0)
                return True
        except (OSError, PermissionError):
            return False

    def cleanup_zombie_sessions(self) -> List[int]:
        with self._sessions_lock:
            zombie_ppids = []
            for ppid in list(self._sessions.keys()):
                if not self.is_process_alive(ppid):
                    zombie_ppids.append(ppid)

            for ppid in zombie_ppids:
                self._sessions.pop(ppid, None)

            self._last_zombie_check = time.time()
            return zombie_ppids

    def get_session_info(self) -> Dict[str, Any]:
        sessions_info = []

        with self._sessions_lock:
            for ppid, session in self._sessions.items():
                sessions_info.append({
                    'ppid': ppid,
                    'foreground': session.foreground,
                    'backgrounds': list(session.backgrounds),
                    'last_access': session.last_access
                })

        return {'sessions': sessions_info}

    def find_sessions_using_port(self, port: str) -> List[Dict[str, Any]]:
        """Find all sessions using a specific port."""
        result = []

        with self._sessions_lock:
            for ppid, session in self._sessions.items():
                if session.has_connection(port):
                    is_fg = session.foreground and session.foreground == port
                    result.append({
                        'ppid': ppid,
                        'is_foreground': is_fg
                    })

        return result

    def clear_all_sessions(self):
        with self._sessions_lock:
            self._sessions.clear()

    def cleanup_empty_sessions(self) -> List[int]:
        with self._sessions_lock:
            empty = [ppid for ppid, sess in self._sessions.items() if sess.is_empty()]
            for ppid in empty:
                self._sessions.pop(ppid, None)
            return empty

    def has_sessions(self) -> bool:
        with self._sessions_lock:
            return len(self._sessions) > 0
