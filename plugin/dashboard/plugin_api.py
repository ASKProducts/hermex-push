"""Capabilities, pairing and restart routes, mounted by the dashboard at ``/api/plugins/hermex-push/`` behind
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
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.append(_ROOT)

from hermex_push import PLATFORM_NAME, PLUGIN_VERSION, RELAY_URL_ENV  # noqa: E402
from hermex_push.hooks import relay_url_from_env  # noqa: E402
from hermex_push.keys import load_or_create_keys  # noqa: E402
from hermex_push.payload import PAYLOAD_VERSION  # noqa: E402

router = APIRouter()
_log = logging.getLogger(__name__)

# Long enough for the 202 to reach the phone before the process goes away.
RESTART_DELAY_S = 1.0
_restart_lock = threading.Lock()
_restart_pending = False


@router.get("/capabilities")
def get_capabilities() -> JSONResponse:
    """Built from ``router.routes``, so the list can't drift from what is mounted: each entry is a
    method and the path relative to the plugin's mount, sorted for a stable answer."""
    routes = sorted(f"{method} {route.path}" for route in router.routes for method in getattr(route, "methods", ()))
    return JSONResponse(
        {"plugin_version": PLUGIN_VERSION, "routes": routes},
        headers={"Cache-Control": "no-store"},
    )


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
