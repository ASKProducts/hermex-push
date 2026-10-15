"""Capabilities, pairing, restart, git, quota and terminal routes, mounted at ``/api/plugins/hermex-push/`` behind
dashboard auth. Hermex calls ``GET /pairing`` once with the user's dashboard login and stores the
result in the Keychain; no key is ever typed.

Response: ``{"relay_url": str, "install_key": <64 hex>, "preview_key": <base64 of 32 bytes>,
"platform": "hermex", "payload_version": 1, "plugin_version": "<semver>"}``. ``plugin_version``
is the code this process loaded, which lags the files on disk until the dashboard restarts; a
response without it comes from a plugin older than 0.2.0. 409 when ``HERMEX_PUSH_RELAY_URL`` is
unset, because a pairing without a relay could never deliver.

``GET /capabilities`` (hermex#1235) lists the routes this process mounted, so Hermex shows a
feature only where its route exists; a POST can't be probed without running it. Response:
``{"plugin_version": "<semver>", "routes": ["GET /capabilities", "GET /pairing", "POST /restart"]}``.
It has no side effects and never answers 409, so a host that uses the plugin without push set up
can still be read. A 404 on it means an older plugin or one the dashboard hasn't loaded.

``POST /restart`` (hermex#934) restarts the dashboard process this route runs in, so a plugin
update Hermex installed is loaded without a trip to the host.

``POST /git/fetch {path, remote?}``, ``POST /git/pull {path, remote?}`` and
``POST /git/branch {path, name, start?}`` (hermex#1236) are the git writes the host's own routes
lack: ``fetch --prune``, a fast-forward-only ``pull``, and ``switch -c`` (create and switch).
They answer as the host's git routes do: 200 ``{"ok": true, "message": <git's output>}``, or 400
``{"detail": ...}``. Each runs in-process through the host's own hardening: the path through
``hermes_cli.web_server_files._fs_path`` and git through ``hermes_cli.web_git._git`` (hardened
argv, non-interactive env). An option-like ``remote`` or ``start``, and a ``name`` the host's
branch sanitizer would rewrite, are refused before git runs.

``GET /provider-quota?provider=&profile=&refresh=`` (hermex#1237) answers one provider's account
limits for a Profile in hermes-webui's ``/api/provider/quota`` envelope (``hermex_push.quota``).

"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket
from starlette.routing import WebSocketRoute
from fastapi.responses import JSONResponse
from pydantic import BaseModel

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.append(_ROOT)

from hermex_push import PLATFORM_NAME, PLUGIN_VERSION, RELAY_URL_ENV, quota, terminal  # noqa: E402
from hermex_push.hooks import relay_url_from_env  # noqa: E402
from hermex_push.keys import load_or_create_keys  # noqa: E402
from hermex_push.payload import PAYLOAD_VERSION  # noqa: E402

_shells = terminal.Shells()


@contextlib.asynccontextmanager
async def _lifespan(app):
    try:
        yield
    finally:
        await _shells.close()


router = APIRouter(lifespan=_lifespan)
_log = logging.getLogger(__name__)

# Long enough for the 202 to reach the phone before the process goes away.
RESTART_DELAY_S = 1.0
_restart_lock = threading.Lock()
_restart_pending = False
# A fetch or pull talks to the remote, so it gets longer than the host's 30 s for local git.
GIT_REMOTE_TIMEOUT_S = 60
_quota_cache = quota.QuotaCache()


@router.get("/capabilities")
def get_capabilities() -> JSONResponse:
    """Built from ``router.routes``, so the list can't drift from what is mounted: each entry is a
    method and the path relative to the plugin's mount, sorted for a stable answer."""
    try:
        from hermes_cli.plugin_isolation import isolation_mode, ISOLATION_HOST, HOST_PROCESS_ENV
        sockets = os.environ.get(HOST_PROCESS_ENV) != "1" and isolation_mode() != ISOLATION_HOST
    except ImportError:
        sockets = True
    routes = sorted(
        f"{method} {route.path}"
        for route in router.routes
        if sockets or not isinstance(route, WebSocketRoute)
        for method in (("WS",) if isinstance(route, WebSocketRoute) else getattr(route, "methods", ()))
    )
    return JSONResponse(
        {"plugin_version": PLUGIN_VERSION, "routes": routes},
        headers={"Cache-Control": "no-store"},
    )


async def terminal_shell(ws: WebSocket) -> None:
    await _shells.connect(ws)


# Starlette routes enter the WS gate before FastAPI's HTTP Profile dependencies.
router.add_websocket_route("/terminal/shell", terminal_shell)


@router.get("/terminal/shell/status")
async def terminal_shell_status(session_id: str = "", profile: str = "") -> JSONResponse:
    return JSONResponse(await _shells.status(session_id, profile), headers={"Cache-Control": "no-store"})


@router.get("/pairing")
def get_pairing() -> JSONResponse:
    relay_url = relay_url_from_env()
    if not relay_url:
        raise HTTPException(status_code=409, detail=f"{RELAY_URL_ENV} is not set on this host")
    keys = load_or_create_keys()
    return JSONResponse(
        {
            "relay_url": relay_url,
            "install_key": keys.install_key,
            "preview_key": keys.preview_key_b64,
            "platform": PLATFORM_NAME,
            "payload_version": PAYLOAD_VERSION,
            "plugin_version": PLUGIN_VERSION,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/provider-quota")
async def get_provider_quota(provider: str = "", profile: str = "", refresh: bool = False) -> JSONResponse:
    """``provider`` defaults to the Profile's configured one, and ``profile`` to the dashboard's own.
    Served from a 45 s cache unless ``refresh`` is set. The envelope's ``status``, not the HTTP
    status, says whether limits came back."""
    body = await quota.in_profile_scope(
        profile, lambda: quota.provider_quota(quota.AGENT, _quota_cache, profile, provider, refresh))
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.post("/restart", status_code=202)
def restart_dashboard() -> JSONResponse:
    """Answers 202, then re-execs this dashboard about a second later with its own command line.

    A POST, so it takes the dashboard's auth and the same cross-site protection as its own POST
    actions (``/api/gateway/restart``): the app-wide Host check, the auth gate and its SameSite
    session cookie, and CORS limited to localhost. ``os.execv`` keeps the PID, so a supervisor
    (launchd, systemd, Hermes Desktop) keeps tracking it, and ``sys.orig_argv`` keeps a ``-m``
    launch and every flag the dashboard was started with. A second request while one is pending
    joins it. 409, with nothing stopped, when the interpreter this process started from is gone
    (a rebuilt virtualenv), since an exec could not replace it.
    """
    global _restart_pending
    if not (sys.orig_argv and os.access(sys.executable, os.X_OK)):
        raise HTTPException(status_code=409, detail="This dashboard's Python is gone; restart it on the host.")
    with _restart_lock:
        if not _restart_pending:
            timer = threading.Timer(RESTART_DELAY_S, _reexec)
            timer.daemon = True
            timer.start()
            _restart_pending = True
    return JSONResponse({"ok": True}, status_code=202, headers={"Cache-Control": "no-store"})


def _reexec() -> None:
    """Runs the session teardown the dashboard registers with ``atexit``, which an exec skips
    (``tui_gateway.server._shutdown_sessions``: flush transcripts, stop running turns and their
    foreground commands, close every session), then replaces the process. Closing releases each
    session's active-session lease: the new image keeps this PID and start time, so a lease left
    behind would still look live and refuse the reopened chat. A host without the teardown is
    still restarted. An exec that fails lowers the exit fence the teardown raised, so the process
    keeps running commands, and lets the next request try again."""
    global _restart_pending
    with contextlib.suppress(Exception):
        sys.modules["tui_gateway.server"]._shutdown_sessions()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    try:
        os.execv(sys.executable, sys.orig_argv)
    except Exception:
        _log.exception("hermex-push: could not restart the dashboard")
        with contextlib.suppress(Exception):
            sys.modules["tools.environments.base"]._exit_fenced = False
        with _restart_lock:
            _restart_pending = False


class GitSyncBody(BaseModel):
    path: str
    remote: str | None = None


class GitBranchBody(BaseModel):
    path: str
    name: str
    start: str | None = None


@router.post("/git/fetch")
async def git_fetch(body: GitSyncBody) -> JSONResponse:
    return await _run_git(body.path, ["fetch", "--prune", "--no-recurse-submodules", *_operand(body.remote)])


@router.post("/git/pull")
async def git_pull(body: GitSyncBody) -> JSONResponse:
    """Fast-forward only: a merge commit is nothing anyone can edit from a phone."""
    return await _run_git(body.path, ["pull", "--ff-only", "--no-recurse-submodules", *_operand(body.remote)])


@router.post("/git/branch")
async def git_branch(body: GitBranchBody) -> JSONResponse:
    """Creates ``name`` from ``start`` (HEAD when absent) and switches to it. Uncommitted changes
    come along, as ``git switch -c`` carries them."""
    start = _operand(body.start)
    from hermes_cli.web_git import _sanitize_branch

    if not body.name or _sanitize_branch(body.name) != body.name:
        raise HTTPException(status_code=400, detail="Invalid branch name.")
    return await _run_git(body.path, ["switch", "-c", body.name, *start])


def _operand(value: str | None) -> list[str]:
    """An optional ref or remote as git's last argument; one git would read as an option is refused."""
    if not value:
        return []
    if value.startswith("-"):
        raise HTTPException(status_code=400, detail="Invalid git argument.")
    return [value]


async def _run_git(path: str, args: list[str]) -> JSONResponse:
    """Runs ``git args`` in ``path`` off the event loop, through the host's path and git hardening.
    A non-zero exit is a 400 carrying git's stderr, as the host's ``_git_op`` answers."""
    from hermes_cli.web_git import _git
    from hermes_cli.web_server_files import _fs_path

    cwd = str(_fs_path(path))
    code, out, err = await asyncio.to_thread(_git, cwd, args, timeout=GIT_REMOTE_TIMEOUT_S)
    if code != 0:
        raise HTTPException(status_code=400, detail=err.strip() or f"git {args[0]} failed")
    return JSONResponse({"ok": True, "message": (out or err).strip()}, headers={"Cache-Control": "no-store"})
