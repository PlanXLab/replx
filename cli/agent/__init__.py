
from .protocol import AgentProtocol
from .client import AgentClient, get_session_id, get_cached_session_id, clear_session_cache

__all__ = [
    'AgentProtocol',
    'AgentClient',
    'get_session_id',
    'get_cached_session_id',
    'clear_session_cache',
    'AgentServer',
    'agent_main',
]


def __getattr__(name):
    # The server stack pulls in ``replx.terminal`` (~150ms) plus all command
    # handlers; CLI clients never need it. Defer until something actually asks.
    if name == 'AgentServer':
        from .server import AgentServer

        return AgentServer
    if name == 'agent_main':
        from .server import main as agent_main

        return agent_main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
