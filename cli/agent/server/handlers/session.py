import time
import threading
import sys

from ..command_dispatcher import CommandContext


class SessionCommandsMixin:
    def _cmd_session_setup(self, ctx: CommandContext, port: str = None,
                           core: str = "RP2350", device: str = None,
                           as_foreground: bool = True, set_default: bool = False,
                           local_default: str = None) -> dict:
        ppid = ctx.ppid
        if not ppid:
            raise ValueError("Session PPID required")

        if not port and ctx.explicit_port:
            port = ctx.explicit_port

        if not port:
            raise ValueError("Port is required")
        
        conn_key = port

        session = self._get_or_create_session(ppid)
        
        # Update session's workspace default - preserve original case
        if local_default:
            session.default_port = local_default

        existing_conn = self._get_connection(conn_key)

        if existing_conn:
            # Pass original port to session, not normalized
            self.session_manager.add_connection_to_session(
                ppid, port,
                as_foreground=as_foreground,
                default_port=self._default_port
            )

            if set_default:
                self._set_default_port(conn_key)

            return {
                "connected": True,
                "device": existing_conn.device,
                "core": existing_conn.core,
                "version": existing_conn.version,
                "manufacturer": existing_conn.manufacturer,
                "device_root_fs": existing_conn.device_root_fs,
                "port": port,
                "is_foreground": session.foreground == conn_key if session.foreground else False,
                "existing": True,
                "switched_from": None
            }

        new_conn = self._create_board_connection(
            port=port,
            core=core,
            device=device
        )

        self._add_connection(conn_key, new_conn)

        # Pass original port to session
        self.session_manager.add_connection_to_session(
            ppid, port,
            as_foreground=as_foreground,
            default_port=self._default_port
        )

        if set_default:
            self._set_default_port(conn_key)

        return {
            "connected": True,
            "device": new_conn.device,
            "core": new_conn.core,
            "version": new_conn.version,
            "manufacturer": new_conn.manufacturer,
            "device_root_fs": new_conn.device_root_fs,
            "port": port,
            "is_foreground": session.foreground == conn_key if session.foreground else False,
            "existing": False,
            "switched_from": None
        }

    def _cmd_session_disconnect(self, ctx: CommandContext, port: str = None, all_ports: bool = False) -> dict:
        ppid = ctx.ppid
        if not port and ctx.explicit_port:
            port = ctx.explicit_port
        if not ppid:
            raise ValueError("Session PPID required")

        session = self._get_session(ppid)
        if not session:
            raise ValueError("No session found for this terminal")

        ports_to_close = []
        old_foreground = session.foreground

        if all_ports:
            ports_to_close = list(session.get_all_connections())
            freed_port = "all"
        elif port:
            # Find port in session
            found = None
            for conn in session.get_all_connections():
                if conn == port:
                    found = conn
                    break
            if not found:
                raise ValueError(f"Port {port} not in session")
            ports_to_close = [found]
            freed_port = found
        else:
            if not session.foreground:
                raise ValueError("No foreground connection to free")
            ports_to_close = [session.foreground]
            freed_port = session.foreground

        for conn_port in ports_to_close:
            self.connection_manager.disconnect(conn_port)

            for sess in self.session_manager.get_all_sessions().values():
                if conn_port in sess.get_all_connections():
                    sess.remove_connection(conn_port)

        self.session_manager.cleanup_empty_sessions()

        if not self.session_manager.has_sessions():
            def delayed_shutdown():
                time.sleep(0.3)
                self.running = False
                if self.server_socket:
                    try:
                        self.server_socket.close()
                    except Exception:
                        pass

            shutdown_thread = threading.Thread(target=delayed_shutdown, daemon=True)
            shutdown_thread.start()

        remaining_connections = len(session.get_all_connections()) if session and not session.is_empty() else 0
        new_foreground = session.foreground if session and not session.is_empty() else None
        fg_changed = old_foreground != new_foreground

        return {
            "freed_port": freed_port,
            "new_foreground": new_foreground,
            "fg_changed": fg_changed,
            "remaining_connections": remaining_connections
        }

    def _cmd_session_switch_fg(self, ctx: CommandContext) -> dict:
        ppid = ctx.ppid
        if not ppid:
            return {"success": False, "error": "Session PPID required"}

        port = ctx.explicit_port
        if not port:
            return {"success": False, "error": "Port required"}

        conn = self.connection_manager.get_connection(port)
        if not conn:
            return {"success": False, "error": f"Connection {port} not found"}

        session = self._get_session(ppid)
        if not session:
            session = self._get_or_create_session(ppid)

        old_fg = session.foreground

        # Pass original port, not normalized
        session.add_connection(port, as_foreground=True)

        return {
            "success": True,
            "old_foreground": old_fg,
            "new_foreground": port,  # Return original port
            "session_created": old_fg is None
        }

    def _cmd_set_default(self, ctx: CommandContext, port: str = None, update_session: bool = False) -> dict:
        """Set default port. If update_session=True, also update calling session's default_port."""
        port = port or ctx.explicit_port
        if port:
            self._set_default_port(port)
            
            # Update session's local default if requested - preserve original case
            if update_session and ctx.ppid:
                session = self._get_session(ctx.ppid)
                if session:
                    session.default_port = port
            
            return {"default": port, "set": True}
        else:
            self._default_port = None
            return {"default": None, "set": False}

    def _cmd_free(self, ctx: CommandContext) -> dict:
        if ctx.ppid:
            if ctx.explicit_port:
                return self._cmd_session_disconnect(ctx, port=ctx.explicit_port)
            else:
                return self._cmd_session_disconnect(ctx, all_ports=False)

        self._cmd_shutdown(ctx)
        return {"released": True, "port": ""}
