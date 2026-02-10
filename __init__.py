__all__ = ["__version__", "get_version", "__description__"]
__version__ = "1.1"
__description__ = "Fast, modern MicroPython CLI with REPL, file sync, install, and smart port detection."
__author__ = "PlanX Lab Development Team"

def get_version() -> str:
    try:
        from importlib.metadata import version as _v  # Python 3.10+
        return _v("replx")
    except Exception:
        return __version__
