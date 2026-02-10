import time
import threading

from replx.utils.constants import REPL_PROMPT
from ..command_dispatcher import CommandContext
from ..connection_manager import BoardConnection

class ReplCommandsMixin:
    def _repl_reader_loop(self, conn: BoardConnection):
        if not conn or not conn.repl_protocol:
            return
        repl = conn.repl_protocol
        while conn.repl.active and conn.repl_protocol:
            try:
                count = repl.in_waiting()
                if count > 0:
                    data = repl.read_bytes(count)
                    if data:
                        conn.repl.append_output(data)
                else:
                    time.sleep(0.01)
            except Exception:
                break

    def _cmd_repl_enter(self, ctx: CommandContext) -> dict:
        conn = ctx.connection
        if not conn or not conn.repl_protocol:
            raise RuntimeError("Not connected")

        if conn.repl.active:
            conn.repl.stop()

        repl = conn.repl_protocol

        if repl._in_raw_repl:
            try:
                repl._leave_repl()
            except Exception:
                pass

        repl.exit_paste_mode()
        time.sleep(0.05)
        repl.interrupt()
        time.sleep(0.1)

        drain_start = time.time()
        while time.time() - drain_start < 0.3:
            if repl.in_waiting() > 0:
                repl.read_bytes(repl.in_waiting())
                time.sleep(0.02)
            else:
                break

        repl.send_raw(b'\r')
        time.sleep(0.15)

        response = b""
        read_start = time.time()
        while time.time() - read_start < 0.5:
            if repl.in_waiting() > 0:
                response += repl.read_bytes(repl.in_waiting())
                if REPL_PROMPT in response:
                    break
            time.sleep(0.02)

        response_str = response.decode('utf-8', errors='replace')
        prompt_found = '>>>' in response_str

        if prompt_found:
            conn.repl.start(ctx.ppid)
            conn.repl.reader_thread = threading.Thread(
                target=self._repl_reader_loop,
                args=(conn,),
                daemon=True,
                name=f'REPL-Reader-{conn.port}'
            )
            conn.repl.reader_thread.start()

        return {
            "entered": prompt_found,
            "output": response_str
        }

    def _cmd_repl_exit(self, ctx: CommandContext) -> dict:
        conn = ctx.connection
        if not conn:
            return {"exited": False, "reason": "Not connected"}

        if not conn.repl.is_owner(ctx.ppid):
            return {"exited": False, "reason": "Not the REPL session owner"}

        conn.repl.stop()
        conn.release()

        return {"exited": True}

    def _cmd_repl_write(self, ctx: CommandContext, data: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.repl_protocol:
            raise RuntimeError("Not connected")

        if not conn.repl.is_owner(ctx.ppid):
            raise RuntimeError("Not the REPL session owner")

        if isinstance(data, str):
            data = data.encode('utf-8')

        conn.repl_protocol.send_raw(data)
        return {"written": len(data)}

    def _cmd_repl_read(self, ctx: CommandContext) -> dict:
        conn = ctx.connection
        if not conn or not conn.repl_protocol:
            raise RuntimeError("Not connected")

        if not conn.repl.is_owner(ctx.ppid):
            raise RuntimeError("Not the REPL session owner")

        data = conn.repl.read_output()

        return {
            "output": data.decode('utf-8', errors='replace')
        }
