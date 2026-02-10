import os
import posixpath
import time
import tempfile
import base64
import threading

from replx.utils.constants import MAX_PAYLOAD_SIZE
from replx.cli.agent.protocol import AgentProtocol
from ..command_dispatcher import CommandContext

class TransferCommandsMixin:
    def _cmd_get_file(self, ctx: CommandContext, remote_path: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(remote_path, conn)
            content = conn.file_system.get(real_path)
            if len(content) > MAX_PAYLOAD_SIZE - 1000:
                raise RuntimeError(f"File too large for UDP transfer: {len(content)} bytes")
            return {"content": content, "path": remote_path}
        except Exception as e:
            raise RuntimeError(f"get_file failed: {e}")

    def _cmd_put_file(self, ctx: CommandContext, remote_path: str, content: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(remote_path, conn)

            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.tmp') as f:
                f.write(content)
                temp_path = f.name

            try:
                conn.file_system.put(temp_path, real_path)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

            return {"uploaded": remote_path}
        except Exception as e:
            raise RuntimeError(f"put_file failed: {e}")

    def _cmd_put_file_batch(self, ctx: CommandContext, file_specs: list) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        results = []
        try:
            for spec in file_specs:
                local_path = spec.get('local_path')
                remote_path = spec.get('remote_path')
                content_b64 = spec.get('content')

                real_path = self._to_real_path(remote_path, conn)

                if content_b64:
                    content_bytes = base64.b64decode(content_b64)
                    with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.tmp') as f:
                        f.write(content_bytes)
                        local_path = f.name

                    try:
                        conn.file_system.put(local_path, real_path)
                        results.append({"path": remote_path, "success": True})
                    except Exception as e:
                        results.append({"path": remote_path, "success": False, "error": str(e)})
                    finally:
                        if os.path.exists(local_path):
                            os.remove(local_path)
                elif local_path and os.path.exists(local_path):
                    try:
                        conn.file_system.put(local_path, real_path)
                        results.append({"path": remote_path, "success": True})
                    except Exception as e:
                        results.append({"path": remote_path, "success": False, "error": str(e)})
                else:
                    results.append({"path": remote_path, "success": False, "error": "No content or local_path"})

            success_count = sum(1 for r in results if r['success'])
            return {"results": results, "success_count": success_count, "total": len(file_specs)}
        except Exception as e:
            raise RuntimeError(f"put_file_batch failed: {e}")

    def _cmd_get_file_batch(self, ctx: CommandContext, remote_paths: list) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        results = []
        try:
            for remote_path in remote_paths:
                try:
                    real_path = self._to_real_path(remote_path, conn)
                    content = conn.file_system.get(real_path)
                    if isinstance(content, str):
                        content = content.encode('utf-8')
                    content_b64 = base64.b64encode(content).decode('ascii')
                    results.append({"path": remote_path, "content": content_b64, "success": True})
                except Exception as e:
                    results.append({"path": remote_path, "success": False, "error": str(e)})

            success_count = sum(1 for r in results if r['success'])
            return {"results": results, "success_count": success_count, "total": len(remote_paths)}
        except Exception as e:
            raise RuntimeError(f"get_file_batch failed: {e}")

    def _cmd_get_to_local(self, ctx: CommandContext, remote_path: str, local_path: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_path = self._to_real_path(remote_path, conn)
            conn.file_system.get(real_path, local_path)
            return {"downloaded": remote_path, "local_path": local_path, "success": True}
        except Exception as e:
            raise RuntimeError(f"get_to_local failed: {e}")

    def _cmd_put_from_local(self, ctx: CommandContext, local_path: str, remote_path: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        if not os.path.exists(local_path):
            raise RuntimeError(f"Local file not found: {local_path}")

        try:
            real_path = self._to_real_path(remote_path, conn)
            conn.file_system.put(local_path, real_path)
            return {"uploaded": remote_path, "local_path": local_path, "success": True}
        except Exception as e:
            raise RuntimeError(f"put_from_local failed: {e}")

    def _cmd_getdir_to_local(self, ctx: CommandContext, remote_path: str, local_path: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        try:
            real_remote_path = self._to_real_path(remote_path, conn)

            real_remote_path = real_remote_path.rstrip('/')
            if not real_remote_path:
                real_remote_path = conn.device_root_fs.rstrip('/')

            os.makedirs(local_path, exist_ok=True)

            items = conn.file_system.ls_recursive(real_remote_path)

            base_name = posixpath.basename(real_remote_path) if real_remote_path != conn.device_root_fs.rstrip('/') else ''

            files_downloaded = 0
            for item in items:
                rel_path, size, is_dir = item

                if base_name and rel_path.startswith(base_name + '/'):
                    relative = rel_path[len(base_name) + 1:]
                elif base_name and rel_path == base_name:
                    relative = ''
                else:
                    relative = rel_path

                if relative:
                    local_file = os.path.join(local_path, relative.replace('/', os.sep))
                else:
                    local_file = os.path.join(local_path, posixpath.basename(rel_path))

                parent_dir = os.path.dirname(local_file)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)

                if rel_path.startswith('/'):
                    remote_file = rel_path
                else:
                    remote_file = '/' + rel_path

                conn.file_system.get(remote_file, local_file)
                files_downloaded += 1

            return {
                "downloaded_dir": remote_path,
                "local_path": local_path,
                "files_count": files_downloaded,
                "success": True
            }
        except Exception as e:
            raise RuntimeError(f"getdir_to_local failed: {e}")

    def _cmd_getdir_to_local_streaming(self, ctx: CommandContext, remote_path: str, local_path: str):
        conn = ctx.connection
        seq = ctx.seq
        client_addr = ctx.client_addr

        def download_thread():
            try:
                if not conn or not conn.file_system:
                    error_response = AgentProtocol.create_response(seq=seq, error="Not connected")
                    error_data = AgentProtocol.encode_message(error_response)
                    self.server_socket.sendto(error_data, client_addr)
                    return

                ack_msg = AgentProtocol.create_ack(seq)
                ack_data = AgentProtocol.encode_message(ack_msg)
                self.server_socket.sendto(ack_data, client_addr)

                real_remote_path = self._to_real_path(remote_path, conn)
                remote_normalized = real_remote_path.rstrip('/')
                if not remote_normalized:
                    remote_normalized = conn.device_root_fs.rstrip('/')

                os.makedirs(local_path, exist_ok=True)

                items = conn.file_system.ls_recursive(remote_normalized)

                file_items = items
                total_files = len(file_items)

                progress_msg = AgentProtocol.create_progress_stream(seq, {
                    "current": 0,
                    "total": total_files,
                    "status": "starting"
                })
                progress_data = AgentProtocol.encode_message(progress_msg)
                self.server_socket.sendto(progress_data, client_addr)

                files_downloaded = 0

                for item in file_items:
                    rel_path, size, is_dir = item

                    # For root directory downloads, rel_path is already the full path without leading /
                    # For subdirectory downloads, we need to strip the base name
                    is_root_download = (remote_normalized == conn.device_root_fs.rstrip('/') or 
                                       remote_normalized == '/')
                    
                    if is_root_download:
                        # Root download: rel_path like "/boot.py" or "/lib/ticle/utools.mpy"
                        # Strip leading slash for local path construction
                        relative = rel_path.lstrip('/')
                        # Ensure remote_file has proper format
                        if rel_path.startswith('/'):
                            remote_file = rel_path
                        else:
                            remote_file = '/' + rel_path
                    else:
                        # Subdirectory download
                        base_name = posixpath.basename(remote_normalized)
                        if base_name and rel_path.startswith(base_name + '/'):
                            relative = rel_path[len(base_name) + 1:]
                        elif base_name and rel_path == base_name:
                            relative = ''
                        else:
                            relative = rel_path
                        
                        # Construct remote file path
                        if rel_path.startswith('/'):
                            remote_file = rel_path
                        else:
                            remote_file = '/' + rel_path

                    if relative:
                        local_file = os.path.join(local_path, relative.replace('/', os.sep))
                    else:
                        local_file = os.path.join(local_path, posixpath.basename(rel_path))

                    parent_dir = os.path.dirname(local_file)
                    if parent_dir:
                        os.makedirs(parent_dir, exist_ok=True)

                    conn.file_system.get(remote_file, local_file)
                    files_downloaded += 1

                    progress_msg = AgentProtocol.create_progress_stream(seq, {
                        "current": files_downloaded,
                        "total": total_files,
                        "file": posixpath.basename(rel_path),
                        "status": "downloading"
                    })
                    progress_data = AgentProtocol.encode_message(progress_msg)
                    self.server_socket.sendto(progress_data, client_addr)

                final_response = AgentProtocol.create_response(seq=seq, result={
                    "downloaded_dir": remote_path,
                    "local_path": local_path,
                    "files_count": files_downloaded,
                    "success": True
                })
                final_data = AgentProtocol.encode_message(final_response)
                self.server_socket.sendto(final_data, client_addr)

            except Exception as e:
                error_response = AgentProtocol.create_response(seq=seq, error=f"getdir_to_local failed: {e}")
                error_data = AgentProtocol.encode_message(error_response)
                self.server_socket.sendto(error_data, client_addr)
            finally:
                if conn:
                    conn.release()

        thread = threading.Thread(target=download_thread, daemon=True)
        thread.start()

    def _cmd_putdir_from_local(self, ctx: CommandContext, local_path: str, remote_path: str) -> dict:
        conn = ctx.connection
        if not conn or not conn.file_system:
            raise RuntimeError("Not connected")

        if not os.path.exists(local_path) or not os.path.isdir(local_path):
            raise RuntimeError(f"Local directory not found: {local_path}")

        try:
            real_remote_path = self._to_real_path(remote_path, conn)

            files_uploaded = 0

            for root, dirs, files in os.walk(local_path):
                rel_root = os.path.relpath(root, local_path)
                if rel_root == '.':
                    remote_dir = real_remote_path
                else:
                    remote_dir = real_remote_path.rstrip('/') + '/' + rel_root.replace('\\', '/')

                for d in dirs:
                    dir_path = remote_dir.rstrip('/') + '/' + d
                    try:
                        conn.file_system.mkdir(dir_path)
                    except Exception:
                        pass

                for f in files:
                    local_file = os.path.join(root, f)
                    remote_file = remote_dir.rstrip('/') + '/' + f
                    conn.file_system.put(local_file, remote_file)
                    files_uploaded += 1

            return {
                "uploaded_dir": remote_path,
                "local_path": local_path,
                "files_count": files_uploaded,
                "success": True
            }
        except Exception as e:
            raise RuntimeError(f"putdir_from_local failed: {e}")

    def _cmd_put_from_local_streaming(self, ctx: CommandContext, local_path: str, remote_path: str):
        conn = ctx.connection
        seq = ctx.seq
        client_addr = ctx.client_addr

        def upload_thread():
            try:
                if not conn or not conn.file_system:
                    error_response = AgentProtocol.create_response(seq=seq, error="Not connected")
                    error_data = AgentProtocol.encode_message(error_response)
                    self.server_socket.sendto(error_data, client_addr)
                    return

                if not os.path.exists(local_path):
                    error_response = AgentProtocol.create_response(seq=seq, error=f"Local file not found: {local_path}")
                    error_data = AgentProtocol.encode_message(error_response)
                    self.server_socket.sendto(error_data, client_addr)
                    return

                ack_msg = AgentProtocol.create_ack(seq)
                ack_data = AgentProtocol.encode_message(ack_msg)
                self.server_socket.sendto(ack_data, client_addr)

                file_size = os.path.getsize(local_path)
                file_name = os.path.basename(local_path)

                progress_msg = AgentProtocol.create_progress_stream(seq, {
                    "current": 0,
                    "total": file_size,
                    "file": file_name,
                    "status": "starting"
                })
                progress_data = AgentProtocol.encode_message(progress_msg)
                self.server_socket.sendto(progress_data, client_addr)

                real_remote_path = self._to_real_path(remote_path, conn)

                bytes_uploaded = [0]
                last_progress_time = [time.time()]

                def progress_callback(bytes_sent: int, total_bytes: int):
                    bytes_uploaded[0] = bytes_sent
                    now = time.time()
                    if now - last_progress_time[0] >= 0.1:
                        last_progress_time[0] = now
                        progress_msg = AgentProtocol.create_progress_stream(seq, {
                            "current": bytes_sent,
                            "total": total_bytes,
                            "file": file_name,
                            "status": "uploading"
                        })
                        progress_data = AgentProtocol.encode_message(progress_msg)
                        self.server_socket.sendto(progress_data, client_addr)

                conn.file_system.put(local_path, real_remote_path, progress_callback=progress_callback)
                bytes_uploaded[0] = file_size

                final_response = AgentProtocol.create_response(seq=seq, result={
                    "uploaded": remote_path,
                    "local_path": local_path,
                    "bytes": bytes_uploaded[0],
                    "success": True
                })
                final_data = AgentProtocol.encode_message(final_response)
                self.server_socket.sendto(final_data, client_addr)

            except Exception as e:
                error_response = AgentProtocol.create_response(seq=seq, error=str(e))
                error_data = AgentProtocol.encode_message(error_response)
                try:
                    self.server_socket.sendto(error_data, client_addr)
                except Exception:
                    pass
            finally:
                if conn:
                    conn.release()

        thread = threading.Thread(target=upload_thread, daemon=True)
        thread.start()

    def _cmd_putdir_from_local_streaming(self, ctx: CommandContext, local_path: str, remote_path: str):
        EXCLUDE_DIRS = {'__pycache__', '.git', '.svn', '.hg', 'node_modules', '.venv', 'venv', '__MACOSX'}
        EXCLUDE_EXTENSIONS = {'.pyc', '.pyo', '.pyd', '.so', '.dll', '.dylib'}

        conn = ctx.connection
        seq = ctx.seq
        client_addr = ctx.client_addr

        def upload_thread():
            try:
                if not conn or not conn.file_system:
                    error_response = AgentProtocol.create_response(seq=seq, error="Not connected")
                    error_data = AgentProtocol.encode_message(error_response)
                    self.server_socket.sendto(error_data, client_addr)
                    return

                if not os.path.exists(local_path) or not os.path.isdir(local_path):
                    error_response = AgentProtocol.create_response(seq=seq, error=f"Local directory not found: {local_path}")
                    error_data = AgentProtocol.encode_message(error_response)
                    self.server_socket.sendto(error_data, client_addr)
                    return

                ack_msg = AgentProtocol.create_ack(seq)
                ack_data = AgentProtocol.encode_message(ack_msg)
                self.server_socket.sendto(ack_data, client_addr)

                base_local = os.path.abspath(local_path)
                real_remote_path = self._to_real_path(remote_path, conn)
                base_remote = real_remote_path.replace("\\", "/")

                file_specs = []
                dirs_to_create = set()

                for parent, child_dirs, child_files in os.walk(base_local, followlinks=True):
                    child_dirs[:] = [d for d in child_dirs if d not in EXCLUDE_DIRS]

                    rel = os.path.relpath(parent, base_local).replace("\\", "/")
                    remote_parent = posixpath.normpath(
                        posixpath.join(base_remote, "" if rel == "." else rel)
                    )

                    dirs_to_create.add(remote_parent)

                    for filename in child_files:
                        _, ext = os.path.splitext(filename)
                        if ext.lower() in EXCLUDE_EXTENSIONS:
                            continue

                        local_file = os.path.join(parent, filename)
                        remote_file = posixpath.join(remote_parent, filename).replace("\\", "/")
                        file_specs.append((local_file, remote_file, filename))

                total_files = len(file_specs)

                progress_msg = AgentProtocol.create_progress_stream(seq, {
                    "current": 0,
                    "total": total_files,
                    "status": "starting"
                })
                progress_data = AgentProtocol.encode_message(progress_msg)
                self.server_socket.sendto(progress_data, client_addr)

                for dir_path in sorted(dirs_to_create):
                    try:
                        conn.file_system.mkdir(dir_path)
                    except Exception:
                        pass

                # Upload files one by one with individual error handling
                files_uploaded = 0
                for local_file, remote_file, filename in file_specs:
                    max_retries = 3
                    for retry in range(max_retries):
                        try:
                            # Force clean REPL state before each file upload (especially after errors)
                            if retry > 0:
                                try:
                                    # After error, force REPL resync
                                    conn.file_system.repl._in_raw_repl = False
                                    conn.file_system.repl._enter_repl()
                                except Exception:
                                    pass
                            
                            # Clean up any leftover file handles
                            if files_uploaded > 0 or retry > 0:
                                try:
                                    with conn.file_system.repl.session():
                                        conn.file_system.repl._exec("try:\n  f.close()\nexcept:\n  pass")
                                except Exception:
                                    pass
                            
                            conn.file_system.put(local_file, remote_file)
                            files_uploaded += 1
                            
                            # Send progress update
                            progress_msg = AgentProtocol.create_progress_stream(seq, {
                                "current": files_uploaded,
                                "total": total_files,
                                "file": filename,
                                "status": "uploading"
                            })
                            progress_data = AgentProtocol.encode_message(progress_msg)
                            self.server_socket.sendto(progress_data, client_addr)
                            break  # Success, move to next file
                            
                        except Exception as e:
                            if retry == max_retries - 1:
                                # Last retry failed - log error but continue with other files
                                import sys
                                print(f"Warning: Failed to upload {filename} after {max_retries} attempts: {e}", file=sys.stderr)
                            else:
                                # Wait longer before retry
                                time.sleep(0.5)

                final_response = AgentProtocol.create_response(seq=seq, result={
                    "uploaded_dir": remote_path,
                    "local_path": local_path,
                    "files_count": total_files,
                    "success": True
                })
                final_data = AgentProtocol.encode_message(final_response)
                self.server_socket.sendto(final_data, client_addr)

            except Exception as e:
                error_response = AgentProtocol.create_response(seq=seq, error=str(e))
                error_data = AgentProtocol.encode_message(error_response)
                try:
                    self.server_socket.sendto(error_data, client_addr)
                except Exception:
                    pass
            finally:
                if conn:
                    conn.release()

        thread = threading.Thread(target=upload_thread, daemon=True)
        thread.start()
