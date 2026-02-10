import os
import ast
import json
import textwrap
import posixpath
from typing import Tuple, List

from replx.utils.exceptions import ProtocolError, TransportError, FileSystemError
from replx.utils.constants import (
    DEVICE_CHUNK_SIZE_DEFAULT, DEVICE_CHUNK_SIZE_EFR32MG,
    PUT_BATCH_BYTES_DEFAULT, PUT_BATCH_BYTES_EFR32MG,
)
from replx.utils.device_info import normalize_core


class DeviceStorage:

    def __init__(self, repl_protocol, core: str = "RP2350", device: str = "", device_root_fs: str = "/"):
        self.repl = repl_protocol
        # Normalize core name (e.g., RP2350B -> RP2350)
        self.core = normalize_core(core)
        self.device = device
        self.device_root_fs = device_root_fs
        
        normalized_core = self.core
        if normalized_core == "EFR32MG":
            self._DEVICE_CHUNK_SIZES = DEVICE_CHUNK_SIZE_EFR32MG
            self._PUT_BATCH_BYTES = PUT_BATCH_BYTES_EFR32MG
        else:
            self._DEVICE_CHUNK_SIZES = DEVICE_CHUNK_SIZE_DEFAULT
            self._PUT_BATCH_BYTES = PUT_BATCH_BYTES_DEFAULT
        
        if device == "xnode":
            self._ls_detailed_func = self._ls_detailed_xbee
            self._ls_recursive_func = self._ls_recursive_xbee
        else:
            self._ls_detailed_func = self._ls_detailed_standard
            self._ls_recursive_func = self._ls_recursive_standard

    def ls(self, path: str = "/") -> List[str]:
        """List directory contents.
        
        Raises:
            TransportError: If the serial port has been disconnected.
        """
        safe_path = path.replace("'", "\\'")
        command = f"""
import os
try:
    print(os.listdir('{safe_path}'))
except Exception as e:
    print('__ERROR__:' + str(e))
"""
        # Let TransportError propagate - this indicates disconnection
        result = self.repl.exec(command).decode('utf-8').strip()
        if result.startswith("__ERROR__:"):
            raise FileSystemError(f"ls failed: {result[len('__ERROR__:'):].strip()}")
        try:
            return ast.literal_eval(result)
        except (ValueError, SyntaxError):
            return []

    def ls_detailed(self, path: str = "/") -> List[list]:
        """List directory contents with details.
        
        Raises:
            TransportError: If the serial port has been disconnected.
        """
        return self._ls_detailed_func(path)

    def ls_recursive(self, path: str = "/") -> List[dict]:
        """List directory contents recursively.
        
        Raises:
            TransportError: If the serial port has been disconnected.
        """
        return self._ls_recursive_func(path)

    def _ls_detailed_standard(self, dir: str = "/") -> List[list]:
        dir = self._normalize_remote_path(dir)
        if not dir.startswith("/"):
            dir = "/" + dir
        # Remove trailing slash for path concatenation (but keep '/' as is)
        if dir != '/' and dir.endswith('/'):
            dir = dir.rstrip('/')
        
        command = f"""
import os
import json
def get_detailed_listing(path):
    try:
        items = []
        for item in os.listdir(path):
            full_path = path + ('/' + item if path != '/' else item)
            try:
                stat_info = os.stat(full_path)
                is_dir = stat_info[0] & 0x4000 != 0
                size = 0 if is_dir else stat_info[6]
                items.append([item, size, is_dir])
            except Exception:
                try:
                    os.listdir(full_path)
                    items.append([item, 0, True])
                except Exception:
                    items.append([item, 0, False])
        return sorted(items, key=lambda x: (not x[2], x[0].lower()))
    except Exception as e:
        return {{'__error__': str(e)}}
print(json.dumps(get_detailed_listing('{dir}')))
"""
        try:
            # Let TransportError propagate - this indicates disconnection
            out = self.repl.exec(command)
            result_str = out.decode("utf-8", errors='replace').strip()
            result = json.loads(result_str)
            if isinstance(result, dict) and "__error__" in result:
                raise FileSystemError(f"ls failed: {result['__error__']}")
            return result
        except TransportError:
            # Re-raise transport errors (serial port disconnected)
            raise
        except (json.JSONDecodeError, ProtocolError):
            pass
        return []

    def _ls_detailed_xbee(self, dir: str = "/") -> List[list]:
        if not dir.startswith("/"):
            dir = "/" + dir
        
        command = f"""
import os
import json
def xbee_is_dir(path):
    try:
        os.listdir(path)
        return True
    except OSError:
        return False

def xbee_get_file_size(path):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
        return size
    except:
        return 0

def get_detailed_listing(path):
    try:
        items = []
        for item in os.listdir(path):
            full_path = path + ('/' + item if path != '/' else item)
            is_dir = xbee_is_dir(full_path)
            if is_dir:
                size = 0
            else:
                size = xbee_get_file_size(full_path)
            items.append([item, size, is_dir])
        return sorted(items, key=lambda x: (not x[2], x[0].lower()))
    except Exception as e:
        return {{'__error__': str(e)}}
print(json.dumps(get_detailed_listing('{dir}')))
"""
        try:
            out = self.repl.exec(command)
            result = json.loads(out.decode("utf-8").strip())
            if isinstance(result, dict) and "__error__" in result:
                raise FileSystemError(f"ls failed: {result['__error__']}")
            return result
        except TransportError:
            # Re-raise transport errors (serial port disconnected)
            raise
        except (json.JSONDecodeError, ProtocolError):
            return []

    def _ls_recursive_standard(self, dir: str = "/") -> List[list]:
        dir = self._normalize_remote_path(dir)
        if not dir.startswith("/"):
            dir = "/" + dir
        
        command = f"""
import os
import json

def get_recursive_listing(path, base_path=None):
    if base_path is None:
        base_path = path
    items = []
    try:
        entries = os.listdir(path)
        for item in entries:
            full_path = path + ('/' + item if path != '/' else item)
            
            # Calculate relative path from base
            if base_path == '/':
                rel_path = full_path
            else:
                rel_path = full_path[len(base_path):].lstrip('/')
            
            try:
                stat_info = os.stat(full_path)
                is_dir = stat_info[0] & 0x4000 != 0
                size = 0 if is_dir else stat_info[6]
            except Exception:
                try:
                    os.listdir(full_path)
                    is_dir = True
                    size = 0
                except Exception:
                    is_dir = False
                    size = 0
            
            if not is_dir:
                items.append([rel_path, size, is_dir])
            else:
                child = get_recursive_listing(full_path, base_path)
                if isinstance(child, dict) and '__error__' in child:
                    return child
                items.extend(child)
        
    except Exception as e:
        return {{'__error__': str(e)}}
    
    return sorted(items, key=lambda x: x[0].lower())

print(json.dumps(get_recursive_listing('{dir}')))
"""
        try:
            out = self.repl.exec(command)
            result = json.loads(out.decode("utf-8").strip())
            if isinstance(result, dict) and "__error__" in result:
                raise FileSystemError(f"ls_recursive failed: {result['__error__']}")
            return result
        except TransportError:
            # Re-raise transport errors (serial port disconnected)
            raise
        except (json.JSONDecodeError, ProtocolError):
            return self._ls_recursive_fallback(dir)

    def _ls_recursive_xbee(self, dir: str = "/") -> List[list]:
        if not dir.startswith("/"):
            dir = "/" + dir
        
        command = f"""
import os
import json

def xbee_is_dir(path):
    try:
        os.listdir(path)
        return True
    except OSError:
        return False

def xbee_get_file_size(path):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
        return size
    except Exception:
        return 0

def get_recursive_listing(path, base_path=None):
    if base_path is None:
        base_path = path
    items = []
    try:
        entries = os.listdir(path)
        for item in entries:
            full_path = path + ('/' + item if path != '/' else item)
            
            # Calculate relative path from base
            if base_path == '/':
                rel_path = full_path
            else:
                rel_path = full_path[len(base_path):].lstrip('/')
            
            is_dir = xbee_is_dir(full_path)
            
            if is_dir:
                child = get_recursive_listing(full_path, base_path)
                if isinstance(child, dict) and '__error__' in child:
                    return child
                items.extend(child)
            else:
                size = xbee_get_file_size(full_path)
                items.append([rel_path, size, is_dir])
        
    except Exception as e:
        return {{'__error__': str(e)}}
    
    return sorted(items, key=lambda x: x[0].lower())

print(json.dumps(get_recursive_listing('{dir}')))
"""
        try:
            out = self.repl.exec(command)
            result = json.loads(out.decode("utf-8").strip())
            if isinstance(result, dict) and "__error__" in result:
                raise FileSystemError(f"ls_recursive failed: {result['__error__']}")
            return result
        except TransportError:
            # Re-raise transport errors (serial port disconnected)
            raise
        except (json.JSONDecodeError, ProtocolError):
            return self._ls_recursive_fallback(dir)

    def _ls_recursive_fallback(self, dir: str = "/") -> List[list]:
        result = []
        
        def walk_dir(current_dir, base_dir):
            items = self.ls_detailed(current_dir)
            for name, size, is_dir in items:
                full_path = posixpath.join(current_dir, name)
                if base_dir == '/':
                    rel_path = full_path
                else:
                    rel_path = full_path[len(base_dir):].lstrip('/')
                    if base_dir != '/':
                        rel_path = base_dir[1:] + '/' + rel_path if rel_path else base_dir[1:]
                
                if not is_dir:
                    result.append([rel_path, size, is_dir])
                else:
                    walk_dir(full_path, base_dir)
        
        try:
            walk_dir(dir, dir)
        except FileSystemError:
            raise
        except Exception:
            pass
        
        return sorted(result, key=lambda x: x[0].lower())

    def is_dir(self, path: str) -> bool:
        """Check if the given path is a directory."""
        path = self._normalize_remote_path(path)
        
        if self.core == "EFR32MG":
            command = f"""
import os
try:
    os.listdir('{path}')
    result = True
except OSError:
    result = False
print(result)
"""
        else:
            command = f"""
import os
try:
    result = os.stat('{path}')[0] & 0x4000 != 0
except Exception:
    try:
        os.listdir('{path}')
        result = True
    except OSError:
        result = False
print(result)
"""
        out = self.repl.exec(command)
        try:
            return ast.literal_eval(out.decode("utf-8").strip())
        except:
            return False

    def state(self, path: str) -> int:
        path = self._normalize_remote_path(path)
        
        if self.core == "EFR32MG":
            command = f"""
try:
    with open('{path}', 'rb') as f:
        f.seek(0, 2)
        size = f.tell()
    print(size)
except Exception:
    print(0)
"""
        else:
            safe_path = path.replace("'", "\\'")
            command = f"""
import os
try:
    st = os.stat('{safe_path}')
    print(st[6])
except Exception:
    print(0)
"""
        out = self.repl.exec(command)
        result = out.decode('utf-8').strip()
        try:
            return int(result)
        except ValueError:
            return 0

    def _file_exists(self, path: str) -> bool:
        safe_path = path.replace("'", "\\'")
        
        if self.core == "EFR32MG":
            command = f"""
try:
    f = open('{safe_path}', 'rb')
    f.close()
    print('1')
except Exception:
    print('0')
"""
        else:
            command = f"""
import os
try:
    os.stat('{safe_path}')
    print('1')
except Exception:
    print('0')
"""
        result = self.repl.exec(command).decode('utf-8').strip()
        return result == '1'

    def mkdir(self, dir: str) -> bool:
        """Create directory on device. Uses forward slashes for device paths."""
        # Normalize to forward slashes for device
        dir = dir.replace('\\', '/')
        command = f"""
import os
def mkdir(dir):
    # Device uses forward slashes
    parts = dir.split('/')
    dirs = ['/'.join(parts[:i+1]) for i in range(len(parts))]
    check = 0
    for d in dirs:
        try:
            os.mkdir(d)
        except OSError as e:
            check += 1
            if "EEXIST" in str(e):
                continue
            else:
                return False
    return check < len(parts)
print(mkdir('{dir}'))
"""
        out = self.repl.exec(command)
        try:
            return ast.literal_eval(out.decode("utf-8").strip())
        except Exception:
            return False

    def rm(self, filename: str):
        command = f"""
import os
os.remove('{filename}')
"""
        self.repl.exec(command)

    def touch(self, filename: str, core: str = None):
        """Create file if not exists, preserve content if exists (like Unix touch)."""
        core = core or self.core
        if core == "EFR32MG":
            command = f"""
import os
try:
    # Try to open file to check if it exists
    f = open('{filename}', 'rb')
    f.close()
except Exception:
    # File doesn't exist, create it
    f = open('{filename}', 'wb')
    f.close()
"""
        else:
            command = f"f = open('{filename}', 'a'); f.close()"
        self.repl.exec(command)

    def rmdir(self, dir: str):
        """Remove a directory and all its contents recursively."""
        if self.core == "EFR32MG":
            command = f"""
import os
def rmdir(p):
    for f in os.listdir(p):
        fp = p + '/' + f
        try:
            os.remove(fp)
        except OSError:
            rmdir(fp)
    os.rmdir(p)
rmdir('{dir}')
"""
        else:
            command = f"""
import os
def rmdir(p):
    for name in os.listdir(p):
        fp = p + '/' + name if p != '/' else '/' + name
        try:
            if os.stat(fp)[0] & 0x4000:
                rmdir(fp)
            else:
                os.remove(fp)
        except OSError:
            try:
                rmdir(fp)
            except Exception:
                pass
    os.rmdir(p)
rmdir('{dir}')
"""
        self.repl.exec(command)

    def cp(self, source: str, dest: str):
        command = f"""
with open('{source}', 'rb') as src:
    with open('{dest}', 'wb') as dst:
        while True:
            chunk = src.read(512)
            if not chunk:
                break
            dst.write(chunk)
"""
        self.repl.exec(command)

    def mv(self, source: str, dest: str):
        if self.core == "EFR32MG":
            command = f"""
import os
try:
    os.listdir('{source}')
    is_dir = True
except Exception:
    is_dir = False

if is_dir:
    # For directories: create dest, copy contents recursively, remove source
    def copy_dir(src, dst):
        try:
            os.mkdir(dst)
        except Exception:
            pass
        for f in os.listdir(src):
            sf = src + '/' + f
            df = dst + '/' + f
            try:
                os.listdir(sf)
                copy_dir(sf, df)
            except Exception:
                with open(sf, 'rb') as s:
                    with open(df, 'wb') as d:
                        while True:
                            c = s.read(512)
                            if not c:
                                break
                            d.write(c)
    def rm_dir(p):
        for f in os.listdir(p):
            fp = p + '/' + f
            try:
                os.remove(fp)
            except OSError:
                rm_dir(fp)
        os.rmdir(p)
    copy_dir('{source}', '{dest}')
    rm_dir('{source}')
else:
    # For files: simple copy and remove
    with open('{source}', 'rb') as src:
        with open('{dest}', 'wb') as dst:
            while True:
                chunk = src.read(512)
                if not chunk:
                    break
                dst.write(chunk)
    os.remove('{source}')
"""
        else:
            command = f"""
import os
os.rename('{source}', '{dest}')
"""
        self.repl.exec(command)

    def mem(self) -> Tuple[int, int, int, float]:
        command = """
import gc
gc.collect()
free = gc.mem_free()
alloc = gc.mem_alloc()
total = free + alloc
usage_pct = round(alloc / total * 100, 2)
print(free, alloc, total, usage_pct)
"""
        out = self.repl.exec(command)
        free_str, alloc_str, total_str, usage_pct_str = out.decode("utf-8").strip().split()
        return int(free_str), int(alloc_str), int(total_str), float(usage_pct_str)

    def df(self) -> Tuple[int, int, int, float]:
        command = """
import os
def get_fs_info(path='/'):
    stats = os.statvfs(path)
    block_size = stats[0]
    total_blocks = stats[2]
    free_blocks = stats[3]
    total = block_size * total_blocks
    free = block_size * free_blocks
    used = total - free
    usage_pct = round(used / total * 100, 2) if total > 0 else 0.0
    return total, used, free, usage_pct
print(get_fs_info())
"""
        out = self.repl.exec(command)
        return ast.literal_eval(out.decode("utf-8").strip())

    def format(self) -> bool:
        # Normalize core name (e.g., RP2350B -> RP2350)
        normalized_core = normalize_core(self.core)
        
        if normalized_core in ("ESP32S3", "ESP32C6"):
            command = """
import os
from flashbdev import bdev
os.umount('/')
os.VfsLfs2.mkfs(bdev)
os.mount(bdev, '/')
"""
        elif normalized_core == "EFR32MG":
            command = """
import os
os.format()
"""
        elif normalized_core == "RP2350":
            command = """
import os, rp2
try:
    os.umount('/')
except Exception:
    pass
bdev = rp2.Flash()
os.VfsLfs2.mkfs(bdev, progsize=256)
fs = os.VfsLfs2(bdev, progsize=256)
os.mount(fs, '/')
"""
        elif normalized_core == "MIMXRT1062DVJ6A":
            command = """
import os, mimxrt
try:
    os.umount('/flash')
except Exception:
    pass
bdev = mimxrt.Flash()
os.VfsLfs2.mkfs(bdev)
fs = os.VfsLfs2(bdev)
os.mount(fs, '/flash')
os.chdir('/flash')
"""
        else:
            return False
        
        try:
            self.repl.exec(command)
        except ProtocolError:
            return False
        return True

    def get(self, remote: str, local: str = None) -> bytes:
        """Download a file from the device."""
        import binascii as binascii_module
        
        local_file = None
        content_parts = []
        
        if local:
            if os.path.isdir(local):
                local = os.path.join(local, os.path.basename(remote))
            local_file = open(local, "wb")
        
        bytes_read = 0

        try:
            file_size = self.state(remote)
            
            if file_size == 0:
                if not self._file_exists(remote):
                    raise ProtocolError(f"File not found: {remote}")
                return b"" if not local else None

            with self.repl.session():
                init_command = f"""
import sys
import binascii
f = open('{remote}', 'rb')
"""
                self.repl._exec(textwrap.dedent(init_command))
                
                CHUNK_SIZE = 12288
                
                while bytes_read < file_size:
                    remaining = min(CHUNK_SIZE, file_size - bytes_read)

                    read_cmd = f"""
chunk = f.read({remaining})
if chunk:
    encoded = binascii.b2a_base64(chunk)
    sys.stdout.write(encoded.decode('ascii'))
"""
                    encoded_data = self.repl._exec(textwrap.dedent(read_cmd))
                    
                    if encoded_data:
                        try:
                            # Strip whitespace/newlines that may be added by REPL output
                            cleaned_data = encoded_data.strip().replace(b'\r', b'').replace(b'\n', b'')
                            chunk_data = binascii_module.a2b_base64(cleaned_data)
                        except Exception as e:
                            raise ProtocolError(f"Failed to decode base64 data: {e}")
                        
                        if local_file:
                            local_file.write(chunk_data)
                        else:
                            content_parts.append(chunk_data)
                    
                        bytes_read += len(chunk_data)
                    else:
                        break

                self.repl._exec("f.close()")
        
        except ProtocolError:
            raise
        except Exception as e:
            raise ProtocolError(f"Download failed: {e}")
        finally:
            if local_file:
                local_file.close()

        if bytes_read != file_size:
            raise ProtocolError(f"Download incomplete: got {bytes_read}/{file_size} bytes")
        
        if not local:
            return b''.join(content_parts)

    def put(self, local: str, remote: str, progress_callback=None):
        """Upload a file to the device."""
        sent = 0
        needs_retry = False
        file_opened = False

        with self.repl.session():
            try:
                self.repl._exec(f"f = open('{remote}', 'wb')")
                file_opened = True
            except ProtocolError as e:
                if "EEXIST" in str(e):
                    needs_retry = True
                else:
                    raise
            else:
                try:
                    with open(local, "rb") as f:
                        total = os.fstat(f.fileno()).st_size
                        
                        if progress_callback:
                            progress_callback(0, total)

                        batch_src_lines = []
                        batch_bytes = 0
                        DEVICE_CHUNK = self._DEVICE_CHUNK_SIZES
                        BATCH_LIMIT = max(8 * 1024, int(self._PUT_BATCH_BYTES))

                        def _flush_batch():
                            nonlocal batch_src_lines, batch_bytes
                            if not batch_src_lines:
                                return

                            code = ";\n".join(batch_src_lines)
                            self.repl._exec(code)
                            batch_src_lines = []
                            batch_bytes = 0
                            
                            if progress_callback:
                                progress_callback(sent, total)

                        while True:
                            chunk = f.read(DEVICE_CHUNK)
                            if not chunk:
                                _flush_batch()
                                break

                            line = f"f.write({repr(chunk)})"
                            batch_src_lines.append(line)
                            batch_bytes += len(line)
                            sent += len(chunk)

                            if batch_bytes >= BATCH_LIMIT:
                                _flush_batch()

                    self.repl._exec("f.close()")
                    file_opened = False
                    
                    if progress_callback:
                        progress_callback(total, total)
                        
                except Exception as e:
                    # Ensure file handle is closed on error
                    if file_opened:
                        try:
                            self.repl._exec("try:\n  f.close()\nexcept:\n  pass")
                        except Exception:
                            pass
                    raise

        if needs_retry:
            self.rm(remote)
            self.put(local, remote, progress_callback)

    def getdir_batch(self, remote: str, local: str, progress_callback=None):
        base_remote = remote.replace("\\", "/")
        base_local = os.path.abspath(local)
        
        try:
            items = self.ls_recursive(base_remote)
        except Exception as e:
            raise ProtocolError(f"Failed to list remote directory: {e}")
        
        if not items:
            return
        
        file_specs = []
        for rel_path, size, is_dir in items:
            if is_dir:
                continue
            
            local_path = os.path.join(base_local, rel_path.replace('/', os.sep))
            local_dir = os.path.dirname(local_path)
            if local_dir and not os.path.exists(local_dir):
                os.makedirs(local_dir, exist_ok=True)
            
            if base_remote == '/':
                full_remote = '/' + rel_path
            else:
                full_remote = base_remote + ('/' if not base_remote.endswith('/') else '') + rel_path
            
            file_specs.append((full_remote, local_path))
        
        if not file_specs:
            return
        
        total = len(file_specs)
        for idx, (remote_file, local_file) in enumerate(file_specs):
            if progress_callback:
                progress_callback(idx, total, os.path.basename(remote_file))
            
            try:
                self.get(remote_file, local_file)
            except Exception as e:
                raise ProtocolError(f"Failed to download {remote_file}: {e}")
        
        if progress_callback:
            progress_callback(total, total, "Complete")

    def putdir(self, local: str, remote: str):
        """Upload directory from local to device.
        
        Args:
            local: Local directory path (OS-specific separators)
            remote: Remote path on device (uses forward slashes)
        """
        import posixpath
        
        base_local = os.path.abspath(local)
        # Normalize remote to forward slashes for device
        base_remote = remote.replace("\\", "/")
        
        for parent, child_dirs, child_files in os.walk(base_local, followlinks=True):
            # Convert local relative path to device path (forward slashes)
            rel = os.path.relpath(parent, base_local).replace("\\", "/")
            remote_parent = posixpath.normpath(
                posixpath.join(base_remote, "" if rel == "." else rel)
            )
            try:
                self.mkdir(remote_parent)
            except Exception:
                pass

            for filename in child_files:
                local_path = os.path.join(parent, filename)
                # Ensure remote path uses forward slashes
                remote_path = posixpath.join(remote_parent, filename)
                self.put(local_path, remote_path)

    def _normalize_remote_path(self, path: str) -> str:
        # Check both with and without trailing slash
        root_with_slash = self.device_root_fs
        root_without_slash = self.device_root_fs.rstrip('/')
        
        # If path already starts with device_root_fs (with or without slash), return as-is
        if path.startswith(root_with_slash) or path == root_without_slash or path.startswith(root_without_slash + '/'):
            return path
        
        if path.startswith("/"):
            path = path[1:]
        return posixpath.join(self.device_root_fs, path)

    def _print_progress_bar(self, current: int, total: int, bar_length: int = 40):
        pct = 0 if total == 0 else min(1.0, current / total)
        block = min(bar_length, int(round(bar_length * pct)))
        bar = "#" * block + "-" * (bar_length - block)
        percent = int(pct * 100)
        print(f"\r[{bar}] {percent}% ({current}/{total})", end="", flush=True)


def create_storage(repl_protocol, core: str = "RP2350", device: str = "", device_root_fs: str = "/") -> DeviceStorage:
    return DeviceStorage(
        repl_protocol,
        core=core,
        device=device,
        device_root_fs=device_root_fs
    )


SerialStorage = DeviceStorage


__all__ = [
    'DeviceStorage',
    'SerialStorage',      # Backward compatibility
    'create_storage',
]
