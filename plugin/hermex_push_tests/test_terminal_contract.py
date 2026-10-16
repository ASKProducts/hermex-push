"""Contract with the Hermes internals the plugin relies on (none of them is a plugin API).

A Hermes update that renames or reshapes any of these fails here, loudly; the shell route itself
fails closed (1011) when the login check is missing.
"""

from __future__ import annotations

import inspect

import pytest
from hermes_cli.pty_session import PtySessionRegistry

from terminal_support import service, shell


def _params(fn):
    return inspect.signature(fn).parameters


def _kinds(fn):
    return [(p.name, p.kind) for p in _params(fn).values()]


P, K = inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY


def test_hermes_internals_contract():
    # Login gate used by /api/pty.
    from hermes_cli.web_routers import chat_ws
    assert inspect.iscoroutinefunction(chat_ws._ws_gate)
    assert _kinds(chat_ws._ws_gate) == [("ws", P), ("kind", P)]
    assert service.load_ws_gate() is chat_ws._ws_gate

    # Keep-alive registry.
    from hermes_cli import pty_session
    from hermes_cli.pty_session import PtySession, PtySessionRegistry, RegistryFull, run_reaper
    assert _kinds(PtySessionRegistry.__init__) == [
        ("self", P), ("ttl", K), ("max_sessions", K), ("buffer_cap", K), ("read_timeout", K)]
    assert inspect.iscoroutinefunction(PtySessionRegistry.attach_or_spawn)
    # Hermes 0.22 added an optional keyword-only ``active_session_file`` (cleanup on close); we
    # don't pass it, so it must stay optional.
    spawn_params = _params(PtySessionRegistry.attach_or_spawn)
    assert _kinds(PtySessionRegistry.attach_or_spawn)[:3] == [("self", P), ("key", P), ("spawn", K)]
    assert all(p.kind == K and p.default is not inspect.Parameter.empty
               for p in list(spawn_params.values())[3:])
    assert not inspect.iscoroutinefunction(PtySessionRegistry.detach)
    assert _kinds(PtySessionRegistry.detach) == [("self", P), ("key", P), ("ws", P)]
    assert inspect.iscoroutinefunction(PtySessionRegistry.reap_idle)
    assert inspect.iscoroutinefunction(PtySessionRegistry.close_all)
    assert _kinds(PtySessionRegistry.close_all) == [("self", P)]
    assert inspect.iscoroutinefunction(PtySession.attach)
    assert _kinds(PtySession.attach) == [("self", P), ("ws", P), ("force_redraw", K)]
    assert inspect.iscoroutinefunction(PtySession.write)
    assert _kinds(PtySession.write) == [("self", P), ("ws", P), ("data", P)]
    assert inspect.iscoroutinefunction(run_reaper)
    assert list(_params(run_reaper))[0] == "registry"
    assert issubclass(RegistryFull, Exception)
    assert pty_session.WS_CLOSE_PROCESS_EXITED == 4410

    # PTY bridge.
    from hermes_cli.pty_bridge import PtyBridge, PtyUnavailableError
    assert inspect.ismethod(PtyBridge.spawn)  # classmethod bound to the class
    assert _kinds(PtyBridge.spawn) == [("argv", P), ("cwd", K), ("env", K), ("cols", K), ("rows", K)]
    assert _kinds(PtyBridge.resize) == [("self", P), ("cols", P), ("rows", P)]
    assert callable(PtyBridge.is_alive) and isinstance(PtyBridge.pid, property)
    assert issubclass(PtyUnavailableError, Exception)

    # Folder: Hermes's default working folder (GET /api/fs/default-cwd) and profile homes.
    from hermes_cli.web_routers import files
    assert _kinds(files._fs_default_cwd) == []
    from hermes_cli import profiles
    for name in ("normalize_profile_name", "validate_profile_name", "profile_exists", "get_profile_dir"):
        assert callable(getattr(profiles, name)), name
    from hermes_constants import get_hermes_home
    assert callable(get_hermes_home)

    # Resize framing: the plugin's own copy matches /api/pty's.
    from hermes_cli.web_server_chat import PTY_REGISTRY, _RESIZE_RE
    assert shell.RESIZE_RE.pattern == _RESIZE_RE.pattern

    # Registry settings: the same as Hermes's /api/pty registry.
    mine = service.TerminalService().registry
    for attr in ("_ttl", "_max", "_buffer_cap", "_read_timeout"):
        assert getattr(mine, attr) == getattr(PTY_REGISTRY, attr), attr
    assert (service.REGISTRY_TTL, service.REGISTRY_MAX_SESSIONS, service.REGISTRY_BUFFER_CAP,
            service.REGISTRY_READ_TIMEOUT) == (30 * 60, 16, 1024 * 1024, 0.2)


@pytest.mark.skipif(not callable(getattr(PtySessionRegistry, "close_other_sessions", None)),
                    reason="Restart needs Hermes 0.22 or newer")
def test_restart_api_contract():
    assert inspect.iscoroutinefunction(PtySessionRegistry.close_other_sessions)
    assert _kinds(PtySessionRegistry.close_other_sessions) == [("self", P), ("prefix", P), ("keep_key", K)]
