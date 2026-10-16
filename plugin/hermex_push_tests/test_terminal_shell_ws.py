"""The shell socket: login gate, keep-alive registry behaviour, resize, status, shutdown."""

from __future__ import annotations

import sys

import pytest
from hermes_cli.pty_session import PtySessionRegistry
from fastapi import Depends
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from terminal_support import (SHELL_URL, STATUS_URL, make_app, pid_alive, read_until, read_until_close,
                      service, wait_until)
from terminal_support import client, home, make_service, open_gate, spawned, svc  # noqa: F401  (fixtures)

pytestmark = pytest.mark.timeout(60)
requires_restart = pytest.mark.skipif(
    not callable(getattr(PtySessionRegistry, "close_other_sessions", None)),
    reason="Restart needs Hermes 0.22 or newer")


def shell_pid(ws) -> int:
    ws.send_text('echo "PID=$$:$((7*6))"\n')
    return int(read_until(ws, r"PID=(\d+):42").group(1))


def status(client, session_id, **params):
    response = client.get(STATUS_URL, params={"session_id": session_id, **params})
    assert response.status_code == 200
    return response.json()["running"]


# --- login gate --------------------------------------------------------------------------------

@pytest.fixture
def gated(home, monkeypatch):
    """Hermes's REAL _ws_gate in gated mode (the box's configuration: password login + tickets).

    Gate state lives on hermes_cli.web_server.app.state, as in Hermes's own WS-auth tests.
    """
    from hermes_cli import web_server
    from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests
    _reset_for_tests()
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "testserver", raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_port", 443, raising=False)
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    yield
    _reset_for_tests()


def gated_client():
    # Mounted exactly as the dashboard mounts plugin routers (with its per-request profile scope).
    from hermes_cli import web_server_dashboard
    scope = getattr(web_server_dashboard, "_plugin_route_secret_scope", None)
    app = make_app(dependencies=[Depends(scope)] if scope else [])
    return TestClient(app)  # Host: testserver, matching bound_host


def test_real_gate_rejects_missing_ticket_4401_and_starts_no_shell(gated, svc, spawned):
    with gated_client() as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"{SHELL_URL}?session_id=chat-a"):
                pass
        assert exc.value.code == 4401
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"{SHELL_URL}?session_id=chat-a&ticket=not-a-ticket"):
                pass
        assert exc.value.code == 4401
    assert spawned == []
    assert not svc.running(service.registry_key("", "chat-a"))


def test_real_gate_accepts_a_minted_ticket_once(gated, svc, spawned):
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket
    ticket = mint_ticket(user_id="aaron", provider="basic")
    with gated_client() as client:
        with client.websocket_connect(f"{SHELL_URL}?session_id=chat-t&ticket={ticket}") as ws:
            ws.send_text('echo "ok-$((20+1))"\n')
            read_until(ws, r"ok-21")
        # Single use: the same ticket again is refused.
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"{SHELL_URL}?session_id=chat-t&ticket={ticket}"):
                pass
        assert exc.value.code == 4401
    assert len(spawned) == 1


@pytest.mark.parametrize("code", [4401, 4403, 4404, 4408])
def test_gate_refusal_is_final(home, svc, spawned, monkeypatch, code):
    from hermes_cli.web_routers import chat_ws

    async def refusing_gate(ws, kind):
        assert kind == "hermex-terminal"
        await ws.close(code=code)
        return None

    monkeypatch.setattr(chat_ws, "_ws_gate", refusing_gate)
    with TestClient(make_app()) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"{SHELL_URL}?session_id=chat-a"):
                pass
    assert exc.value.code == code
    assert spawned == []


def test_gate_import_failure_closes_1011_without_accepting(home, svc, spawned, monkeypatch, caplog):
    import hermes_cli.web_routers as routers_pkg
    monkeypatch.delattr(routers_pkg, "chat_ws", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_routers.chat_ws", None)  # import -> ImportError
    with TestClient(make_app()) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"{SHELL_URL}?session_id=chat-a"):
                pass
    assert exc.value.code == 1011
    assert exc.value.reason == service.GATE_MISSING_REASON
    assert spawned == []
    assert any(r.levelname == "ERROR" and "_ws_gate" in r.getMessage() for r in caplog.records)


def test_gate_that_is_not_async_closes_1011(home, svc, spawned, monkeypatch):
    from hermes_cli.web_routers import chat_ws
    monkeypatch.setattr(chat_ws, "_ws_gate", lambda ws, kind: ("peer", "mode", "cred"))
    with TestClient(make_app()) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"{SHELL_URL}?session_id=chat-a"):
                pass
    assert exc.value.code == 1011
    assert spawned == []


@pytest.mark.parametrize("query", ["", "session_id=", "session_id=a%00b", "session_id=a&cols=wide",
                                   "session_id=a&profile=Not/Valid", "session_id=a&profile=nosuchprofile"])
def test_bad_query_closes_4400(client, spawned, query):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"{SHELL_URL}?{query}"):
            pass
    assert exc.value.code == 4400
    assert spawned == []


# --- keep-alive registry -----------------------------------------------------------------------

def test_reattach_replays_missed_output(client, spawned, tmp_path):
    flag = tmp_path / "printed"
    url = f"{SHELL_URL}?session_id=chat-r&cols=100&rows=30"
    with client.websocket_connect(url) as ws:
        first_pid = shell_pid(ws)
        # Printed after this socket is gone: only the registry's buffer can hand it back.
        ws.send_text(f'sleep 0.5; echo "missed-$((6*7))x"; touch "{flag}"\n')
        read_until(ws, r"sleep 0\.5")  # the shell has the line before we disconnect
    assert status(client, "chat-r") is True
    assert wait_until(flag.exists)
    with client.websocket_connect(url) as ws:
        transcript = []
        read_until(ws, r"missed-42x", transcript)
        assert shell_pid(ws) == first_pid
    assert len(spawned) == 1


@requires_restart
def test_restart_closes_the_old_shell(client, spawned):
    with client.websocket_connect(f"{SHELL_URL}?session_id=chat-x") as ws:
        old_pid = shell_pid(ws)
    assert pid_alive(old_pid)
    with client.websocket_connect(f"{SHELL_URL}?session_id=chat-x&restart=1") as ws:
        new_pid = shell_pid(ws)
        assert new_pid != old_pid
        assert not pid_alive(old_pid)
    assert len(spawned) == 2


@requires_restart
def test_restart_while_another_socket_is_attached(client, spawned):
    with client.websocket_connect(f"{SHELL_URL}?session_id=chat-y") as old:
        old_pid = shell_pid(old)
        with client.websocket_connect(f"{SHELL_URL}?session_id=chat-y&restart=1") as new:
            assert shell_pid(new) != old_pid
            assert not pid_alive(old_pid)
            assert read_until_close(old) == 4409  # the old viewer is told it was superseded


@requires_restart
def test_restart_keeps_other_chats_and_profiles(client, home, spawned):
    work = home / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "config.yaml").write_text("{}\n")
    with client.websocket_connect(f"{SHELL_URL}?session_id=same") as target:
        old_pid = shell_pid(target)
    with client.websocket_connect(f"{SHELL_URL}?session_id=other") as other:
        other_pid = shell_pid(other)
    with client.websocket_connect(f"{SHELL_URL}?session_id=same&profile=work") as profile:
        profile_pid = shell_pid(profile)
    with client.websocket_connect(f"{SHELL_URL}?session_id=same&restart=1") as new:
        assert shell_pid(new) != old_pid
    assert not pid_alive(old_pid)
    assert pid_alive(other_pid) and pid_alive(profile_pid)
    assert len(spawned) == 4


def test_one_shell_per_chat_and_per_profile(client, home, spawned):
    work = home / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "config.yaml").write_text("{}\n")
    with client.websocket_connect(f"{SHELL_URL}?session_id=same") as a:
        pid_a = shell_pid(a)
    with client.websocket_connect(f"{SHELL_URL}?session_id=same&profile=work") as b:
        assert shell_pid(b) != pid_a
    with client.websocket_connect(f"{SHELL_URL}?session_id=same&profile=current") as c:
        assert shell_pid(c) == pid_a
    assert status(client, "same", profile="work") is True
    assert status(client, "same", profile="Work") is True
    assert len(spawned) == 2


def test_shell_exit_closes_4410(client, spawned):
    with client.websocket_connect(f"{SHELL_URL}?session_id=chat-e") as ws:
        shell_pid(ws)
        ws.send_text("exit\n")
        assert read_until_close(ws) == 4410
    assert status(client, "chat-e") is False
    # The next connection gets a fresh shell.
    with client.websocket_connect(f"{SHELL_URL}?session_id=chat-e") as ws:
        shell_pid(ws)
    assert len(spawned) == 2


def test_registry_full_closes_1013(home, make_service, spawned, open_gate):
    make_service(max_sessions=1)
    with TestClient(make_app()) as client:
        with client.websocket_connect(f"{SHELL_URL}?session_id=one") as first:
            shell_pid(first)
            with client.websocket_connect(f"{SHELL_URL}?session_id=two") as second:
                msg = second.receive()
                assert msg["type"] == "websocket.close"
                assert msg["code"] == 1013 and "too many terminals" in msg["reason"]
        # Once the first is idle (detached) it makes room: the oldest idle shell is closed.
        old_pid = spawned[0].pid
        with client.websocket_connect(f"{SHELL_URL}?session_id=two") as second:
            shell_pid(second)
            # The registry closes the evicted shell in the background.
            assert wait_until(lambda: not pid_alive(old_pid))
    assert len(spawned) == 2


def test_resize_frame_resizes_and_is_never_written(client, spawned):
    transcript = []
    with client.websocket_connect(f"{SHELL_URL}?session_id=chat-s&cols=80&rows=24") as ws:
        ws.send_text('echo "S=$(stty size)=$((1+2))"\n')
        assert read_until(ws, r"S=(\d+ \d+)=3", transcript).group(1) == "24 80"
        ws.send_bytes(b"\x1b[RESIZE:132;40]")
        ws.send_text("\x1b[RESIZE:120;33]")  # text frames too
        ws.send_text('echo "S=$(stty size)=$((2+2))"\n')
        assert read_until(ws, r"S=(\d+ \d+)=4", transcript).group(1) == "33 120"
    assert "RESIZE" not in "".join(transcript)


def test_reattach_applies_the_new_size(client, spawned):
    with client.websocket_connect(f"{SHELL_URL}?session_id=chat-z&cols=80&rows=24") as ws:
        shell_pid(ws)
    with client.websocket_connect(f"{SHELL_URL}?session_id=chat-z&cols=50&rows=20") as ws:
        ws.send_text('echo "S=$(stty size)=$((3+4))"\n')
        assert read_until(ws, r"S=(\d+ \d+)=7").group(1) == "20 50"


def test_status_true_and_false(client, spawned):
    assert status(client, "chat-st") is False
    with client.websocket_connect(f"{SHELL_URL}?session_id=chat-st") as ws:
        shell_pid(ws)
        assert status(client, "chat-st") is True
    assert status(client, "chat-st") is True  # detached, still running
    assert status(client, "chat-st", profile="work") is False
    assert status(client, "other-chat") is False
    assert client.get(STATUS_URL).status_code == 400
    assert client.get(STATUS_URL, params={"session_id": "x", "profile": "Bad/Name"}).status_code == 400


def test_dashboard_shutdown_closes_every_shell(home, svc, spawned, open_gate):
    with TestClient(make_app()) as client:
        with client.websocket_connect(f"{SHELL_URL}?session_id=chat-1") as ws:
            pid_one = shell_pid(ws)
        with client.websocket_connect(f"{SHELL_URL}?session_id=chat-2") as ws:
            pid_two = shell_pid(ws)
        assert pid_alive(pid_one) and pid_alive(pid_two)
    # Lifespan shutdown ran the router's shutdown handler.
    assert not pid_alive(pid_one) and not pid_alive(pid_two)


def test_input_reaches_the_shell_as_bytes(client, spawned):
    with client.websocket_connect(f"{SHELL_URL}?session_id=chat-b") as ws:
        ws.send_bytes('echo "bytes-ü-$((5*5))"\n'.encode())
        read_until(ws, r"bytes-ü-25")


def test_restart_without_public_api_returns_error_and_preserves_shell(client, spawned, monkeypatch):
    monkeypatch.delattr(PtySessionRegistry, "close_other_sessions", raising=False)
    url = f"{SHELL_URL}?session_id=unsupported-restart"
    with client.websocket_connect(url) as ws:
        old_pid = shell_pid(ws)
    with client.websocket_connect(url + "&restart=1") as ws:
        assert ws.receive_text() == "\r\nRestart needs Hermes 0.22 or newer\r\n"
        message = ws.receive()
        assert message == {"type": "websocket.close", "code": 1011,
                           "reason": "Restart needs Hermes 0.22 or newer"}
    assert pid_alive(old_pid)
    assert status(client, "unsupported-restart") is True
    with client.websocket_connect(url) as ws:
        assert shell_pid(ws) == old_pid
    assert len(spawned) == 1
