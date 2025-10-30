"""
MicroPython Device File System management.
Provides high-level file operations for connected devices.
"""
import os
import sys
import ast
import json
import textwrap
import posixpath
from typing import Tuple

from .exceptions import ReplxError


class DeviceFileSystem:
    """
    Manages MicroPython device file system operations.
    This class provides high-level methods for file and directory management on connected devices.
    """
    
    _PUT_BATCH_BYTES = 16 * 1024
    _DEVICE_CHUNK_SIZES = 4096
    
    def __init__(self, repl_protocol, core: str = "RP2350", device_root_fs: str = "/"):
        """
        Initialize the file system manager.
        :param repl_protocol: The REPL protocol handler for communication.
        :param core: The core type of the device (e.g., "RP2350", "ESP32", "EFR32MG").
        :param device_root_fs: The root filesystem path on the device.
        """
        self.repl = repl_protocol
        self.core = core
        self.device_root_fs = device_root_fs
    
    def _normalize_remote_path(self, path: str) -> str:
        """
        Normalize a remote path to ensure it starts with device_root_fs.
        :param path: The path to normalize.
        :return: Normalized path.
        """
        if not path.startswith(self.device_root_fs):
            if path.startswith("/"):
                path = path[1:]
            return posixpath.join(self.device_root_fs, path)
        return path
    
    def _print_progress_bar(self, current: int, total: int, bar_length: int = 40):
        """
        Print a progress bar.
        :param current: Current progress value.
        :param total: Total value.
        :param bar_length: Length of the progress bar.
        """
        pct = 0 if total == 0 else min(1.0, current / total)
        block = min(bar_length, int(round(bar_length * pct)))
        bar = "#" * block + "-" * (bar_length - block)
        percent = int(pct * 100)
        print(f"\r[{bar}] {percent}% ({current}/{total})", end="", flush=True)
    
    def get(self, remote: str, local: str):
        """
        Download a file from the connected device to the local filesystem.
        :param remote: The path to the file on the device.
        :param local: The local path where the file should be saved.
        :raises ReplxError: If the file is empty or if the download fails.
        """
        local_file = None
        
        if local:
            if os.path.isdir(local):
                local = os.path.join(local, os.path.basename(remote))
            local_file = open(local, "wb")
        
        bytes_read = 0
        bar_length = 40

        # Don't print filename - handled by caller

        try:
            file_size = self.state(remote)

            self.repl._enter_repl()

            init_command = f"""
                import sys
                f = open('{remote}', 'rb')
                """
            self.repl._exec(textwrap.dedent(init_command))
                            
            while bytes_read < file_size:
                remaining = min(self._DEVICE_CHUNK_SIZES, file_size - bytes_read)

                read_cmd = f"""
                    chunk = f.read({remaining})
                    if chunk:
                        sys.stdout.buffer.write(chunk)
                    """
                chunk_data = self.repl._exec(textwrap.dedent(read_cmd))
                
                if chunk_data:
                    if local_file:
                        local_file.write(chunk_data)
                    else:
                        sys.stdout.buffer.write(chunk_data)
                        sys.stdout.flush()
                
                    bytes_read += len(chunk_data)
                    # Don't print progress bar - handled by caller
                else:
                    break

            self.repl._exec("f.close()")
        
        except Exception as e:
            raise ReplxError(f"Download failed: {e}")
        finally:
            self.repl._leave_repl()
            if local_file:
                local_file.close()

        if bytes_read != file_size:
            raise ReplxError(f"Download incomplete: got {bytes_read}/{file_size} bytes")
    
    def state(self, path: str) -> int:
        """
        Return file size of given path.
        """
        if self.core == "EFR32MG":
            command = f"""
                try:
                    with open('{path}', 'rb') as f:
                        f.seek(0, 2)
                        size = f.tell()
                    print(size)
                except Exception as e:
                    print(0)
            """
            out = self.repl.exec(command)
            return int(out.decode('utf-8'))
        else:
            command = f"""
                import os
                try:
                    st = os.stat('{path}')
                    print(st[6])
                except:
                    print(0)
            """
        out = self.repl.exec(command)
        return int(out.decode('utf-8'))
    
    def is_dir(self, path: str) -> bool:
        """
        Check if the given path is a directory.
        :param path: The path to check.
        :return: True if the path is a directory, False otherwise.
        """
        command = f"""
            vstat = None
            try:
                from os import stat
            except ImportError:
                from os import listdir
                vstat = listdir
            def ls_dir(path):
                if vstat is None:
                    return stat(path)[0] & 0x4000 != 0
                else:
                    try:
                        vstat(path)
                        return True
                    except OSError as e:
                        return False
            print(ls_dir('{path}'))
        """
        out = self.repl.exec(command)
        return ast.literal_eval(out.decode("utf-8"))
    
    def ls_detailed(self, dir: str = "/") -> list:
        """
        List the contents of a directory with detailed information (size, type) in a single operation.
        :param dir: The directory to list. Defaults to the root directory ("/").
        :return: A list of tuples containing (name, size, is_dir) for each item.
        """
        if not dir.startswith("/"):
            dir = "/" + dir

        command = f"""
            import os
            import json
            import sys
            def xbee3_zigbee_state(path):
                try:
                    with open(path, 'rb') as f:
                        f.seek(0, 2)
                        size = f.tell()
                    return size
                except Exception as e:
                    return 0

            def get_detailed_listing(path):
                try:
                    items = []
                    for item in os.listdir(path):
                        full_path = path + ('/' + item if path != '/' else item)
                        if sys.platform == 'xbee3-zigbee':
                            is_dir = False
                            size = xbee3_zigbee_state(full_path)
                            if size == 0:
                                is_dir = True
                            items.append([item, size, is_dir])
                            continue
                        try:
                            stat_info = os.stat(full_path)
                            is_dir = stat_info[0] & 0x4000 != 0
                            size = 0 if is_dir else stat_info[6]
                            items.append([item, size, is_dir])
                        except:
                            # If stat fails, try to determine if it's a directory
                            try:
                                os.listdir(full_path)
                                items.append([item, 0, True])  # It's a directory
                            except:
                                items.append([item, 0, False])  # It's a file
                    return sorted(items, key=lambda x: (not x[2], x[0].lower()))
                except Exception as e:
                    return []

            print(json.dumps(get_detailed_listing('{dir}')))
        """

        try:
            out = self.repl.exec(command)
            result = json.loads(out.decode("utf-8").strip())
            return result
        except (json.JSONDecodeError, ReplxError):
            return self._ls_fallback(dir)
    
    def _ls_fallback(self, dir: str = "/") -> list:
        """
        Fallback method for listing directory contents (original implementation).
        """
        if not dir.startswith("/"):
            dir = "/" + dir
            
        command = f"""
            import os
            def listdir(dir):
                if dir == '/':                
                    return sorted([dir + f for f in os.listdir(dir)])
                else:
                    return sorted([dir + '/' + f for f in os.listdir(dir)])
            print(listdir('{dir}'))
        """
        out = self.repl.exec(command)
        file_list = ast.literal_eval(out.decode("utf-8"))
        
        # Convert to detailed format for compatibility
        result = []
        for f in file_list:
            f_name = f.split("/")[-1]
            is_dir = self.is_dir(f)
            size = 0 if is_dir else self.state(f)
            result.append([f_name, size, is_dir])
        
        return result
    
    def mem(self) -> Tuple[int, int, int, float]:
        """
        Get the memory usage of the connected device.
        :return: A tuple containing (free_memory, alloc_memory, total_memory, usage_pct) in bytes.
        """
        command = f"""
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
    
    def mkdir(self, dir: str) -> bool:
        """
        Create a directory on the connected device.
        :param dir: The directory to create.
        :return: True if the directory was created, False if it already exists.
        """
        command = f"""
            import os
            def mkdir(dir):
                parts = dir.split(os.sep)
                dirs = [os.sep.join(parts[:i+1]) for i in range(len(parts))]
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
        return ast.literal_eval(out.decode("utf-8"))
    
    def putdir(self, local: str, remote: str):
        """
        Upload a directory and its contents to the connected device.
        :param local: The local directory to upload.
        :param remote: The remote directory path on the device.
        """
        base_local = os.path.abspath(local)
        base_remote = remote.replace("\\", "/")
        for parent, child_dirs, child_files in os.walk(base_local, followlinks=True):
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
                remote_path = posixpath.join(remote_parent, filename).replace("\\", "/")
                self.put(local_path, remote_path)
    
    def put(self, local: str, remote: str):
        """
        Upload a file to the connected device.
        :param local: The local file path to upload.
        :param remote: The remote file path on the device.
        :raises ReplxError: If the upload fails or if the file already exists.
        """
        sent = 0
        bar_length = 40

        self.repl._enter_repl()
        try:
            self.repl._exec(f"f = open('{remote}', 'wb')")
        except ReplxError as e:
            if "EEXIST" in str(e):
                self.repl._leave_repl()
                self.rm(remote)
                self.put(local, remote)
                return

        try:
            with open(local, "rb") as f:
                total = os.fstat(f.fileno()).st_size
                # Don't print filename and progress bar - handled by caller

                batch_src_lines = []
                batch_bytes = 0
                DEVICE_CHUNK = self._DEVICE_CHUNK_SIZES
                BATCH_LIMIT = max(8 * 1024, int(self._PUT_BATCH_BYTES))

                def _flush_batch():
                    nonlocal batch_src_lines, batch_bytes, sent
                    if not batch_src_lines:
                        return

                    code = ";\n".join(batch_src_lines)
                    self.repl._exec(code)
                    batch_src_lines = []
                    batch_bytes = 0

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

                    # Don't print progress bar - handled by caller

                self.repl._exec("f.close()")
        finally:
            self.repl._leave_repl()
    
    def putdir_batch(self, local: str, remote: str, progress_callback=None):
        """
        Upload a directory and its contents using optimized batch mode.
        This method reduces REPL overhead by uploading all files in a single session.
        
        :param local: The local directory to upload.
        :param remote: The remote directory path on the device.
        :param progress_callback: Optional callback(done, total, filename) for progress tracking
        :raises ReplxError: If any file upload fails
        """
        base_local = os.path.abspath(local)
        base_remote = remote.replace("\\", "/")
        
        # Collect all files to upload
        file_specs = []
        for parent, child_dirs, child_files in os.walk(base_local, followlinks=True):
            rel = os.path.relpath(parent, base_local).replace("\\", "/")
            remote_parent = posixpath.normpath(
                posixpath.join(base_remote, "" if rel == "." else rel)
            )
            
            # Create remote directories (before batch upload starts)
            try:
                self.mkdir(remote_parent)
            except Exception:
                pass
            
            for filename in child_files:
                local_path = os.path.join(parent, filename)
                remote_path = posixpath.join(remote_parent, filename).replace("\\", "/")
                file_specs.append((local_path, remote_path))
        
        if not file_specs:
            return
        
        # Use batch mode for upload
        self.repl.put_files_batch(file_specs, progress_callback)
    
    def rm(self, filename: str):
        """
        Remove a file from the connected device.
        :param filename: The file to remove.
        """
        command = f"""
            import os
            os.remove('{filename}')
        """
        self.repl.exec(command)
    
    def rmdir(self, dir: str):
        """
        Remove a directory and all its contents recursively.
        :param dir: The directory to remove.
        """
        if self.core == "EFR32MG":
            command = f"""
                import os
                def rmdir(dir):
                    os.chdir(dir)
                    for f in os.listdir():
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                    for f in os.listdir():
                        rmdir(f)
                    os.chdir('..')
                    os.rmdir(dir)
                rmdir('{dir}')
            """
        else:
            command = f"""
                import os
                def rmdir(p):
                    for name in os.listdir(p):
                        fp = p + '/' + name if p != '/' else '/' + name
                        try:
                            if os.stat(fp)[0] & 0x4000:  # 디렉터리
                                rmdir(fp)
                            else:
                                os.remove(fp)
                        except OSError:
                            try:
                                rmdir(fp)
                            except:
                                pass
                    os.rmdir(p)
                rmdir('{dir}')
            """
        self.repl.exec(command)
    
    def format(self) -> bool:
        """
        Format the filesystem of the connected device based on its core type.
        :return: True if the filesystem was successfully formatted, False otherwise.
        """
        if self.core == "ESP32": 
            command = """
                import os 
                os.fsformat('/flash') 
            """ 
        elif self.core in ("ESP32S3", "ESP32C6"): 
            command = """
                import os 
                from flashbdev import bdev 
                os.umount('/') 
                os.VfsLfs2.mkfs(bdev) 
                os.mount(bdev, '/') 
            """ 
        elif self.core == "EFR32MG": 
            command = """
                import os 
                os.format() 
            """
        elif self.core == "RP2350":
            command = """
                import os, rp2 
                bdev = rp2.Flash() 
                os.VfsFat.mkfs(bdev) 
                os.mount(bdev, '/') 
            """ 
        else: 
            return False 
        
        try: 
            self.repl.exec(command) 
        except ReplxError: 
            return False 
        return True
    
    def df(self):
        """
        Get filesystem information including total, used, free space and usage percentage.
        :return: A tuple containing total space, used space, free space, and usage percentage.
        """
        command = f"""
            import os
            import json
            def get_fs_info(path='/'):
                stats = os.statvfs(path)
                block_size = stats[0]
                total_blocks = stats[2]
                free_blocks = stats[3]

                total = block_size * total_blocks
                free = block_size * free_blocks
                used = total - free
                usage_pct = round(used / total * 100, 2)
                
                return total, used, free, usage_pct
            print(get_fs_info())
        """
        out = self.repl.exec(command)
        return ast.literal_eval(out.decode("utf-8"))
