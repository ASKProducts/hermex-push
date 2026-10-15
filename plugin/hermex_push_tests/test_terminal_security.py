"""Security and resource bounds against Hermes's real gate, bridge and registry.

These tests skip without Hermes, as the plugin's git integration tests do.
"""
import inspect
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermex_push_tests.test_terminal import (PREFIX, load_api, next_bytes, receive_replay, receive_until,  # noqa: F401
                                             session_db, shell_client)


def test_the_dashboard_gate_rejects_before_accept_and_consumes_one_ticket(shell_client, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.web_routers import chat_ws
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket
    client, api, _ = shell_client
    monkeypatch.setattr(chat_ws, "_ws_gate", api.real_gate)
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    url = f"{PREFIX}/terminal/shell?session_id=chat"
    for suffix in ("", "&ticket=wrong", "&token=wrong"):
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(url + suffix):
                pytest.fail("An unauthenticated upgrade was accepted")
        assert closed.value.code == 4401
        assert api.bridges == []
    ticket = mint_ticket(user_id="terminal-test", provider="basic")
    with client.websocket_connect(url + "&ticket=" + ticket) as ws:
        ws.send_text("printf 'login-%s\\n' ok\n")
        receive_until(ws, b"login-ok")
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(url + "&ticket=" + ticket):
            pytest.fail("A spent ticket was accepted")
    assert closed.value.code == 4401 and len(api.bridges) == 1


def test_status_requires_the_dashboard_http_login(monkeypatch):
    web_server = pytest.importorskip("hermes_cli.web_server")
    api = load_api()
    monkeypatch.setattr(web_server.app.router, "routes", list(web_server.app.router.routes))
    web_server.app.include_router(api.router, prefix=PREFIX)
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    client = TestClient(web_server.app, base_url="http://127.0.0.1")
    assert client.get(f"{PREFIX}/terminal/shell/status?session_id=chat").status_code == 401
    assert api._shells.registry is None


def test_post_accept_failure_still_closes_with_1011(shell_client):
    import asyncio
    from starlette.websockets import WebSocket
    from hermex_push.terminal import Shells
    frames = []
    connected = False

    async def receive():
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        raise OSError("input transport failed")

    async def send(message):
        frames.append(message)

    async def scenario():
        shells = Shells()
        try:
            ws = WebSocket({"type": "websocket", "query_string": b"session_id=chat", "headers": []}, receive, send)
            await shells.connect(ws)
        finally:
            await shells.close()
    asyncio.run(scenario())
    assert any(frame["type"] == "websocket.close" and frame["code"] == 1011 for frame in frames)


def test_validation_accepts_before_sending_its_close_code(shell_client):
    import asyncio
    from starlette.websockets import WebSocket
    _, api, _ = shell_client
    frames = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        frames.append(message)

    ws = WebSocket({"type": "websocket", "query_string": b"session_id=missing", "headers": []}, receive, send)
    asyncio.run(api._shells.connect(ws))
    assert [frame["type"] for frame in frames] == ["websocket.accept", "websocket.close"]
    assert frames[-1]["code"] == 4400
    assert api.bridges == []


def test_missing_gate_fails_closed(shell_client, monkeypatch):
    client, api, _ = shell_client
    monkeypatch.setitem(__import__("sys").modules, "hermes_cli.web_routers.chat_ws", None)
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(f"{PREFIX}/terminal/shell?session_id=chat"):
            pytest.fail("No gate must mean no shell")
    assert closed.value.code == 1011 and api.bridges == []


def test_a_second_viewer_supersedes_the_first_without_a_second_shell(shell_client):
    client, api, _ = shell_client
    url = f"{PREFIX}/terminal/shell?session_id=chat"
    with client.websocket_connect(url) as old:
        with client.websocket_connect(url) as new:
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    next_bytes(old)
            assert closed.value.code == 4409
            new.send_text("printf 'viewer-%s\\n' ok\n")
            receive_until(new, b"viewer-ok")
    assert len(api.bridges) == 1


def test_shell_count_is_bounded_and_oldest_idle_shell_makes_room(shell_client, hermes_home, monkeypatch):
    from hermex_push import terminal
    monkeypatch.setattr(terminal, "MAX_SHELLS", 2)
    client, api, cwd = shell_client
    session_db(hermes_home, "second", cwd)
    session_db(hermes_home, "third", cwd)
    url = f"{PREFIX}/terminal/shell?session_id="
    with client.websocket_connect(url + "chat") as first:
        with client.websocket_connect(url + "second"):
            with pytest.raises(WebSocketDisconnect) as closed:
                with client.websocket_connect(url + "third") as third:
                    next_bytes(third)
            assert closed.value.code == 1013
            assert len(api.bridges) == 2
    with client.websocket_connect(url + "third") as third:
        third.send_text("printf 'slot-%s\\n' ok\n")
        receive_until(third, b"slot-ok")
        assert len(api._shells.registry._sessions) == 2
    assert client.get(f"{PREFIX}/terminal/shell/status?session_id=second").json() == {"running": False}
    # Await shutdown of the registry's remaining children; the evicted child is
    # already being joined by the registry's own scheduled close.
    client.portal.call(api._shells.registry.close_all)
    deadline = time.monotonic() + 5
    while api.bridges[0].is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not api.bridges[0].is_alive()


def test_idle_reaping_keeps_attached_shells_and_closes_detached_shells(shell_client, hermes_home):
    from hermex_push import terminal
    client, api, cwd = shell_client
    session_db(hermes_home, "second", cwd)
    url = f"{PREFIX}/terminal/shell?session_id="
    with client.websocket_connect(url + "chat"):
        with client.websocket_connect(url + "second"):
            pass
        future = time.monotonic() + terminal.IDLE_TTL_S + 1
        client.portal.call(api._shells.registry.reap_idle, future)
        assert api.bridges[0].is_alive()
        assert not api.bridges[1].is_alive()
    assert api._shells.reaper is not None and not api._shells.reaper.done()


def test_replay_memory_is_bounded(shell_client, monkeypatch):
    from hermex_push import terminal
    monkeypatch.setattr(terminal, "BUFFER_BYTES", 1024)
    client, api, _ = shell_client
    url = f"{PREFIX}/terminal/shell?session_id=chat"
    with client.websocket_connect(url) as ws:
        ws.send_text("printf '%4096s' x; printf 'buffer-%s\\n' end\n")
        receive_until(ws, b"buffer-end")
    session = next(iter(api._shells.registry._sessions.values()))
    assert session.buffer.truncated and len(session.buffer.snapshot()) <= 1024
    with client.websocket_connect(url) as ws:
        replay = receive_replay(ws)
        assert len(replay) <= 1024 and b"buffer-end" in replay


def test_profiles_and_chats_have_different_shells_and_default_aliases_share_one(shell_client, hermes_home, monkeypatch):
    from agent.secret_scope import is_multiplex_active, set_multiplex_active
    from hermes_cli import profiles
    from tui_gateway import launch_profile_policy
    client, api, cwd = shell_client
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)
    other = hermes_home / "profiles" / "work"
    other.mkdir(parents=True)
    (other / "profile.yaml").write_text("name: work\n")
    other_cwd = cwd / "work"
    other_cwd.mkdir()
    session_db(other, "chat", other_cwd)
    session_db(hermes_home, "second", cwd)
    url = f"{PREFIX}/terminal/shell?session_id="
    multiplexed = is_multiplex_active()
    try:
        with client.websocket_connect(url + "chat&shell_profile=default") as first:
            first.send_text("export CHAT_MARKER=one; printf 'first-%s\\n' ready\n")
            receive_until(first, b"first-ready")
        with client.websocket_connect(url + "chat") as alias:
            alias.send_text("printf 'alias=%s:done\\n' \"$CHAT_MARKER\"\n")
            receive_until(alias, b"alias=one:done")
            assert len(api.bridges) == 1
        with client.websocket_connect(url + "chat&shell_profile=work") as work:
            work.send_text("pwd; printf 'work=%s:done\\n' \"${CHAT_MARKER-unset}\"\n")
            output = receive_until(work, b"work=unset:done")
            assert str(other_cwd).encode() in output
        with client.websocket_connect(url + "second&shell_profile=default") as second:
            second.send_text("printf 'second=%s:done\\n' \"${CHAT_MARKER-unset}\"\n")
            receive_until(second, b"second=unset:done")
        assert len(api.bridges) == 3
    finally:
        set_multiplex_active(multiplexed)


def test_hermes_pty_entry_points_at_the_tested_pin():
    registry = pytest.importorskip("hermes_cli.pty_session")
    bridge = pytest.importorskip("hermes_cli.pty_bridge")
    gate = pytest.importorskip("hermes_cli.web_routers.chat_ws")._ws_gate
    assert inspect.iscoroutinefunction(gate)
    assert list(inspect.signature(gate).parameters) == ["ws", "kind"]
    assert {"ttl", "max_sessions", "buffer_cap", "read_timeout"} <= set(inspect.signature(registry.PtySessionRegistry).parameters)
    assert "spawn" in inspect.signature(registry.PtySessionRegistry.attach_or_spawn).parameters
    assert {"cwd", "env", "cols", "rows"} <= set(inspect.signature(bridge.PtyBridge.spawn).parameters)
    assert inspect.iscoroutinefunction(registry.run_reaper)
    instance = registry.PtySessionRegistry(ttl=1, max_sessions=1, buffer_cap=1, read_timeout=0.2)
    assert instance._sessions == {}
    session = registry.PtySession("tripwire", None, buffer_cap=1, read_timeout=0.2)
    assert session._ws is None
    session.detach(None)
    assert not session.attached and session.last_detached_at is not None


def test_real_plugin_mount_authenticates_before_profile_resolution(hermes_home, monkeypatch):
    from fastapi import FastAPI
    from hermes_cli import web_server_dashboard as dashboard
    from hermes_cli.web_routers import chat_ws
    from agent import secret_scope
    if not hasattr(dashboard, "_mount_one_plugin_api"):
        pytest.skip("Pin mounts plugin routers directly; covered by the real gate test")
    calls = []
    async def deny(ws, kind):
        calls.append(kind)
        await ws.close(code=4401)
    monkeypatch.setattr(chat_ws, "_ws_gate", deny)
    before = secret_scope.is_multiplex_active()
    app = FastAPI()
    plugin = {"name": "hermex-push", "source": "user", "_dir": str(__import__("pathlib").Path(__file__).resolve().parents[1] / "dashboard"), "_api_file": "plugin_api.py"}
    assert dashboard._mount_one_plugin_api(app, plugin, {"hermex-push"}, set())
    with TestClient(app) as client:
        for profile in ("missing", "work"):
            with pytest.raises(WebSocketDisconnect) as closed:
                with client.websocket_connect(f"{PREFIX}/terminal/shell?session_id=chat&shell_profile={profile}&profile={profile}"):
                    pytest.fail("Unauthenticated socket accepted")
            assert closed.value.code == 4401
    assert calls == ["hermex-shell", "hermex-shell"]
    assert secret_scope.is_multiplex_active() == before


def test_unreadable_chat_database_is_a_clean_error(shell_client, hermes_home):
    client, api, _ = shell_client
    (hermes_home / "state.db").write_bytes(b"not sqlite")
    assert client.get(f"{PREFIX}/terminal/shell/status?session_id=chat").status_code == 409
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(f"{PREFIX}/terminal/shell?session_id=chat") as ws:
            next_bytes(ws)
    assert closed.value.code == 4400
    assert api.bridges == []


def test_status_honours_idle_ttl(shell_client):
    from hermex_push import terminal
    client, api, _ = shell_client
    with client.websocket_connect(f"{PREFIX}/terminal/shell?session_id=chat") as ws:
        receive_replay(ws)
    session = next(iter(api._shells.registry._sessions.values()))
    session.last_detached_at = time.monotonic() - terminal.IDLE_TTL_S - 1
    assert client.get(f"{PREFIX}/terminal/shell/status?session_id=chat").json() == {"running": False}


def test_capabilities_omit_sockets_in_isolated_plugin_hosts(monkeypatch):
    isolation = pytest.importorskip("hermes_cli.plugin_isolation")
    api = load_api()
    monkeypatch.setenv(isolation.HOST_PROCESS_ENV, "1")
    assert "WS /terminal/shell" not in __import__("json").loads(api.get_capabilities().body)["routes"]


def test_startup_query_in_unseen_replay_can_complete(shell_client, monkeypatch):
    import asyncio
    import json
    from pathlib import Path
    from hermex_push import terminal
    from hermex_push_tests.test_terminal import next_frame
    client, api, _ = shell_client
    (Path.home() / ".bashrc").write_text("printf '\\033[6n'; IFS= read -r -d R reply; printf 'startup-answered:%s:done\\n' \"$reply\"\n")
    attach = terminal.Viewer.attach
    async def after_startup(viewer, session):
        async with asyncio.timeout(5):
            while b"\x1b[6n" not in session.buffer.snapshot():
                await asyncio.sleep(0.01)
        return await attach(viewer, session)
    monkeypatch.setattr(terminal.Viewer, "attach", after_startup)
    with client.websocket_connect(f"{PREFIX}/terminal/shell?session_id=chat") as ws:
        kind, text = next_frame(ws)
        assert kind == "text" and json.loads(text)["unseen"] > 0
        assert b"\x1b[6n" in next_bytes(ws)
        ws.send_bytes(b"\x1b[1;1R")
        assert b"startup-answered:" in receive_until(ws, b":done")


def test_native_cancellation_finishes_acquisition_and_keeps_shell_managed(hermes_home, tmp_path, monkeypatch):
    import asyncio
    from hermex_push import terminal
    from hermes_cli.pty_session import PtySessionRegistry
    from hermes_cli.web_routers import chat_ws
    async def scenario():
        shells = terminal.Shells()
        entered, release = asyncio.Event(), asyncio.Event()
        async def gate(ws, kind): return ("test", "test", "test")
        async def resolve(*args): return "chat", str(tmp_path)
        original = PtySessionRegistry.attach_or_spawn
        async def held(registry, *args, **kwargs):
            entered.set()
            await release.wait()
            return await original(registry, *args, **kwargs)
        monkeypatch.setattr(chat_ws, "_ws_gate", gate)
        monkeypatch.setattr(terminal, "resolve_chat", resolve)
        monkeypatch.setattr(PtySessionRegistry, "attach_or_spawn", held)
        class Socket:
            query_params = {}
            async def accept(self): pytest.fail("Cancelled request accepted")
        task = asyncio.create_task(shells.connect(Socket()))
        await entered.wait()
        task.cancel(); release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert len(shells.registry._sessions) == 1
        session = shells.registry._sessions["chat"]
        assert not session.attached and session.last_detached_at is not None
        await shells.close()
        assert not session.bridge.is_alive()
    asyncio.run(scenario())


def test_concurrent_connects_each_replace_the_previous_viewer(tmp_path, monkeypatch):
    import asyncio
    from hermex_push import terminal
    from hermes_cli.web_routers import chat_ws
    from hermex_push_tests.test_terminal_replay import RecordingSocket
    async def scenario():
        shells = terminal.Shells()
        async def gate(ws, kind): return ("test", "test", "test")
        async def resolve(*args): return "chat", str(tmp_path)
        monkeypatch.setattr(chat_ws, "_ws_gate", gate)
        monkeypatch.setattr(terminal, "resolve_chat", resolve)
        monkeypatch.setattr(terminal, "shell_command", lambda: ["/bin/cat"])
        class Socket(RecordingSocket):
            query_params = {}
            def __init__(self):
                super().__init__()
                self.ready = asyncio.Event()
                self.closed = asyncio.Event()
            async def accept(self): pass
            async def receive(self):
                self.ready.set()
                await self.closed.wait()
                return {"type": "websocket.disconnect"}
            async def close(self, code=1000):
                await asyncio.sleep(0)
                await super().close(code)
                self.closed.set()
        sockets = [Socket(), Socket(), Socket()]
        tasks = []
        try:
            tasks.append(asyncio.create_task(shells.connect(sockets[0])))
            await asyncio.wait_for(sockets[0].ready.wait(), 5)
            tasks.extend(asyncio.create_task(shells.connect(ws)) for ws in sockets[1:])
            await asyncio.wait_for(asyncio.gather(*(ws.ready.wait() for ws in sockets[1:])), 5)
            assert sum(("close", 4409) in ws.frames for ws in sockets) == 2
            assert len(shells.registry._sessions) == 1
        finally:
            for ws in sockets: ws.closed.set()
            await asyncio.gather(*tasks)
            await shells.close()
    asyncio.run(scenario())


def test_failed_replay_closes_the_accepted_socket(shell_client, monkeypatch):
    from hermex_push import terminal
    client, api, _ = shell_client
    async def failed(viewer, session): return False
    monkeypatch.setattr(terminal.Viewer, "attach", failed)
    with client.websocket_connect(f"{PREFIX}/terminal/shell?session_id=chat") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            next_bytes(ws)
        assert closed.value.code == 1011
    assert not next(iter(api._shells.registry._sessions.values())).attached
