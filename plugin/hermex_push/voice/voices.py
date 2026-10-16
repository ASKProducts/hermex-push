"""Voice choice for the Settings picker: read/write one voice key in a profile's config.yaml.

- ``live`` (GPT-Live): ``voice.gpt_live.voice``, read by ``tools/voice_live.py``
  (``build_session_config``/``resolve_gpt_live_status``: ``live.get("voice") or DEFAULT_LIVE_VOICE``).
- ``cascade`` (Hermes TTS, OpenAI provider): ``tts.openai.voice``, read by
  ``tools/tts_tool_openai.py`` (``oai_config.get("voice", DEFAULT_OPENAI_VOICE)``), the streaming
  provider in ``tools/tts_streaming.py`` and ``tools/voice_client_config.py``.

Only the raw YAML is read (never ``load_config()``, which expands ``${VAR}`` secrets), and the write
goes through Hermes's own ``save_config`` under the target profile's home, like ``PUT /api/config``.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Dict, Tuple

# OpenAI's documented gpt-4o-mini-tts voices. Hermes has no tuple for these, only the comment on
# ``tts.openai.voice`` in hermes_cli/config_defaults.py, which lists exactly this set.
OPENAI_TTS_VOICES: Tuple[str, ...] = (
    "alloy", "ash", "ballad", "cedar", "coral", "echo", "fable", "marin", "nova", "onyx", "sage",
    "shimmer", "verse",
)

MODES: Dict[str, Tuple[str, ...]] = {
    "live": ("voice", "gpt_live", "voice"),
    "cascade": ("tts", "openai", "voice"),
}

_LOCAL_LOCK = threading.RLock()


def choices(mode: str) -> Tuple[str, ...]:
    if mode == "live":
        from tools.voice_live import GPT_LIVE_VOICES
        return tuple(GPT_LIVE_VOICES)
    return OPENAI_TTS_VOICES


def default(mode: str) -> str:
    if mode == "live":
        from tools.voice_live import DEFAULT_LIVE_VOICE
        return DEFAULT_LIVE_VOICE
    # What load_config() yields when the key is unset (DEFAULT_CONFIG), else the tool's fallback.
    from hermes_cli.config import DEFAULT_CONFIG
    from tools.tts_tool_openai import DEFAULT_OPENAI_VOICE
    value = _lookup(DEFAULT_CONFIG, MODES["cascade"])
    return str(value or DEFAULT_OPENAI_VOICE)


def _lookup(data: Any, path: Tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _scoped(home: Path, fn):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        return fn()
    finally:
        reset_hermes_home_override(token)


def _read_raw(home: Path) -> Dict[str, Any]:
    from hermes_cli.config import require_readable_config_before_write
    return require_readable_config_before_write(home / "config.yaml")


def _body(raw: Dict[str, Any]) -> dict:
    out: dict = {"ok": True}
    for mode, path in MODES.items():
        value = _lookup(raw, path)
        voice = str(value).strip() if isinstance(value, str) and value.strip() else default(mode)
        out[mode] = {"voice": voice, "choices": list(choices(mode))}
    return out


def read(home: Path) -> dict:
    return _scoped(home, lambda: _body(_read_raw(home)))


def _mutation_lock():
    # The dashboard's own read-modify-write lock for config.yaml when running inside it.
    web = sys.modules.get("hermes_cli.web_server")
    lock = getattr(web, "_CONFIG_MUTATION_LOCK", None) if web is not None else None
    return lock if lock is not None and hasattr(lock, "__enter__") else _LOCAL_LOCK


def write(home: Path, mode: str, voice: str) -> dict:
    """Set one voice key; re-read the file immediately before a single save so concurrent edits to
    other keys survive. Caller has validated ``mode``/``voice``."""
    path = MODES[mode]

    def _run() -> dict:
        from hermes_cli import config as hc
        with _mutation_lock(), hc._CONFIG_LOCK:
            raw = _read_raw(home)
            node = raw
            for key in path[:-1]:
                child = node.get(key)
                if not isinstance(child, dict):
                    child = {}
                    node[key] = child
                node = child
            node[path[-1]] = voice
            # preserve_keys keeps the choice explicit even when it equals the schema default.
            hc.save_config(raw, preserve_keys={path})
            return _body(_read_raw(home))

    return _scoped(home, _run)
