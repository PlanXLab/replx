"""Installation and registry utilities."""
import os
from typing import Optional, Tuple
from urllib.parse import urlparse

import typer

from replx.utils import device_name_to_path
from . import get_global_context
from .store import StoreManager
from ..agent.client import AgentClient


class InstallHelper:
    """Installation utilities."""
    
    @staticmethod
    def is_url(s: str) -> bool:
        """Check if string is a valid URL."""
        try:
            u = urlparse(s)
            return u.scheme in ("http", "https") and bool(u.netloc) and bool(u.path)
        except Exception:
            return False
    
    @staticmethod
    def resolve_spec(spec: str) -> Tuple[str, str]:
        """Resolve a spec string to scope and rest."""
        if spec == "core":
            return "core", ""
        if spec == "device":
            return "device", ""
        if spec.startswith("core/"):
            return "core", spec[len("core/"):]
        if spec.startswith("device/"):
            return "device", spec[len("device/"):]
        raise typer.BadParameter(
            f"Invalid spec: {spec} (expect 'core[/...]' or 'device[/...]')"
        )
    
    @staticmethod
    def download_raw_file(owner: str, repo: str, ref_: str, path: str, out_path: str) -> str:
        """Download a raw file from GitHub."""
        import urllib.request
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref_}/{path}"
        req = urllib.request.Request(url, headers=StoreManager.gh_headers())
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with urllib.request.urlopen(req) as r, open(out_path, "wb") as f:
            f.write(r.read())
        return out_path
    
    @staticmethod
    def ensure_remote_dir(remote_dir: str):
        """Ensure remote directory exists on device using agent.
        
        Args:
            remote_dir: Virtual path (e.g., 'lib/xxx') - agent will convert to real path
        """
        if not remote_dir:
            return
        client = AgentClient()
        parts = [p for p in remote_dir.replace("\\", "/").strip("/").split("/") if p]
        # Build virtual path - agent's mkdir command will convert to real path
        path = "/"
        for p in parts:
            path = path + p + "/"
            try:
                client.send_command('mkdir', path=path.rstrip('/'))
            except Exception:
                pass  # Directory may already exist
    
    @staticmethod
    def remote_dir_for(scope: str, rel_dir: str) -> str:
        """Get the remote directory for a given scope."""
        _core, _device, _version, _device_root_fs, _device_path = get_global_context()
        
        if scope == "core":
            return "lib/" + (rel_dir + "/" if rel_dir else "")
        else:
            return f"lib/{device_name_to_path(_device)}/" + (rel_dir + "/" if rel_dir else "")
    
    @staticmethod
    def list_local_py_targets(scope: str, rest: str) -> Tuple[str, list]:
        """List local Python and other files for installation."""
        _core, _device, _version, _device_root_fs, _device_path = get_global_context()
        
        if scope == "core":
            base = os.path.join(StoreManager.pkg_root(), "core", _core, "src")
        else:
            base = os.path.join(StoreManager.pkg_root(), "device", device_name_to_path(_device), "src")

        target_path = os.path.join(base, rest)
        if os.path.isfile(target_path):
            rel = os.path.relpath(target_path, base).replace("\\", "/")
            return base, [(target_path, rel)]

        if os.path.isdir(target_path):
            out = []
            for dp, _, fns in os.walk(target_path):
                for fn in fns:
                    ap = os.path.join(dp, fn)
                    rel = os.path.relpath(ap, base).replace("\\", "/")
                    out.append((ap, rel))
            return base, out
        return base, []
    
    @staticmethod
    def local_store_ready_for_full_install(core: str, device: str) -> Tuple[bool, str]:
        """Check if local store is ready for full installation."""
        StoreManager.ensure_home_store()
        meta_path = StoreManager.local_meta_path()
        if not os.path.isfile(meta_path):
            return False, "meta-missing"

        try:
            _ = StoreManager.load_local_meta()
        except Exception:
            return False, "meta-broken"

        req_dirs = [
            os.path.join(StoreManager.pkg_root(), "core", core, "src"),
            os.path.join(StoreManager.pkg_root(), "core", core, "typehints"),
            os.path.join(StoreManager.pkg_root(), "device", device_name_to_path(device), "src"),
            os.path.join(StoreManager.pkg_root(), "device", device_name_to_path(device), "typehints"),
        ]
        missing = [p for p in req_dirs if not os.path.isdir(p)]
        if missing:
            return False, "dirs-missing"

        return True, "ok"


class SearchHelper:
    """Registry search utilities."""
    
    @staticmethod
    def fmt_ver_with_star(remote_ver: float, local_ver: float, missing_local: bool) -> str:
        """Format version with star if update available."""
        star = "*" if missing_local or (remote_ver > (local_ver or 0.0)) else ""
        return f"{remote_ver:.1f}{star}"
    
    @staticmethod
    def key_ci(items: set | dict, name: str) -> Optional[str]:
        """Case-insensitive key lookup in set or dict."""
        if not name:
            return None
        
        # Handle set (new structure)
        if isinstance(items, set):
            if name in items:
                return name
            n = name.lower()
            for k in items:
                if isinstance(k, str) and k.lower() == n:
                    return k
            return None
        
        # Handle dict (old structure compatibility)
        if isinstance(items, dict):
            if name in items:
                return name
            n = name.lower()
            for k in items.keys():
                if isinstance(k, str) and k.lower() == n:
                    return k
            return None
        
        return None


class RegistryHelper:
    """Helper class for managing registry metadata operations."""
    
    @staticmethod
    def root_sections(reg: dict):
        """Return (cores_dict, devices_dict) from registry.
        
        New registry structure (schema_version >= 5):
        - Uses 'packages' dictionary with package metadata
        - platform_cores contains core names
        - device_configs contains device names
        
        Returns: (set of core names, set of device names)
        """
        # New structure: extract from platform_cores and device_configs
        platform_cores = reg.get("platform_cores", {})
        device_configs = reg.get("device_configs", {})
        
        # Get core names from platform_cores keys
        cores = set(platform_cores.keys()) if platform_cores else set()
        
        # Get device names from device_configs keys
        devices = set(device_configs.keys()) if device_configs else set()
        
        return cores, devices

    @staticmethod
    def get_packages_for_platform(reg: dict, platform: str) -> list[str]:
        """Get list of package patterns included for a platform core.
        
        Returns list of patterns like: ["_std/*", "_std_peri/*", "_std_comm/*"]
        """
        platform_cores = reg.get("platform_cores", {})
        core_info = platform_cores.get(platform, {})
        return core_info.get("includes", [])
    
    @staticmethod
    def get_packages_for_device(reg: dict, device: str) -> list[str]:
        """Get list of package patterns included for a device config.
        
        Returns list of patterns like: ["_std/display/lcd_hd44780", "_ticle/ain", ...]
        """
        device_configs = reg.get("device_configs", {})
        device_info = device_configs.get(device, {})
        return device_info.get("includes", [])
    
    @staticmethod
    def match_pattern(pkg_name: str, pattern: str) -> bool:
        """Check if package name matches a pattern.
        
        Pattern examples:
        - "_std/*" matches any package with source starting with "core/_std/"
        - "_std/display/lcd_hd44780" matches specific package
        - "ws2812@_ticle" matches specific variant
        """
        if "*" in pattern:
            # Wildcard pattern
            prefix = pattern.rstrip("/*")
            return pkg_name.startswith(prefix)
        else:
            # Exact match - remove file extension for comparison
            pkg_base = pkg_name
            for ext in (".py", ".pyi", ".json"):
                if pkg_base.endswith(ext):
                    pkg_base = pkg_base[:-len(ext)]
                    break
            return pkg_base == pattern or pkg_name == pattern
    
    @staticmethod
    def get_packages_matching(reg: dict, patterns: list[str], package_type: str = None) -> dict:
        """Get all packages matching the given patterns.
        
        Args:
            reg: Registry dictionary
            patterns: List of patterns to match (e.g., ["_std/*", "_std_peri/*"])
            package_type: Optional filter by type ("core", "device-std", "device-specific")
            
        Returns:
            Dictionary of {package_name: package_metadata}
        """
        packages = reg.get("packages", {})
        result = {}
        
        for pkg_name, pkg_meta in packages.items():
            # Check type filter
            if package_type:
                pkg_type = pkg_meta.get("type", "")
                if package_type == "core" and pkg_type != "core":
                    continue
                elif package_type == "device" and not pkg_type.startswith("device-"):
                    continue
            
            # Check pattern match
            source = pkg_meta.get("source", "")
            for pattern in patterns:
                if RegistryHelper.match_pattern(source, pattern):
                    result[pkg_name] = pkg_meta
                    break
        
        return result
    
    @staticmethod
    def get_version(pkg_meta: dict) -> float:
        """Extract version from package metadata."""
        v = pkg_meta.get("version", "0.0")
        try:
            # Handle semantic versioning (e.g., "1.0.0" -> 1.0)
            if isinstance(v, str) and "." in v:
                parts = v.split(".")
                major = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
                minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                return float(f"{major}.{minor}")
            return float(v)
        except Exception:
            return 0.0
    
    @staticmethod
    def effective_version(reg: dict, scope: str, target: str, part: str, relpath: str) -> float:
        """Get version for a specific file path.
        
        In new structure, looks up package by source path and returns its version.
        """
        packages = reg.get("packages", {})
        
        # Construct expected source path
        if scope == "core":
            # For core: source is like "core/_std/src/slip.py"
            source_path = f"core/{target}/{part}/{relpath}"
        else:
            # For device: source is like "device/_std/src/xxx.py" or "device/_ticle/src/xxx.py"
            source_path = f"device/{target}/{part}/{relpath}"
        
        # Find matching package
        for pkg_name, pkg_meta in packages.items():
            pkg_source = pkg_meta.get("source", "")
            if pkg_source == source_path:
                return RegistryHelper.get_version(pkg_meta)
        
        # If not found, return 0.0
        return 0.0
    
    @staticmethod
    def walk_files_for_core(reg: dict, core_name: str, part: str = "src"):
        """Walk all files for a core platform.
        
        Args:
            reg: Registry dictionary
            core_name: Core platform name (e.g., "RP2350", "ESP32S3")
            part: "src" or "typehints"
            
        Yields:
            (relpath, package_metadata) tuples
        """
        packages = reg.get("packages", {})
        patterns = RegistryHelper.get_packages_for_platform(reg, core_name)
        
        for pkg_name, pkg_meta in packages.items():
            # Only include core packages
            if pkg_meta.get("type") != "core":
                continue
            
            source = pkg_meta.get("source", "")
            
            # Check if this package matches platform patterns
            match_found = False
            for pattern in patterns:
                # Extract the core module directory from pattern (e.g., "_std" from "_std/*")
                # Pattern examples: "_std/*", "_std_peri/*", "_std_comm/*"
                if "*" in pattern:
                    # Wildcard: check if source contains this pattern prefix
                    pattern_prefix = pattern.rstrip("/*")
                    # For core: source like "core/_std/src/slip.py" should match pattern "_std/*"
                    if f"/{pattern_prefix}/" in source or source.endswith(f"/{pattern_prefix}"):
                        match_found = True
                        break
                else:
                    # Exact match pattern
                    if RegistryHelper.match_pattern(source, pattern):
                        match_found = True
                        break
            
            if not match_found:
                continue
            
            # Get appropriate path based on part
            if part == "src":
                file_path = source
            elif part == "typehints":
                file_path = pkg_meta.get("typehint", "")
                if not file_path:
                    continue
            else:
                continue
            
            # Extract relative path from full path
            # e.g., "core/_std/src/slip.py" -> "slip.py"
            path_parts = file_path.split("/")
            if len(path_parts) >= 3 and path_parts[2] == part:
                relpath = "/".join(path_parts[3:])
                yield (relpath, pkg_meta)
    
    @staticmethod
    def walk_files_for_device(reg: dict, device_name: str, part: str = "src"):
        """Walk all files for a device configuration.
        
        Args:
            reg: Registry dictionary
            device_name: Device config name (e.g., "ticle-lite", "ticle-sensor")
            part: "src" or "typehints"
            
        Yields:
            (relpath, package_metadata) tuples
        """
        packages = reg.get("packages", {})
        patterns = RegistryHelper.get_packages_for_device(reg, device_name)
        
        for pkg_name, pkg_meta in packages.items():
            # Check variants first (device-specific variants of core packages)
            variants = pkg_meta.get("variants", {})
            for variant_name, variant_meta in variants.items():
                variant_deploy = variant_meta.get("deploy_path", "")
                
                # Check if variant matches patterns
                # Pattern format: "_ticle/ain" should match variant_name="_ticle" with deploy_path="ain.py"
                match_found = False
                for pattern in patterns:
                    # Construct full variant path: variant_name/module_name
                    # e.g., "_ticle" + "/" + "ain" from "ain.py"
                    module_name = variant_deploy.replace("/__init__.py", "").replace(".py", "")
                    variant_full_path = f"{variant_name}/{module_name}"
                    
                    if RegistryHelper.match_pattern(variant_full_path, pattern):
                        match_found = True
                        break
                    if "/" in pattern and variant_full_path.startswith(pattern + "/"):
                        match_found = True
                        break
                
                if not match_found:
                    continue
                
                # Get appropriate path based on part
                if part == "src":
                    file_path = variant_meta.get("source", "")
                elif part == "typehints":
                    file_path = variant_meta.get("typehint", "")
                    # Ensure it's a .pyi file
                    if not file_path or not file_path.endswith(".pyi"):
                        continue
                else:
                    continue
                
                if not file_path:
                    continue
                
                # Extract module name from deploy_path
                # For typehints, change extension from .py to .pyi
                if part == "typehints":
                    relpath = variant_deploy.replace(".py", ".pyi")
                else:
                    relpath = variant_deploy
                # Merge variant metadata with base package metadata
                merged_meta = pkg_meta.copy()
                merged_meta.update(variant_meta)
                yield (relpath, merged_meta)
            
            # Then check device-std and device-specific packages
            pkg_type = pkg_meta.get("type", "")
            if not pkg_type.startswith("device-"):
                continue
            
            source = pkg_meta.get("source", "")
            deploy_path = pkg_meta.get("deploy_path", "")
            
            # Check if this package matches device patterns
            # Match against both source (full path with category) and deploy_path
            match_found = False
            for pattern in patterns:
                # For device-specific packages (e.g., ws2812@_ticle), extract device name from pkg_name
                # e.g., "ws2812@_ticle" -> device="_ticle", module="ws2812"
                if "@" in pkg_name:
                    parts = pkg_name.split("@")
                    module_name = parts[0]
                    device_prefix = parts[1]
                    # Pattern: "_ticle/ws2812" should match device="_ticle", module="ws2812"
                    variant_full_path = f"{device_prefix}/{module_name}"
                    if RegistryHelper.match_pattern(variant_full_path, pattern):
                        match_found = True
                        break
                
                # Extract category/module path from source for pattern matching
                # e.g., "device/_std/src/input/button.py" -> "_std/input/button"
                if source.startswith("device/"):
                    # Remove "device/" prefix and "/src/" or "/typehints/" part
                    source_parts = source.split("/")
                    if len(source_parts) >= 4:  # device/category/src_or_typehints/...
                        # Reconstruct path: category/path/to/file
                        category = source_parts[1]  # e.g., "_std", "_ticle"
                        file_parts = source_parts[3:]  # e.g., ["input", "button.py"] or ["display", "ws2812", "__init__.py"]
                        
                        # Build source pattern path from category and file structure
                        # e.g., "device/_ticle/src/display/ws2812/__init__.py" -> "_ticle/display/ws2812"
                        # e.g., "device/_std/src/input/button.py" -> "_std/input/button"
                        file_path = "/".join(file_parts)
                        # Remove extension and __init__.py suffix
                        if file_path.endswith("/__init__.py"):
                            file_path = file_path[:-12]  # Remove "/__init__.py"
                        elif file_path.endswith(".py") or file_path.endswith(".pyi"):
                            file_path = file_path[:-3]  # Remove extension
                        
                        source_pattern_path = f"{category}/{file_path}"
                        
                        # Try pattern matching with source path
                        if RegistryHelper.match_pattern(source_pattern_path, pattern):
                            match_found = True
                            break
                
                # Also try deploy_path matching (fallback)
                if RegistryHelper.match_pattern(deploy_path, pattern):
                    match_found = True
                    break
                # Handle directory-level patterns
                if "/" in pattern and deploy_path.startswith(pattern + "/"):
                    match_found = True
                    break
            
            if not match_found:
                continue
            
            # Get appropriate path based on part
            if part == "src":
                file_path = source
            elif part == "typehints":
                file_path = pkg_meta.get("typehint", "")
                # Ensure it's a .pyi file
                if not file_path or not file_path.endswith(".pyi"):
                    continue
            else:
                continue
            
            # Extract module name from deploy_path
            # For typehints, change extension from .py to .pyi
            # e.g., "button.py" -> "button.pyi", "ws2812/__init__.py" -> "ws2812/__init__.pyi"
            if part == "typehints":
                relpath = deploy_path.replace(".py", ".pyi")
            else:
                relpath = deploy_path
            yield (relpath, pkg_meta)
