# Terminal (`hermex_push.terminal`)

A plain host shell for each chat, using the dashboard login. Both routes are listed in
`GET /api/plugins/hermex-push/capabilities` (`WS /terminal/shell`,
`GET /terminal/shell/status`). Mounted by `plugin/dashboard/plugin_api.py`.

## Routes (under `/api/plugins/hermex-push/terminal/`)

- **`WS shell`** `?session_id=&profile=&cols=&rows=&restart=1&ticket=`
  - Bytes or text frames both ways: keystrokes in, terminal output out.
  - A frame that is exactly `ESC[RESIZE:<cols>;<rows>]` resizes the shell and is never written to
    it (the `/api/pty` framing; the plugin keeps its own copy of the pattern).
  - `cols`/`rows` (default 80×24) size a new shell; on a reconnect they resize the running one.
  - `restart=1` closes this chat's shell first (a socket still attached to it gets 4409).
- **`GET shell/status`** `?session_id=&profile=` → `{"running": bool}`. Ordinary plugin HTTP
  route, so the dashboard login applies. 400 for a missing `session_id` or invalid profile name.

Close codes:

| Code | Meaning |
| --- | --- |
| 4401 / 4403 / 4404 / 4408 | Hermes's `_ws_gate`: bad or missing credential / host-origin mismatch / embedded chat off / peer not allowed |
| 1011 | Hermes's login check not found (import failed or not async; logged as an error, never accepted), or the shell failed to start |
| 4400 | bad query: no `session_id`, non-numeric `cols`/`rows`, invalid or unknown `profile` |
| 1013 | too many terminals (16 attached shells), or the shell stopped accepting input |
| 4409 | superseded: another socket attached to (or restarted) this chat's shell |
| 4410 | the shell exited |

The gate closes **before** accepting, as it does for `/api/pty`. Through the TestClient that shows
as the close code; through uvicorn (the real dashboard) a close before accept is sent as an
**HTTP 403 handshake rejection**, so the app sees a failed upgrade, not a 4401 close frame. The
same is true of 1011 (gate missing) and 4400.

## Login

WebSocket upgrades skip the dashboard's HTTP middleware, so the shell route calls
`hermes_cli.web_routers.chat_ws._ws_gate(ws, "hermex-terminal")` itself before accepting: the same
check `/api/pty` uses (the second argument is only the log label). The gate checks are
path-independent (`_ws_auth_reason`, `_ws_host_origin_reason`, `_ws_client_reason`), so the
route's move under `/api/plugins/hermex-push/` changes nothing about who gets in. The app's credential is a single-use ticket from `POST /api/auth/ws-ticket`
in `?ticket=`, like the voice speak-stream socket.

## Folder and environment

- Folder: `sessions.cwd` for `session_id` in the profile's `state.db` (read-only SQLite
  connection). No row, no folder, or a folder that no longer exists: Hermes's default working
  folder (`hermes_cli.web_routers.files._fs_default_cwd`, what `GET /api/fs/default-cwd` returns).
- Shell: `$SHELL` if it exists, else zsh, bash or sh; zsh/bash/sh start with `-i`.
- Environment: only `PATH HOME USER LOGNAME SHELL LANG LC_ALL LC_CTYPE LC_MESSAGES LANGUAGE TZ
  TMPDIR TEMP XDG_RUNTIME_DIR XDG_CONFIG_HOME XDG_DATA_HOME` from the dashboard, plus
  `TERM=xterm-256color COLORTERM=truecolor PWD=<folder> HERMEX_TERMINAL=1`, passed explicitly to
  `PtyBridge.spawn`. Hermes's `PtyBridge.spawn` itself adds `HERMES_PTY_HOST=dashboard`.

## Shell lifetime

One `hermes_cli.pty_session.PtySessionRegistry` with the `/api/pty` registry's settings (30 min
idle TTL, 16 shells, 1 MB replay, 0.2 s read timeout), key `"<profile>\0<session_id>"` (profile
`""` for the request's own profile). Disconnect keeps the shell running and buffers output;
reconnect replays it; nobody connected for 30 min closes it; a shell that exits closes at once;
the 17th shell evicts the oldest idle one or gets 1013. The reaper (`run_reaper`) starts with the
first connection. The router's shutdown handler closes every shell when the dashboard stops.

## Hermes internals relied on

`chat_ws._ws_gate`; `pty_session.PtySessionRegistry` (`attach_or_spawn`, `detach`, `reap_idle`,
`close_all`, and `close_other_sessions` for `restart=1` on newer Hermes; the older pin uses the same locked
teardown through the registry's `_attach_lock` and `_sessions` and the session's `_ws`/`close`),
`PtySession.attach/write`,
`run_reaper`, `RegistryFull`; `pty_bridge.PtyBridge` (`spawn`, `resize`), `PtyUnavailableError`;
`web_routers.files._fs_default_cwd`; `hermes_cli.profiles` and `hermes_constants.get_hermes_home`.
`plugin/hermex_push_tests/test_terminal_contract.py` checks each one's name and signature, so a Hermes update
that renames or reshapes one fails loudly.

## Tests

`plugin/hermex_push_tests/test_terminal_*.py` (fixtures in `terminal_support.py`), run with the
plugin's suite.

Real shells (bash with an empty `HOME`), a throwaway `HERMES_HOME`, and a check after every test
that each shell it started is gone. One test runs Hermes's real `_ws_gate` in gated mode.

## Live check

Install the companion plugin as described in `plugin/README.md`, then restart the dashboard.
Run `plugin/scripts/terminal_live_check.py [session_id]` against a disposable dashboard:
status, missing ticket refused, echo, stored folder, reconnect replay, allowlisted environment,
and exit with 4410. See the script's required environment variables; it never reads host credentials.

## Host requirements

The terminal requires the public `PtySessionRegistry.close_other_sessions` API for restart,
as provided by the tested Hermes v0.22.0 baseline. The older tested `ca678285` pin lacks that
API: restart prints “Restart needs Hermes 0.22 or newer” to the terminal and closes only the
request socket with 1011 and that reason. The existing shell remains running and may be
reattached normally. No private-registry fallback is used.
It runs only in the dashboard process. With `plugins.isolation: host`, Hermes forwards HTTP
but not WebSockets; terminal routes are therefore omitted from `/capabilities` in that mode
and inside a plugin-host process. Other companion capabilities remain available.
