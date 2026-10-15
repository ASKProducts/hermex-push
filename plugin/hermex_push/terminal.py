"""Chat shells using the dashboard terminal gate and keep-alive PTYs."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import json
import os
from pathlib import Path
import re
import sqlite3
from weakref import WeakValueDictionary

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from hermex_push.quota import in_profile_scope

_log = logging.getLogger(__name__)
IDLE_TTL_S = 1800
MAX_SHELLS = 16
BUFFER_BYTES = 1024 * 1024
READ_TIMEOUT_S = 0.2
_RESIZE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")
_ENV_NAMES = "PATH HOME USER LOGNAME SHELL LANG LC_ALL LC_CTYPE LC_MESSAGES LANGUAGE TZ TMPDIR TEMP".split()
REPLAY_START = '{"type":"replay","state":"start","unseen":0}'
SEND_TIMEOUT_S = 10
REPLAY_END = '{"type":"replay","state":"end"}'


def chat_context(session_id: str) -> tuple[str, str]:
    """Read only, inside the requested Profile scope. Never create a missing DB/chat."""
    from hermes_constants import get_hermes_home
    from hermes_cli.web_routers.files import _fs_default_cwd

    if not session_id or len(session_id) > 256 or "\0" in session_id:
        raise HTTPException(status_code=400, detail="A session_id is required (at most 256 characters).")
    home = get_hermes_home().resolve()
    database = home / "state.db"
    if not database.is_file():
        raise HTTPException(status_code=404, detail="Chat not found in this Profile.")
    try:
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=409, detail="The chat database is unavailable.") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Chat not found in this Profile.")
    cwd = str(row[0] or _fs_default_cwd())
    if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise HTTPException(status_code=409, detail="The chat's working folder is unavailable.")
    return f"{home}\0{session_id}", cwd


async def resolve_chat(session_id: str, profile: str) -> tuple[str, str]:
    from hermes_cli.web_routers._common import destructive_profile

    profile = destructive_profile(profile, "terminal/shell") or ""
    return await in_profile_scope(profile, lambda: chat_context(session_id))


def shell_command() -> list[str]:
    shell = os.environ.get("SHELL", "")
    if not shell or not Path(shell).is_absolute() or not os.access(shell, os.X_OK):
        shell = "/bin/sh"
    return [shell, "-i"]


def shell_env(cwd: str) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_NAMES if key in os.environ}
    env.update(TERM="xterm-256color", COLORTERM="truecolor", PWD=cwd)
    return env


class OutputHistory:
    """Count output appended by Hermes and the prefix delivered to a viewer."""

    def __init__(self, buffer):
        self.buffer = buffer
        self.produced = len(buffer.snapshot())
        self.delivered = 0

    def append(self, data):
        self.buffer.append(data)
        self.produced += len(data)

    def snapshot(self):
        return self.buffer.snapshot()

    @property
    def truncated(self):
        return self.buffer.truncated


class Viewer:
    """Order replay before live output; close remains independent of a stalled send."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._order = asyncio.Lock()
        self._attaching = None
        self._started = False

    async def attach(self, session) -> bool:
        self.history = session.buffer
        async with self._order:
            try:
                return await self._replay(session)
            except Exception:
                session.detach(self)
                return False

    async def _replay(self, session):
        self._attaching = asyncio.current_task()
        try:
            if not await session.attach(self):
                return False
            if not self._started:
                await asyncio.wait_for(self.ws.send_text(REPLAY_START), SEND_TIMEOUT_S)
            await asyncio.wait_for(self.ws.send_text(REPLAY_END), SEND_TIMEOUT_S)
            return True
        finally:
            self._attaching = None

    async def send_bytes(self, data: bytes):
        if asyncio.current_task() is self._attaching:
            end = self.history.produced
            unseen = min(len(data), end - self.history.delivered)
            await asyncio.wait_for(self.ws.send_text(json.dumps({"type": "replay", "state": "start", "unseen": unseen})), SEND_TIMEOUT_S)
            self._started = True
            await asyncio.wait_for(self.ws.send_bytes(data), SEND_TIMEOUT_S)
            self.history.delivered = max(self.history.delivered, end)
            return
        end = self.history.produced
        async with self._order:
            try:
                await asyncio.wait_for(self.ws.send_bytes(data), SEND_TIMEOUT_S)
            except Exception:
                await self.close(code=1011)
                raise
            self.history.delivered = max(self.history.delivered, end)

    async def close(self, code: int = 1000):
        with contextlib.suppress(RuntimeError):
            await self.ws.close(code=code)


class Shells:
    """One registry per loaded dashboard API, never shared with /api/pty."""

    def __init__(self):
        self.registry = None
        self.reaper = None
        self._connect_locks = WeakValueDictionary()

    def start(self):
        from hermes_cli.pty_session import PtySessionRegistry, run_reaper

        if self.registry is None:
            self.registry = PtySessionRegistry(ttl=IDLE_TTL_S, max_sessions=MAX_SHELLS,
                                               buffer_cap=BUFFER_BYTES, read_timeout=READ_TIMEOUT_S)
            self.reaper = asyncio.create_task(run_reaper(self.registry))
        return self.registry

    async def close(self):
        if self.reaper is not None:
            self.reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reaper
            self.reaper = None
        if self.registry is not None:
            await self.registry.close_all()
            self.registry = None

    async def status(self, session_id: str, profile: str) -> dict:
        key, _ = await resolve_chat(session_id, profile)
        # Hermes has no public lookup; status reads its registry.
        if self.registry is not None:
            await self.registry.reap_idle()
        session = self.registry._sessions.get(key) if self.registry is not None else None
        return {"running": bool(session and session.alive)}

    async def connect(self, ws: WebSocket):
        try:
            from hermes_cli.web_routers.chat_ws import _ws_gate
        except ImportError:
            _log.exception("hermex-push: dashboard terminal login gate is unavailable")
            await ws.close(code=1011)
            return
        if await _ws_gate(ws, "hermex-shell") is None:
            return
        try:
            session_id = ws.query_params.get("session_id", "")
            profile = ws.query_params.get("shell_profile", "")
            cols = int(ws.query_params.get("cols", "80"))
            rows = int(ws.query_params.get("rows", "24"))
            if not 1 <= cols <= 2000 or not 1 <= rows <= 1000:
                raise ValueError("Invalid terminal size")
            key, cwd = await resolve_chat(session_id, profile)
        except (HTTPException, ValueError):
            await ws.accept()
            await ws.close(code=4400)
            return
        try:
            from hermes_cli.pty_bridge import PtyBridge
            from hermes_cli.pty_session import RegistryFull
        except ImportError:
            _log.exception("hermex-push: dashboard PTYs are unavailable")
            await ws.close(code=1011)
            return
        registry = self.start()
        lock = self._connect_locks.setdefault(key, asyncio.Lock())
        viewer = Viewer(ws)
        try:
            async with lock:
                async def acquire():
                    session, created = await registry.attach_or_spawn(
                        key, spawn=lambda: PtyBridge.spawn(shell_command(), cwd=cwd, env=shell_env(cwd), cols=cols, rows=rows))
                    if created:
                        session.detach(None)
                        session.buffer = OutputHistory(session.buffer)
                    return session, created

                acquisition = asyncio.create_task(acquire())
                try:
                    session, created = await asyncio.shield(acquisition)
                except asyncio.CancelledError:
                    await acquisition
                    raise
                await ws.accept()
                if not await viewer.attach(session):
                    await viewer.close(code=1011)
                    return
                if not created:
                    session.bridge.resize(cols=cols, rows=max(1, rows - 1))
                    session.bridge.resize(cols=cols, rows=rows)
            while True:
                try:
                    message = await ws.receive()
                except RuntimeError:  # The registry already closed this socket on EOF/supersession.
                    break
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("bytes")
                if raw is None:
                    raw = (message.get("text") or "").encode("utf-8")
                if not raw:
                    continue
                resize = _RESIZE.fullmatch(raw)
                if resize:
                    if session._ws is viewer:
                        session.bridge.resize(cols=int(resize[1]), rows=int(resize[2]))
                elif not await session.write(viewer, raw):
                    await viewer.close(code=1013)
                    break
        except RegistryFull:
            await ws.accept()
            await viewer.close(code=1013)
        except (OSError, RuntimeError):
            _log.exception("hermex-push: could not start a chat shell")
            with contextlib.suppress(RuntimeError):
                await ws.accept()
            await viewer.close(code=1011)
        except WebSocketDisconnect:
            pass
        finally:
            registry.detach(key, viewer)
