"""Terminal routes, mounted at ``/api/plugins/hermex-push/terminal/``
(``dashboard/plugin_api.py``).

- ``WS shell?session_id=&profile=&cols=&rows=&restart=1&ticket=``: the chat's shell. Bytes or
  text both ways; a frame that is exactly ``ESC[RESIZE:<cols>;<rows>]`` resizes and is never
  written to the shell. A plain Starlette WebSocket route runs Hermes's ``_ws_gate`` before
  any Profile lookup, avoiding FastAPI's mount-level Profile dependency. Close codes: 4401/4403/4404/4408 from the gate, 1011 gate missing, shell failed
  to start or restart unsupported (error text sent), 4400 bad query, 1013 too many terminals, 4409 superseded, 4410 shell exited.
- ``GET shell/status?session_id=&profile=`` → ``{"running": bool}`` (dashboard login applies).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket

from hermex_push.terminal import service

router = APIRouter(prefix="/terminal")


async def shell_ws(ws: WebSocket) -> None:
    await service.current().handle_shell(ws)


# Plain Starlette routing bypasses FastAPI Profile dependencies before authentication.
router.add_websocket_route("/terminal/shell", shell_ws)


@router.get("/shell/status")
async def shell_status(session_id: Optional[str] = None, profile: Optional[str] = None) -> dict:
    try:
        return service.current().status(session_id, profile)
    except service.BadRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _close_shells() -> None:
    # Runs when the dashboard runs its included routers' shutdown handlers (FastAPI merges an
    # included router's shutdown handlers into the including router's, so this reaches the app
    # through the plugin's router).
    await service.current().shutdown()


router.on_shutdown.append(_close_shells)
