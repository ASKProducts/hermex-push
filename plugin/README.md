# hermex-push plugin

A `hermes-agent` platform plugin (`hermex`) that posts sealed events to a hermex-push relay on
each turn boundary. Design: [hermex#490](https://github.com/uzairansaruzi/hermex/issues/490).
Slice: [hermex#555](https://github.com/uzairansaruzi/hermex/issues/555).

Install identifier the app sends: `https://github.com/uzairansaruzi/hermex-push.git/plugin`
(hermes-agent splits it at `.git/` into the clone URL and the `plugin` subdirectory). Enable
with `hermes plugins enable hermex-push`, set `HERMEX_PUSH_RELAY_URL`, restart the gateway.

## Fact checks (hermes-agent v0.21.2 = tag `v2026.9.11`, re-read at v0.21.3)

1. **The turn-end hook fires for webui turns.** hermes-webui runs `AIAgent` in its own process
   with `platform="webui"` (`api/streaming.py`), and every `run_conversation()` ends in
   `agent/turn_finalizer.py`, which invokes `on_session_end`. `invoke_hook` lazily discovers
   plugins, so hooks fire in any process that shares the Hermes home and has the plugin in
   `plugins.enabled`. Hermex's Bot connection runs through the Hermes Desktop backend
   (`tui_gateway`); its sessions carry the source the creating client declared, `ios` for
   phone-created sessions (observed on the owner's host), `desktop` or `tui` for its own
   surfaces. The plugin maps `ios`/`desktop`/`tui` to `source: bot`, `webui` to
   `source: webui`, anything else (including `bot_room` group chats) to `other`. A session whose
   platform the turn hooks never named gets nothing (fail closed), so an evicted chat-platform
   session cannot be pushed to the phone by mistake.
   No plugin change is needed for [hermex#561](https://github.com/uzairansaruzi/hermex/issues/561).
   One caveat: the Desktop backend also fires `on_session_end` with `interrupted=True` when a
   socket drops; the plugin sends nothing for interrupted turns, so a turn never pushes twice.
2. **A `kind: platform` plugin can mount a dashboard route.** Dashboard discovery scans
   `<plugins>/*/dashboard/manifest.json` without reading `plugin.yaml`, and
   `_mount_plugin_api_routes` mounts the manifest's `api` file at `/api/plugins/<name>/` once
   the plugin is in `plugins.enabled`. All `/api/` routes sit behind the dashboard's auth
   middleware. So `GET /api/plugins/hermex-push/pairing` works and no QR fallback is needed.
3. **Approvals use the observer hooks, not `register_approval_transport`.** Both exist on
   0.21.2, but a registered transport *replaces* every built-in prompt surface once selected
   by `security.approval.transport`, which would take approvals away from the Bot UI that
   answers them. `pre_approval_request` / `post_approval_response` are in `VALID_HOOKS` and
   observe every surface, so the plugin uses those. Questions come from `pre_tool_call` on the
   `clarify` tool. The `hermex` platform is loaded eagerly (only bundled platforms defer), so
   the hooks are live in every process.

**Profiles.** Sessions run under a Hermes profile scope (`<root>/profiles/<name>`), and each
profile has its own plugin directory, `plugins.enabled` list and `.env`. hermes-agent discovers
a profile's plugins once, on first use, so a per-profile install only takes effect after the
backend restarts. Instead, one install serves every profile in the same process
(`hermex_push/scopes.py`, [hermex#634](https://github.com/uzairansaruzi/hermex/issues/634)):
the first copy loaded wraps `PluginManager.invoke_hook` and `has_hook` and pushes for any
profile without its own copy, under that profile's name. That covers the dashboard backend the
Bot connection uses and hermes-webui, including profiles created later; a profile run in its own
process (`hermes -p <name>` in a terminal) still needs its own install. These are hermes-agent
internals: on a host without them the plugin logs one warning and only profiles with their own
copy push. Keys live at the root so every profile pairs as one host, and a profile without
`HERMEX_PUSH_RELAY_URL` inherits the root `.env` value.

## Validated on the owner's host (2026-09-18, hermes-agent 0.21.3)

CLI, webui and Bot turns each produced exactly one sealed `reply` plus `running`/`done`
progress events against a local capture endpoint, alongside Cadu. The captured bodies
contained ciphertext only. The pairing route answered 401 without a login and returned the
keys through the dashboard login.

## What it sends

`POST {relay_url}/installs/{install_key}/notify`, JSON. Ciphertext only; the relay never sees
a title or body. See `hermex_push/payload.py` for the authoritative shapes.

Notification (`kind` is `reply`, `approval`, `clarify` or `turn_error`):

```json
{"v": 1, "kind": "reply", "event_id": "<32 hex>", "thread_id": "<32 hex>", "collapse_id": "<32 hex>",
 "session_id": "…", "source": "bot", "is_subagent": false, "sent_at": 1726600000, "sealed": "<base64>"}
```

`sealed` is base64 of `nonce(12) || AES-256-GCM ciphertext || tag(16)` over the UTF-8 JSON
`{"title","subtitle","body","profile","request_id","bot_name"}`, AAD `hermex-preview-v1:<sha256 hex of
install_key>`. `bot_name` is the name Hermex's bot roster shows (the Desktop title, then
`display_name`, then the Profile, with the default Profile as "Hermes"), read from the host's
`profile.yaml` for each banner and clipped to 80 characters. Hermex builds the banner title from it
and `kind` in the phone's language; `title` carries the same in English (`<name> · Approval
needed`, `· Question`, `· Turn failed`, the name alone for a reply) for older app builds.
Banners render no markdown, so subtitle and body are flattened to plain text before title,
subtitle and body are clipped to 80, 120 and 400 characters. The title is never flattened,
because it is the bot's name and a fixed label; neither is an approval banner, because its body
is a shell command.
A `null` seal means encryption failed on the host; the phone shows a generic
banner. `event_id` is stable per turn or request for relay dedupe; `thread_id` and
`collapse_id` are stable per session. All three are HMAC-SHA256 keyed from the install key.

Progress (Live Activity state, routine updates at most one per session every five seconds,
status changes always through). When the relay answers `no_activity` (no phone shows a Live
Activity for the session), routine updates slow to one a minute after the turn's first 30
seconds; the next update the relay answers otherwise restores the five-second cadence:

```json
{"v": 1, "kind": "progress", "event_id": "…", "thread_id": "…", "session_id": "…", "source": "bot",
 "is_subagent": false, "sent_at": 1726600000, "status": "running", "tool": "terminal", "tool_calls": 3,
 "started_at": 1726599990}
```

Progress carries the tool's name only, never arguments or results. A notification, and the
progress that ends a turn (`done` or `failed`), is retried once after a 5xx or network error.
Other progress is not: the next update replaces it.

## Pairing

`GET /api/plugins/hermex-push/pairing` (dashboard auth) returns
`{"relay_url", "install_key", "preview_key", "platform": "hermex", "payload_version": 1,
"plugin_version": "<semver>"}`. `plugin_version` is the plugin code the dashboard process loaded. It
lags the files on disk (what `hermes plugins list` shows) until that process restarts, so Hermex
can tell an outdated plugin from one that only needs a restart; plugins older than 0.2.0 omit
it. `preview_key` is base64 of 32 bytes. Both keys are generated on first use under a file lock at
`<hermes root>/plugin-data/hermex-push/{install_key,preview_key}` (mode 0600) and never rotated
behind a paired device. They live outside the plugin's install directory so `hermes plugins
update`, a forced reinstall or `remove` cannot unpair a phone; keys from the first release's
location inside the install directory are migrated on first use. The route answers 409 while `HERMEX_PUSH_RELAY_URL` is unset.

## Capabilities

`GET /api/plugins/hermex-push/capabilities` (dashboard auth, plugin 0.5.0 and later) returns
`{"plugin_version": "<semver>", "routes": ["GET /capabilities", "GET /pairing", "POST /restart", ...]}`,
built from the routes this dashboard process mounted (method, then the path below
`/api/plugins/hermex-push`). Hermex shows a feature that needs a plugin route only where the route
is listed, since a POST can't be probed without running it. The read has no side effects, never
answers 409 and creates no keys, so a host that uses the plugin without push set up can still
answer it. A 404 means an older plugin or one the dashboard hasn't loaded since it was installed.

## Terminal

The dashboard also mounts a plain shell for each chat. See
[`hermex_push/terminal/README.md`](hermex_push/terminal/README.md) for the wire contract,
login, folder/environment rules and shell lifetime. This requires Hermes's PTY modules;
installing the plugin grants authenticated dashboard users shell access as the dashboard user.

## Provider quota

`GET /api/plugins/hermex-push/provider-quota?provider=&profile=&refresh=` (dashboard auth, plugin 0.7.0 and later) answers
one provider's account limits for a Profile in hermes-webui's `/api/provider/quota` envelope, so
Hermex's Usage screen reads both servers the same way
([hermex#1237](https://github.com/uzairansaruzi/hermex/issues/1237)):
`{"ok", "provider", "display_name", "supported", "status", "quota": null, "message"}`, plus `label`
and `account_limits` (webui's serialized account-usage snapshot) when there are limits. The limits
come from hermes-agent's `fetch_account_usage`, the source of `/usage`'s account lines, run on a
worker thread inside the Profile's config and secret scope, so the credentials are that Profile's.
An omitted `provider` is the Profile's `model.provider`; an omitted `profile` is the dashboard's own.

`status` is `available` for a snapshot with limits, `unavailable` for one with an
`unavailable_reason` or for no snapshot, and `unsupported` for a provider with neither a built-in
fetcher (`openai-codex`, `anthropic`, `openrouter`) nor a provider plugin hook. `no_key` and
`invalid_key` never appear: the agent's fetchers answer the same for both and for a network
failure. A good answer is cached for 45 s per Profile and provider (64 entries at most);
`refresh=1` skips the cached read, and a failed read never replaces a good entry.

## Restart

`POST /api/plugins/hermex-push/restart` (dashboard auth, plugin 0.4.0 and later) answers
`202 {"ok": true}` and about a second later re-execs the dashboard process it runs in with
`os.execv(sys.executable, sys.orig_argv)`, so an update Hermex installed is loaded without a trip
to the host ([hermex#934](https://github.com/uzairansaruzi/hermex/issues/934)). Before the exec it
runs the session teardown the dashboard registers for its own exit (flush transcripts, stop running
turns and their foreground commands, close every session), so running Bot turns end as they would
on a normal stop, and each closed session releases its active-session lease: the new image keeps
the PID, so a lease left behind would look live and refuse the reopened chat. The PID stays the
same, so launchd, systemd and Hermes Desktop keep tracking the process, and the command line keeps
a `-m` launch and every flag. A `hermes dashboard` started without `--no-open` opens its browser
tab again. The route answers 409, with nothing stopped, when the interpreter the dashboard started
from is gone (a rebuilt virtualenv). Like the dashboard's own POST actions, the route
relies on the app-wide Host check, auth gate and CORS policy, which cover every plugin route.
Hermex probes the public `/api/status` until the dashboard answers, then reads the pairing route
for the new `plugin_version`.

## Git

The host's own git routes can push and switch branches but not fetch, pull or create a branch, so
the plugin adds them (dashboard auth, plugin 0.6.0 and later;
[hermex#1236](https://github.com/uzairansaruzi/hermex/issues/1236)). Hermex shows Fetch, Pull and
New branch on a Hermes chat only while `/capabilities` lists all three.

- `POST /api/plugins/hermex-push/git/fetch {"path", "remote"?}` runs
  `git fetch --prune --no-recurse-submodules [remote]`.
- `POST /api/plugins/hermex-push/git/pull {"path", "remote"?}` runs
  `git pull --ff-only --no-recurse-submodules [remote]`: never a merge commit.
- `POST /api/plugins/hermex-push/git/branch {"path", "name", "start"?}` runs
  `git switch -c <name> [start]`, which creates the branch and switches to it, carrying any
  uncommitted changes.

They answer as the host's git routes do: `200 {"ok": true, "message": <git's output>}`, or
`400 {"detail"}` when git fails. They run in the dashboard process through the host's own
hardening: the path through `hermes_cli.web_server_files._fs_path`, git through
`hermes_cli.web_git._git` (hardened argv, non-interactive env, no prompts), off the event loop with
a 60 s timeout. An option-like `remote` or `start`, and a `name` the host's `_sanitize_branch`
would rewrite, are refused with 400 before git runs.

## Layout

- `__init__.py` registers the `hermex` platform and the hooks.
- `hermex_push/keys.py` key files; `privacy.py` sealing and keyed ids; `payload.py` event shapes;
  `sources.py` coarse source and skip rules; `progress.py` coalescing; `relay.py` background POST;
  `hooks.py` hook callbacks; `adapter.py` the send-only platform adapter.
- `dashboard/plugin_api.py` the capabilities, pairing, restart, git and provider-quota routes,
  with `hermex_push/quota.py` behind the last; `dashboard/dist/index.js` a no-op the SPA
  requires.
- `hermex_push_tests/` pytest suite; `fixtures/sealed_preview.json` is a fixed-key vector the
  phone's Notification Service Extension tests can decrypt as well.

`HERMEX_PUSH_RELAY_URL` must be https; plain http is accepted only to loopback (local capture),
because the install key travels in the URL path. Redirects from the relay are refused.

Cron `deliver: hermex` and the `input`, `delivery` and `system` kinds are V1.1
([hermex#566](https://github.com/uzairansaruzi/hermex/issues/566)).

## Development

```sh
uv venv .venv && uv pip install --python .venv/bin/python pytest cryptography fastapi httpx pyyaml
.venv/bin/python -m pytest plugin -q
```

`test_restart_needs_the_dashboard_sign_in`, the git route tests that run git, and
`test_the_agent_host_knows_the_built_in_fetchers` need hermes-agent importable; they skip in the
venv above. To run them against hermes-agent itself, use a hermes-agent virtualenv, for example
`uv run --no-project --python <hermes-agent venv>/bin/python --with pytest python -m pytest plugin -q`.

Every merged change under `plugin/` bumps `PLUGIN_VERSION` in `hermex_push/__init__.py` (patch
for fixes, minor for features) along with `plugin.yaml`, `pyproject.toml` and
`dashboard/manifest.json`; `test_plugin.py` fails when the four disagree.

Hosts pick up a change with `hermes plugins update hermex-push` on hermes-agent 0.21.5 or later.
Earlier versions cannot update a subdirectory install, so reinstall there with
`hermes plugins install https://github.com/uzairansaruzi/hermex-push.git/plugin --force --enable`.
Either way the pairing keys survive, and the change takes effect once the Hermes processes that
run agents restart.
