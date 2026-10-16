"""Shell lifetime and the two routes, on Hermes's own keep-alive PTY registry.

The plugin owns one ``hermes_cli.pty_session.PtySessionRegistry`` with the settings of Hermes's
``/api/pty`` registry (``hermes_cli/web_server_chat.py`` ``PTY_REGISTRY``): a shell survives its
socket, missed output (up to 1 MB) is replayed on reconnect, a shell nobody is connected to closes
after 30 minutes, a shell that exits closes at once, and with 16 shells open the oldest idle one
makes room (none idle: 1013). Each chat has one shell, keyed ``"<profile>\\0<session_id>"``.

Login: WebSocket upgrades bypass the dashboard's HTTP middleware, so the shell route runs the same
pre-accept check ``/api/pty`` uses, ``hermes_cli.web_routers.chat_ws._ws_gate``. If that check
can't be found the route closes with 1011 and never accepts.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
from dataclasses import dataclass
from typing import Dict, Optional

from fastapi import WebSocket, WebSocketDisconnect

from hermes_cli.pty_bridge import PtyUnavailableError
from hermes_cli.pty_session import PtySession, PtySessionRegistry, RegistryFull, run_reaper

from . import folder, shell

_log = logging.getLogger("hermex_push.terminal")

GATE_KIND = "hermex-terminal"
GATE_MISSING_REASON = "terminal unavailable: Hermes login check not found"
RESTART_UNAVAILABLE_REASON = "Restart needs Hermes 0.22 or newer"

# Hermes's /api/pty registry settings (web_server_chat.py PTY_REGISTRY).
REGISTRY_TTL = 30 * 60
REGISTRY_MAX_SESSIONS = 16
REGISTRY_BUFFER_CAP = 1 * 1024 * 1024
REGISTRY_READ_TIMEOUT = 0.2

CLOSE_BAD_REQUEST = 4400
CLOSE_TOO_MANY = 1013
CLOSE_UNAVAILABLE = 1011

DEFAULT_COLS, DEFAULT_ROWS = 80, 24
MAX_COLS, MAX_ROWS = 2000, 1000
_SESSION_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_TRUE = {"1", "true", "yes", "on"}


class BadRequest(ValueError):
    pass


def canonical_profile(profile: Optional[str]) -> str:
    """"" for the request's own profile (absent, empty or ``current``), else the validated name."""
    raw = (profile or "").strip()
    if raw in ("", "current"):
        return ""
    from hermes_cli import profiles
    try:
        name = profiles.normalize_profile_name(raw)
        profiles.validate_profile_name(name)
    except ValueError as exc:
        raise BadRequest(f"invalid profile: {exc}") from exc
    return name


def validate_session_id(session_id: Optional[str]) -> str:
    value = session_id or ""
    if not _SESSION_ID_RE.match(value) or not value.strip():
        raise BadRequest("session_id is required")
    return value


def registry_key(profile: str, session_id: str) -> str:
    return f"{profile}\0{session_id}"


def _dimension(raw: Optional[str], default: int, maximum: int, name: str) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise BadRequest(f"{name} must be a number") from exc
    return max(1, min(value, maximum))


@dataclass(frozen=True)
class ShellRequest:
    session_id: str
    profile: str
    cols: int
    rows: int
    restart: bool
    explicit_size: bool

    @property
    def key(self) -> str:
        return registry_key(self.profile, self.session_id)

    @classmethod
    def from_query(cls, query) -> "ShellRequest":
        cols_raw, rows_raw = query.get("cols"), query.get("rows")
        return cls(
            session_id=validate_session_id(query.get("session_id")),
            profile=canonical_profile(query.get("profile")),
            cols=_dimension(cols_raw, DEFAULT_COLS, MAX_COLS, "cols"),
            rows=_dimension(rows_raw, DEFAULT_ROWS, MAX_ROWS, "rows"),
            restart=(query.get("restart") or "").strip().lower() in _TRUE,
            explicit_size=bool(cols_raw) and bool(rows_raw),
        )


def load_ws_gate():
    """Hermes's ``_ws_gate`` when it can be imported and is async, else None (logged)."""
    try:
        from hermes_cli.web_routers.chat_ws import _ws_gate
    except Exception as exc:  # ImportError, or anything the module raises while importing
        _log.error("hermex-terminal: Hermes login check (chat_ws._ws_gate) not importable: %s", exc)
        return None
    if not inspect.iscoroutinefunction(_ws_gate):
        _log.error("hermex-terminal: Hermes login check chat_ws._ws_gate is not an async function")
        return None
    return _ws_gate


class TerminalService:
    """One registry of shells, its reaper, and which shells this plugin started."""

    def __init__(self, *, ttl: float = REGISTRY_TTL, max_sessions: int = REGISTRY_MAX_SESSIONS,
                 buffer_cap: int = REGISTRY_BUFFER_CAP, read_timeout: float = REGISTRY_READ_TIMEOUT) -> None:
        self.registry = PtySessionRegistry(
            ttl=ttl, max_sessions=max_sessions, buffer_cap=buffer_cap, read_timeout=read_timeout)
        # The registry has no public lookup; keep the sessions attach_or_spawn handed us so
        # shell/status can ask them whether they're alive.
        self._sessions: Dict[str, PtySession] = {}
        self._reaper: Optional[asyncio.Task] = None

    def ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(run_reaper(self.registry))

    def running(self, key: str) -> bool:
        session = self._sessions.get(key)
        if session is None:
            return False
        if session.alive and session.bridge.is_alive():
            return True
        self._sessions.pop(key, None)
        return False

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        await self.registry.close_all()
        self._sessions.clear()

    async def restart(self, key: str) -> None:
        await self.registry.close_other_sessions(key, keep_key="")

    async def handle_shell(self, ws: WebSocket) -> None:
        gate_fn = load_ws_gate()
        if gate_fn is None:
            await ws.close(code=CLOSE_UNAVAILABLE, reason=GATE_MISSING_REASON)
            return
        gate = await gate_fn(ws, GATE_KIND)
        if gate is None:  # the gate already closed the socket (4401/4403/4404/4408)
            return

        try:
            req = ShellRequest.from_query(ws.query_params)
            if req.restart and not callable(getattr(self.registry, "close_other_sessions", None)):
                await ws.accept()
                await ws.send_text(f"\r\n{RESTART_UNAVAILABLE_REASON}\r\n")
                await ws.close(code=CLOSE_UNAVAILABLE, reason=RESTART_UNAVAILABLE_REASON)
                return
            cwd = await asyncio.to_thread(folder.resolve_folder, req.session_id, req.profile)
        except (BadRequest, LookupError) as exc:
            await ws.close(code=CLOSE_BAD_REQUEST, reason=str(exc)[:120])
            return

        await ws.accept()
        _log.info("hermex-terminal accepted gate=%s profile=%r restart=%s", gate, req.profile, req.restart)
        self.ensure_reaper()
        key = req.key

        def _spawn():
            return shell.spawn_shell(cwd, cols=req.cols, rows=req.rows)

        try:
            if req.restart:
                await self.restart(key)
            session, created = await self.registry.attach_or_spawn(key, spawn=_spawn)
        except RegistryFull:
            await ws.close(code=CLOSE_TOO_MANY, reason="too many terminals open")
            return
        except (PtyUnavailableError, OSError) as exc:
            _log.warning("hermex-terminal: shell failed to start: %s: %s", type(exc).__name__, exc)
            await ws.send_text("\r\n\x1b[31mThe terminal could not start a shell.\x1b[0m\r\n")
            await ws.close(code=CLOSE_UNAVAILABLE, reason="shell failed to start")
            return
        self._sessions[key] = session
        if not created and req.explicit_size:
            session.bridge.resize(cols=req.cols, rows=req.rows)

        # A plain shell's replay is self-contained: no redraw (that's for Hermes's TUI).
        if not await session.attach(ws, force_redraw=False):
            self.registry.detach(key, ws)
            return

        # /api/pty's keep-alive writer loop (hermes_cli/web_routers/chat_ws.py): the session's
        # drain task forwards output to the attached socket and buffers it while detached; on
        # shell exit it closes the socket with 4410, which unparks ws.receive().
        try:
            while True:
                try:
                    msg = await ws.receive()
                except RuntimeError:  # receive() after the drain task already closed us
                    break
                if msg.get("type") == "websocket.disconnect":
                    break
                raw = msg.get("bytes")
                if raw is None:
                    text = msg.get("text")
                    raw = text.encode("utf-8") if isinstance(text, str) else b""
                if not raw:
                    continue
                size = shell.parse_resize(raw)
                if size is not None:  # consumed here, never written to the shell
                    session.bridge.resize(cols=size[0], rows=size[1])
                    continue
                if not await session.write(ws, raw):
                    _log.warning("hermex-terminal: shell input stalled; closing the socket")
                    with contextlib.suppress(Exception):
                        await ws.close(code=CLOSE_TOO_MANY, reason="shell input stalled")
                    break
        except WebSocketDisconnect:
            pass
        finally:
            # Detach only: the shell keeps running for a reconnect; the reaper closes it after
            # the TTL, or at once when it has exited.
            self.registry.detach(key, ws)

    def status(self, session_id: Optional[str], profile: Optional[str]) -> dict:
        key = registry_key(canonical_profile(profile), validate_session_id(session_id))
        return {"running": self.running(key)}


_SERVICE = TerminalService()


def current() -> TerminalService:
    return _SERVICE
