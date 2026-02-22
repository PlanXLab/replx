"""MPY compilation utilities with intelligent caching."""
import os
import hashlib
import time

import typer

from . import get_global_context
from .store import StoreManager
from replx.utils.exceptions import CompilationError, ValidationError


class CompilerHelper:
    _compile_cache = {}
    
    @staticmethod
    def mpy_arch_tag() -> str:
        _core, _device, _version, _device_root_fs, _device_path = get_global_context()
        if not _core:
            return "unknown"
        if "/" in _core:
            return _core.split("/", 1)[0]
        return _core
    
    @staticmethod
    def staging_out_for(abs_py: str, base: str, arch_tag: str) -> str:
        rel = os.path.relpath(abs_py, base).replace("\\", "/")
        rel_mpy = os.path.splitext(rel)[0] + ".mpy"
        out_path = StoreManager.HOME_STAGING / arch_tag / rel_mpy
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return str(out_path)
    
    @staticmethod
    def _compute_file_hash(filepath: str) -> str:
        hash_md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    @staticmethod
    def _march_for_core(core: str, version: str) -> list[str]:
        if not core:
            raise typer.BadParameter("The core is unknown")

        if "/" in core:
            core = core.split("/", 1)[0]

        args: list[str] = ['-msmall-int-bits=31']

        if core == "EFR32MG":
            try:
                ver_parts = version.split('.')
                ver_float = float(f"{ver_parts[0]}.{ver_parts[1]}" if len(ver_parts) >= 2 else version)
            except (ValueError, IndexError):
                ver_float = 0.0
            if ver_float < 1.19:
                args.append('-mno-unicode')
            return args

        if core in ("ESP32", "ESP32S2"):
            args.append('-march=xtensa')
            return args

        if core == "ESP32S3":
            args.append('-march=xtensawin')
            return args

        if core.startswith("ESP32C"):
            args.append('-march=rv32imc')
            return args

        if core.startswith("ESP32P"):
            args.append('-march=rv32imc')
            return args

        if core == "RP2350":
            args.append('-march=armv7emsp')
            return args

        raise typer.BadParameter(f"The {core} is not supported")
    
    @staticmethod
    def compile_to_staging(abs_py: str, base: str) -> str:
        _core, _device, _version, _device_root_fs, _device_path = get_global_context()
        
        if not os.path.exists(abs_py):
            raise ValidationError(f"Source file not found: {abs_py}")
        
        arch_tag = CompilerHelper.mpy_arch_tag()
        out_mpy = CompilerHelper.staging_out_for(abs_py, base, arch_tag)
        
        cache_key = (abs_py, arch_tag)
        current_hash = CompilerHelper._compute_file_hash(abs_py)
        
        if cache_key in CompilerHelper._compile_cache:
            cached_hash, cached_out = CompilerHelper._compile_cache[cache_key]
            if cached_hash == current_hash and os.path.exists(cached_out) and os.path.getsize(cached_out) > 0:
                return cached_out
        
        args = ['_filepath_', '-o', '_outpath_']
        args.extend(CompilerHelper._march_for_core(_core, _version))
        
        out_dir = os.path.dirname(out_mpy)
        os.makedirs(out_dir, exist_ok=True)
        
        args[0] = abs_py
        args[2] = out_mpy
        
        try:
            import mpy_cross
            mpy_cross.run(*args)
        except Exception as e:
            raise CompilationError(f"MPY compilation failed for {abs_py}: {e}")
        
        for _ in range(10):  # Try for 1 second
            if os.path.exists(out_mpy) and os.path.getsize(out_mpy) > 0:
                CompilerHelper._compile_cache[cache_key] = (current_hash, out_mpy)
                return out_mpy
            time.sleep(0.1)
        
        raise CompilationError(f"Compilation failed: {out_mpy} not found or empty")
