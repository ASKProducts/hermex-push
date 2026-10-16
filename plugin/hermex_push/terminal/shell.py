"""The shell itself: which program, its arguments, its environment, and the spawn.

Shell choice and environment mirror the hermes-webui terminal (``api/terminal.py``): ``$SHELL``
when it exists, else zsh, bash or sh, interactive; only an allowlist of the dashboard's environment
reaches the shell, so no API keys or other secrets do.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import List, Mapping, Optional

from hermes_cli.pty_bridge import PtyBridge

# The web UI terminal's list (hermes-webui api/terminal.py ``_SAFE_ENV_KEYS``).
ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
    "LANGUAGE", "TZ", "TMPDIR", "TEMP", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
)
# Set by the plugin on top of the allowlist (PWD is the shell's starting folder).
TERMINAL_VARS = ("TERM", "COLORTERM", "PWD", "HERMEX_TERMINAL")

# Same framing as Hermes's /api/pty (hermes_cli/web_server_chat.py ``_RESIZE_RE``); the plugin
# keeps its own copy. Only a frame that is exactly this resizes; anything else is shell input.
RESIZE_RE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")


def shell_path(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    shell = env.get("SHELL") or ""
    if shell and Path(shell).exists():
        return shell
    return shutil.which("zsh") or shutil.which("bash") or shutil.which("sh") or "/bin/sh"


def shell_argv(shell: str) -> List[str]:
    if Path(shell).name in {"zsh", "bash", "sh"}:
        return [shell, "-i"]
    return [shell]


def build_env(cwd: str, environ: Optional[Mapping[str, str]] = None) -> dict:
    """The shell's whole environment: the allowlist plus the terminal variables, nothing else."""
    source = os.environ if environ is None else environ
    env = {key: source[key] for key in ENV_ALLOWLIST if key in source}
    env.update({
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "PWD": cwd,
        "HERMEX_TERMINAL": "1",
    })
    return env


def parse_resize(frame: bytes) -> Optional[tuple]:
    """``(cols, rows)`` when ``frame`` is exactly a resize frame, else None."""
    match = RESIZE_RE.match(frame)
    if match and match.end() == len(frame):
        return int(match.group(1)), int(match.group(2))
    return None


def spawn_shell(cwd: str, *, cols: int, rows: int) -> PtyBridge:
    """Start the shell behind a PTY. Blocking (fork/exec): the registry runs it in a thread.

    ``env`` is always explicit: with none, ``PtyBridge.spawn`` hands the child the dashboard's
    whole environment.
    """
    argv = shell_argv(shell_path())
    return PtyBridge.spawn(argv, cwd=cwd, env=build_env(cwd), cols=cols, rows=rows)
