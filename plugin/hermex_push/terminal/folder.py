"""Which folder a chat's shell starts in.

The chat's own folder is ``sessions.cwd`` for its session id in the profile's ``state.db``, read
with a read-only SQLite connection. With no stored folder, or one that no longer exists, the shell
starts in Hermes's default working folder: the one ``GET /api/fs/default-cwd`` returns.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

_log = logging.getLogger("hermex_push.terminal")


def profile_home(profile: str) -> Path:
    """HERMES_HOME for a canonical profile name ("" = the request's own profile).

    Raises ``LookupError`` for a profile that does not exist.
    """
    if not profile:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    from hermes_cli import profiles
    if not profiles.profile_exists(profile):
        raise LookupError(f"profile {profile!r} does not exist")
    return profiles.get_profile_dir(profile)


def stored_cwd(db_path: Path, session_id: str) -> Optional[str]:
    """``sessions.cwd`` for ``session_id``, or None (no db, no row, no folder, unreadable)."""
    if not db_path.is_file():
        return None
    try:
        con = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
        try:
            row = con.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        _log.warning("hermex-terminal: could not read sessions.cwd from %s: %s", db_path, exc)
        return None
    if row and isinstance(row[0], str) and row[0].strip():
        return row[0]
    return None


def default_cwd() -> str:
    from hermes_cli.web_routers.files import _fs_default_cwd
    return _fs_default_cwd()


def resolve_folder(session_id: str, profile: str) -> str:
    """The chat's folder when it is stored and still a directory, else Hermes's default."""
    cwd = stored_cwd(profile_home(profile) / "state.db", session_id)
    if cwd and os.path.isabs(cwd) and os.path.isdir(cwd):
        return cwd
    return default_cwd()
