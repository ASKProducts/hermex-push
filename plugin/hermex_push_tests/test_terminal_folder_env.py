"""Folder and environment: the chat's stored folder or Hermes's default; only allowlisted variables."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from terminal_support import SHELL_URL, folder, read_until, shell
from terminal_support import client, home, make_service, open_gate, spawned, svc  # noqa: F401  (fixtures)

pytestmark = pytest.mark.timeout(60)

SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-test-secret",
    "OPENAI_API_KEY": "sk-test-secret",
    "OPENAI_BASE_URL": "https://example.invalid",
    "ANTHROPIC_TOKEN": "anthropic-test-token",
    "GITHUB_TOKEN": "ghp-test-token",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": "pw",
    "TELNYX_API_KEY": "KEYtest",
}
# What the shell adds by itself (bash: SHLVL, $_ and OLDPWD) and what Hermes's PtyBridge.spawn
# always adds to the env it is given (hermes_cli/pty_bridge.py: HERMES_PTY_HOST=dashboard).
SHELL_OWN = {"SHLVL", "_", "OLDPWD"}
BRIDGE_OWN = {"HERMES_PTY_HOST"}


def write_session(home: Path, session_id: str, cwd) -> None:
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, cwd TEXT)")
    db.execute("INSERT OR REPLACE INTO sessions (id, source, cwd) VALUES (?, 'tui', ?)", (session_id, cwd))
    db.commit()
    db.close()


# --- folder ------------------------------------------------------------------------------------

def test_folder_stored(home, tmp_path):
    chat_dir = tmp_path / "project"
    chat_dir.mkdir()
    write_session(home, "chat-1", str(chat_dir))
    assert folder.resolve_folder("chat-1", "") == str(chat_dir)


def test_folder_missing_uses_default(home, tmp_path):
    write_session(home, "chat-null", None)
    default = str(tmp_path / "default-cwd")
    assert folder.default_cwd() == default  # Hermes's _fs_default_cwd: terminal.cwd in config.yaml
    assert folder.resolve_folder("chat-null", "") == default     # row without a folder
    assert folder.resolve_folder("no-such-chat", "") == default  # no row
    (home / "state.db").unlink()
    assert folder.resolve_folder("chat-null", "") == default     # no state.db at all


def test_folder_deleted_uses_default(home, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    write_session(home, "chat-gone", str(gone))
    gone.rmdir()
    assert folder.resolve_folder("chat-gone", "") == str(tmp_path / "default-cwd")


def test_folder_reads_the_profiles_state_db(home, tmp_path):
    work = home / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "config.yaml").write_text("{}\n")
    work_dir = tmp_path / "work-project"
    work_dir.mkdir()
    write_session(work, "chat-w", str(work_dir))
    write_session(home, "chat-w", str(tmp_path))  # same id in the default profile: not used
    assert folder.resolve_folder("chat-w", "work") == str(work_dir)
    with pytest.raises(LookupError):
        folder.resolve_folder("chat-w", "nosuchprofile")


def test_folder_lookup_is_read_only(home, tmp_path):
    write_session(home, "chat-ro", str(tmp_path))
    before = sorted(p.name for p in home.iterdir())
    folder.resolve_folder("chat-ro", "")
    assert sorted(p.name for p in home.iterdir()) == before


def test_folder_reads_a_real_hermes_state_db(home, tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(home / "state.db")
    db.create_session("chat-real", "tui")
    db.close()
    project = tmp_path / "real-project"
    project.mkdir()
    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (str(project), "chat-real"))
    con.commit()
    con.close()
    assert folder.resolve_folder("chat-real", "") == str(project)


def test_shell_starts_in_the_chats_folder(client, home, tmp_path):
    chat_dir = tmp_path / "chat folder"
    chat_dir.mkdir()
    write_session(home, "chat-pwd", str(chat_dir))
    with client.websocket_connect(f"{SHELL_URL}?session_id=chat-pwd") as ws:
        ws.send_text('echo "P[$(pwd)]$((1+1))"\n')
        assert read_until(ws, r"P\[(.*?)\]2").group(1) == str(chat_dir)


# --- environment -------------------------------------------------------------------------------

def test_build_env_is_exactly_the_allowlist_plus_terminal_vars(monkeypatch):
    environ = {key: f"v-{key}" for key in shell.ENV_ALLOWLIST}
    environ.update(SECRETS)
    environ.update({"HERMES_HOME": "/x", "PYTHONPATH": "/y", "SSH_AUTH_SOCK": "/z"})
    env = shell.build_env("/some/dir", environ)
    assert set(env) == set(shell.ENV_ALLOWLIST) | set(shell.TERMINAL_VARS)
    assert env["TERM"] == "xterm-256color" and env["COLORTERM"] == "truecolor"
    assert env["PWD"] == "/some/dir" and env["HERMEX_TERMINAL"] == "1"
    assert all(env[k] == f"v-{k}" for k in shell.ENV_ALLOWLIST)


def test_shell_choice(monkeypatch):
    assert shell.shell_path({"SHELL": "/bin/bash"}) == "/bin/bash"
    fallback = shell.shell_path({"SHELL": "/nonexistent/shell"})
    assert Path(fallback).name in {"zsh", "bash", "sh"}
    assert shell.shell_argv("/bin/bash") == ["/bin/bash", "-i"]
    assert shell.shell_argv("/usr/bin/zsh") == ["/usr/bin/zsh", "-i"]
    assert shell.shell_argv("/usr/bin/fish") == ["/usr/bin/fish"]


def test_real_shell_env_has_only_allowlisted_names(client, monkeypatch):
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "t")
    with client.websocket_connect(f"{SHELL_URL}?session_id=chat-env") as ws:
        # Arithmetic markers so the echoed command line never matches the markers themselves.
        ws.send_text('echo "<<$((40+2))>>"; env; echo "<<$((50+3))>>"\n')
        body = read_until(ws, r"(?s)<<42>>\r?\n(.*?)<<53>>").group(1)
    names = set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=", body.replace("\r", ""), re.M))
    assert names, body
    allowed = set(shell.ENV_ALLOWLIST) | set(shell.TERMINAL_VARS) | SHELL_OWN | BRIDGE_OWN
    assert names <= allowed, sorted(names - allowed)
    assert {"TERM", "COLORTERM", "PWD", "HERMEX_TERMINAL", "PATH", "HOME"} <= names
    leaked = [n for n in names if n.endswith("_API_KEY") or n.endswith("_TOKEN")
              or n.startswith("ANTHROPIC_") or n.startswith("OPENAI_")]
    assert not leaked
    assert not any(value in body for value in SECRETS.values())
