"""Shared setup for the terminal tests (test_terminal_*.py import the fixtures by name): the
companion plugin's api module loaded the way the dashboard loads it, and fixtures that give each
test its own shell registry and prove every shell it started is gone afterwards. conftest.py
points HERMES_HOME at a throwaway directory before any Hermes import."""

from __future__ import annotations

import os
import re
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import importlib.util
import sys
from pathlib import Path

MOUNT = "/api/plugins/hermex-push"
TERMINAL = MOUNT + "/terminal"


def load_api(name):
    path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

api = load_api("hermes_dashboard_plugin_hermex_push_terminal_test")

from hermex_push.terminal import folder, service, shell  # noqa: E402,F401

SHELL_URL = TERMINAL + "/shell"
STATUS_URL = TERMINAL + "/shell/status"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until(predicate, timeout: float = 10.0) -> bool:
    """Poll a real condition (a file, a process) with a bounded wait; tests only."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


class Closed(Exception):
    def __init__(self, code):
        super().__init__(f"socket closed with {code}")
        self.code = code


def read_until(ws, pattern: str, transcript: list | None = None) -> re.Match:
    """Read output until ``pattern`` matches the accumulated text (pytest-timeout bounds it)."""
    text = "".join(transcript) if transcript is not None else ""
    regex = re.compile(pattern)
    while True:
        match = regex.search(text)
        if match:
            return match
        msg = ws.receive()
        if msg["type"] == "websocket.close":
            raise Closed(msg.get("code"))
        chunk = msg.get("bytes")
        piece = chunk.decode("utf-8", "replace") if chunk is not None else (msg.get("text") or "")
        if transcript is not None:
            transcript.append(piece)
        text += piece


def read_until_close(ws) -> int:
    while True:
        msg = ws.receive()
        if msg["type"] == "websocket.close":
            return msg.get("code")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fresh HERMES_HOME, an empty user HOME (no rc files), bash, and a known default folder."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    user_home = tmp_path / "user"
    user_home.mkdir()
    default_dir = tmp_path / "default-cwd"
    default_dir.mkdir()
    # Hermes's default working folder comes from terminal.cwd (GET /api/fs/default-cwd).
    (hermes_home / "config.yaml").write_text(f"terminal:\n  cwd: {default_dir}\n")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: hermes_home)
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(hermes_home)
    try:
        yield hermes_home
    finally:
        reset_hermes_home_override(token)


@pytest.fixture
def spawned(monkeypatch):
    """Every shell started during the test (bridges); asserts each one's process is gone at the end."""
    bridges = []
    real_spawn = shell.spawn_shell

    def counting_spawn(cwd, *, cols, rows):
        bridge = real_spawn(cwd, cols=cols, rows=rows)
        bridges.append(bridge)
        return bridge

    monkeypatch.setattr(shell, "spawn_shell", counting_spawn)
    yield bridges
    leftovers = [b.pid for b in bridges if pid_alive(b.pid)]
    for bridge in bridges:
        bridge.close()  # idempotent; only does work if a test failed before cleanup
    assert not leftovers, f"shell processes left running: {leftovers}"


@pytest.fixture
def make_service(monkeypatch):
    def _make(**kwargs) -> service.TerminalService:
        svc = service.TerminalService(**kwargs)
        monkeypatch.setattr(service, "_SERVICE", svc)
        return svc
    return _make


@pytest.fixture
def svc(make_service):
    return make_service()


@pytest.fixture
def open_gate(monkeypatch):
    """Hermes's gate replaced by one that lets every socket through (auth has its own tests)."""
    from hermes_cli.web_routers import chat_ws
    calls = []

    async def gate(ws, kind):
        calls.append(kind)
        return ("testclient", "test", "none")

    monkeypatch.setattr(chat_ws, "_ws_gate", gate)
    return calls


def make_app(**include_kwargs) -> FastAPI:
    app = FastAPI()
    app.include_router(api.router, prefix=MOUNT, **include_kwargs)
    return app


@pytest.fixture
def client(home, svc, spawned, open_gate):
    with TestClient(make_app()) as c:
        yield c
    # Leaving the block ran the router's shutdown handler: every shell is closed.
    assert all(not b.is_alive() for b in spawned)
