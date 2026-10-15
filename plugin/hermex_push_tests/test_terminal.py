"""A plain shell per chat: route discovery, login, isolation and keep-alive lifetime."""
import importlib.util
import json
import sys
from pathlib import Path
import sqlite3

import pytest
from starlette.websockets import WebSocketDisconnect

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermex_push.terminal import REPLAY_END, REPLAY_START

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/plugins/hermex-push"


def load_api():
    spec = importlib.util.spec_from_file_location("hermex_push_terminal_under_test", ROOT / "dashboard" / "plugin_api.py")
    assert spec is not None and spec.loader is not None
    api = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = api
    spec.loader.exec_module(api)
    return api


def test_capabilities_lists_the_shell_and_status_without_starting_a_shell(hermes_home):
    api = load_api()
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    with TestClient(app) as client:
        routes = client.get(f"{PREFIX}/capabilities").json()["routes"]
    assert "WS /terminal/shell" in routes
    assert "GET /terminal/shell/status" in routes
    assert not (hermes_home / "state.db").exists()
    assert not (hermes_home / "plugin-data").exists()


def session_db(home, session_id, cwd):
    with sqlite3.connect(home / "state.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, cwd TEXT)")
        db.execute("INSERT INTO sessions VALUES (?, ?)", (session_id, str(cwd)))


def next_frame(ws):
    """One frame from the shell: ("text", str) or ("bytes", bytes); a close raises."""
    message = ws.receive()
    if message["type"] == "websocket.close":
        raise WebSocketDisconnect(message.get("code", 1000))
    if message.get("bytes") is not None:
        return "bytes", message["bytes"]
    return "text", message["text"]


def next_bytes(ws):
    """Terminal output, past the replay control frames."""
    while True:
        kind, value = next_frame(ws)
        if kind == "bytes":
            return value
        assert json.loads(value)["type"] == "replay"


def receive_replay(ws):
    """The attach history: everything between the start and end frames."""
    kind, value = next_frame(ws)
    assert kind == "text"
    control = json.loads(value)
    assert control["type"] == "replay" and control["state"] == "start"
    assert control["unseen"] >= 0
    history = b""
    while (frame := next_frame(ws)) != ("text", REPLAY_END):
        assert frame[0] == "bytes", frame
        history += frame[1]
    return history


def receive_until(ws, marker):
    output = b""
    while marker not in output:
        output += next_bytes(ws)
    return output


@pytest.fixture
def shell_client(hermes_home, tmp_path, monkeypatch):
    chat_ws = pytest.importorskip("hermes_cli.web_routers.chat_ws")
    real_gate = chat_ws._ws_gate

    async def allow(ws, kind):
        return ("test", "test", "test")

    monkeypatch.setattr(chat_ws, "_ws_gate", allow)
    monkeypatch.setenv("SHELL", "/bin/bash")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    session_db(hermes_home, "chat", cwd)
    from hermes_cli.pty_bridge import PtyBridge
    bridges = []
    spawn = PtyBridge.spawn

    def track_spawn(*args, **kwargs):
        bridge = spawn(*args, **kwargs)
        bridges.append(bridge)
        return bridge

    monkeypatch.setattr(PtyBridge, "spawn", track_spawn)
    api = load_api()
    api.bridges = bridges
    api.real_gate = real_gate
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    with TestClient(app) as client:
        yield client, api, cwd
    assert all(not bridge.is_alive() for bridge in bridges), "Lifespan shutdown must stop every child"


def test_shell_runs_in_the_chat_folder_and_reconnects(shell_client):
    client, api, cwd = shell_client
    url = f"{PREFIX}/terminal/shell?session_id=chat"
    status = f"{PREFIX}/terminal/shell/status?session_id=chat"
    assert client.get(status).json() == {"running": False}
    with client.websocket_connect(url) as ws:
        ws.send_text("pwd; printf 'shell-%s\\n' ok\n")
        output = receive_until(ws, b"shell-ok")
        assert str(cwd).encode() in output
    assert client.get(status).json() == {"running": True}
    with client.websocket_connect(url) as ws:
        assert b"shell-ok" in receive_replay(ws)
        ws.send_bytes(b"exit\n")
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                next_bytes(ws)
        assert closed.value.code == 4410
    assert client.get(status).json() == {"running": False}


def test_a_multiplexed_host_requires_an_explicit_profile(shell_client, monkeypatch):
    from agent import secret_scope
    monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: True)
    client, api, _ = shell_client
    response = client.get(f"{PREFIX}/terminal/shell/status?session_id=chat")
    assert response.status_code == 400
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(f"{PREFIX}/terminal/shell?session_id=chat") as ws:
            next_bytes(ws)
    assert closed.value.code == 4400
    assert api.bridges == []


def test_disconnect_immediately_after_upgrade_leaves_a_managed_shell(shell_client):
    client, api, _ = shell_client
    with client.websocket_connect(f"{PREFIX}/terminal/shell?session_id=chat"):
        pass
    assert len(api.bridges) == 1, "An accepted upgrade must already own its child"
    assert client.get(f"{PREFIX}/terminal/shell/status?session_id=chat").json() == {"running": True}


def test_resize_text_and_binary_input_and_limited_environment(shell_client, monkeypatch):
    client, api, _ = shell_client
    monkeypatch.setenv("TERMINAL_TEST_SECRET", "not-a-real-key")
    monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "not-a-real-password")
    with client.websocket_connect(f"{PREFIX}/terminal/shell?session_id=chat&cols=90&rows=30") as ws:
        ws.send_text("stty size; printf 'size-%s\\n' initial\n")
        assert b"30 90" in receive_until(ws, b"size-initial")
        ws.send_bytes(b"\x1b[RESIZE:110;40]")
        ws.send_bytes(b"stty size; printf 'size-%s\\n' changed\n")
        assert b"40 110" in receive_until(ws, b"size-changed")
        ws.send_text("printf 'secret=%s:%s:done\\n' \"${TERMINAL_TEST_SECRET-unset}\" \"${HERMES_DASHBOARD_BASIC_AUTH_PASSWORD-unset}\"\n")
        assert b"secret=unset:unset:done" in receive_until(ws, b"secret=unset:unset:done")
    assert len(api.bridges) == 1


@pytest.mark.parametrize("query", ["", "session_id=unknown", "session_id=chat&cols=no", "session_id=chat&rows=0",
                                       "session_id=chat&cols=2001", "session_id=chat&shell_profile=../escape",
                                       "session_id=chat&shell_profile=missing"])
def test_bad_queries_never_spawn_a_shell(shell_client, query):
    client, api, _ = shell_client
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(f"{PREFIX}/terminal/shell?{query}") as ws:
            next_bytes(ws)
    assert closed.value.code == 4400
    assert api.bridges == []


def test_missing_folder_is_not_replaced_by_the_launch_directory(shell_client):
    client, api, cwd = shell_client
    cwd.rmdir()
    assert client.get(f"{PREFIX}/terminal/shell/status?session_id=chat").status_code == 409
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(f"{PREFIX}/terminal/shell?session_id=chat") as ws:
            next_bytes(ws)
    assert closed.value.code == 4400 and api.bridges == []
