"""Live check of the host terminal (hermex-push /terminal/*) through the public dashboard URL, the
way the app calls it.

Run only against a disposable dashboard with the plugin enabled. Set TERMINAL_BASE,
TERMINAL_USERNAME and TERMINAL_PASSWORD_FILE (a file containing the disposable password).

Usage: python terminal_live_check.py [session_id]

session_id defaults to the newest chat in ~/.hermes/state.db that has a stored folder (read-only).
The check refuses a chat whose shell is already running (it would attach to it), and ends by typing
``exit`` so its own shell closes (4410).

Checks: shell/status; no ticket is refused (Hermes's gate closes 4401 before accepting, which
uvicorn sends to the client as an HTTP 403 handshake rejection); a ticket opens the shell;
``echo ok`` round trip; ``pwd`` = the chat's folder; disconnect + reconnect replays output printed
while away; ``env`` shows no secret names; ``exit`` closes 4410 and shell/status goes false.
"""
import asyncio
import http.cookiejar
import json
import os
import re
import sqlite3
import sys
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ["TERMINAL_BASE"].rstrip("/")
WS_BASE = BASE.replace("https://", "wss://").replace("http://", "ws://")
PLUGIN = "/api/plugins/hermex-push/terminal"
STEP_TIMEOUT = 15


def _password():
    from pathlib import Path
    return Path(os.environ["TERMINAL_PASSWORD_FILE"]).read_text().strip()


def _username():
    return os.environ["TERMINAL_USERNAME"]


jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def api(method, path, body=None, timeout=30):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"")[:300].decode("utf-8", "replace")


def login():
    s, _ = api("POST", "/auth/password-login", {"provider": "basic", "username": _username(), "password": _password()})
    s2, me = api("GET", "/api/auth/me")
    print(f"login {s}, me {s2}")
    return s == 200


def ticket():
    s, t = api("POST", "/api/auth/ws-ticket", {})
    assert s == 200, (s, t)
    return t["ticket"]


def pick_session():
    db = sqlite3.connect(f"file:{os.path.join(os.environ['HERMES_HOME'], 'state.db')}?mode=ro", uri=True)
    try:
        row = db.execute("select id, cwd from sessions where cwd is not null and cwd != '' "
                         "order by started_at desc limit 1").fetchone()
    finally:
        db.close()
    if not row:
        raise SystemExit("no chat with a stored folder")
    return row


def stored_folder(session_id):
    db = sqlite3.connect(f"file:{os.path.join(os.environ['HERMES_HOME'], 'state.db')}?mode=ro", uri=True)
    try:
        row = db.execute("select cwd from sessions where id = ?", (session_id,)).fetchone()
    finally:
        db.close()
    return row[0] if row else None


def status(session_id):
    s, body = api("GET", f"{PLUGIN}/shell/status?" + urllib.parse.urlencode({"session_id": session_id}))
    assert s == 200, (s, body)
    return body["running"]


def shell_url(session_id, with_ticket=True, **extra):
    query = {"session_id": session_id, "cols": 100, "rows": 30, **extra}
    if with_ticket:
        query["ticket"] = ticket()
    return f"{WS_BASE}{PLUGIN}/shell?" + urllib.parse.urlencode(query)


async def connect(url):
    import websockets
    return await websockets.connect(url, origin=BASE, max_size=4 * 1024 * 1024)


async def read_until(ws, pattern, seen=""):
    regex = re.compile(pattern, re.S)
    deadline = time.monotonic() + STEP_TIMEOUT
    while not regex.search(seen):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no {pattern!r} in {seen[-400:]!r}")
        frame = await asyncio.wait_for(ws.recv(), remaining)
        seen += frame.decode("utf-8", "replace") if isinstance(frame, bytes) else frame
    return regex.search(seen), seen


async def main(session_id, folder):
    import websockets
    results = {}
    assert status(session_id) is False, "this chat's shell is already running; pick another chat"
    results["status_before"] = False

    try:
        ws = await connect(shell_url(session_id, with_ticket=False))
        try:
            await asyncio.wait_for(ws.recv(), STEP_TIMEOUT)
        except websockets.ConnectionClosed as exc:
            results["no_ticket"] = f"closed {exc.rcvd.code if exc.rcvd else None}"
        else:
            results["no_ticket"] = "ACCEPTED (bad)"
    except websockets.InvalidStatus as exc:
        results["no_ticket"] = f"handshake refused HTTP {exc.response.status_code}"
    print("no ticket:", results["no_ticket"])

    ws = await connect(shell_url(session_id))
    await ws.send('echo "o""k-$((40+2))"\n')
    await read_until(ws, r"ok-42")
    results["echo"] = "ok"
    await ws.send('echo "PWD=[$(pwd)]$((1+1))"\n')
    # The shell echoes the typed line too ('PWD=[$(pwd)]$((1+1))'), so match only a bracketed path
    # with no '$' or line break in it: the printed result, never the echoed command.
    match, _ = await read_until(ws, r"PWD=\[([^\]\r\n$]*)\]2")
    results["pwd"] = match.group(1)
    results["pwd_is_chat_folder"] = match.group(1) == folder
    assert status(session_id) is True
    # Printed while nobody is connected; the flag file (same machine) says when it has been.
    flag = os.path.join(tempfile.mkdtemp(prefix="terminal-live-"), "printed")
    await ws.send(f'sleep 1; echo "away-$((6*7))x"; touch {flag}\n')
    await read_until(ws, r"sleep 1")
    await ws.close()
    deadline = time.monotonic() + STEP_TIMEOUT
    while not os.path.exists(flag):
        assert time.monotonic() < deadline, "the shell never printed while away"
        await asyncio.sleep(0.1)

    ws = await connect(shell_url(session_id))
    await read_until(ws, r"away-42x")
    results["replay"] = "ok"
    await ws.send('echo "<<$((40+2))>>"; env; echo "<<$((50+3))>>"\n')
    match, _ = await read_until(ws, r"<<42>>\r?\n(.*?)<<53>>")
    names = sorted(set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=", match.group(1).replace("\r", ""), re.M)))
    secret = [n for n in names if n.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
              or n.startswith(("ANTHROPIC_", "OPENAI_"))]
    results["env_names"] = names
    results["env_secret_names"] = secret
    await ws.send("exit\n")
    try:
        await asyncio.wait_for(ws.recv(), STEP_TIMEOUT)
        while True:
            await asyncio.wait_for(ws.recv(), STEP_TIMEOUT)
    except websockets.ConnectionClosed as exc:
        results["exit_close_code"] = exc.rcvd.code if exc.rcvd else None
    results["status_after"] = status(session_id)
    print(json.dumps(results, indent=2))
    ok = (results["pwd_is_chat_folder"] and results["replay"] == "ok" and not secret and results["exit_close_code"] == 4410
          and results["status_after"] is False and not results["no_ticket"].startswith("ACCEPTED"))
    print("PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sid = sys.argv[1]
        folder = stored_folder(sid)
    else:
        sid, folder = pick_session()
    print("chat", sid, "folder", folder)
    assert login()
    sys.exit(0 if asyncio.run(main(sid, folder)) else 1)
