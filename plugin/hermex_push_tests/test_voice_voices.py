"""GET/PUT /voices: one voice key per mode in a profile's config.yaml; all homes are temporary."""
import json
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

# companion puts the Hermes source root on sys.path, as the dashboard entry point does: the venv's
# editable-install finder predates some top-level Hermes modules (hermes_yaml) hermes_cli.config imports.
from hermex_push.voice import routes as api

MOUNT = "/api/plugins/hermex-push"
VOICE = MOUNT + "/voice"

from tools.voice_live import GPT_LIVE_VOICES  # noqa: E402

URL = VOICE + "/voices"
CASCADE = ["alloy", "ash", "ballad", "cedar", "coral", "echo", "fable", "marin", "nova", "onyx",
           "sage", "shimmer", "verse"]

SAMPLE = """\
# user comment kept
model:
  default: claude-opus-5-5
  provider: anthropic
tts:
  provider: openai
  openai:
    model: gpt-4o-mini-tts
    voice: marin   # cascade voice
    api_key: ${OPENAI_API_KEY}
voice:
  voice_chat_mode: gpt-live
  gpt_live:
    model: gpt-live-1
    voice: marin
    instructions: Speak slowly.
plugins:
  enabled:
    - hermex-push
"""
# Sequences use Hermes's own writer indentation (save_config re-emits ``  - x`` as ``    - x``), so the
# byte comparison below isolates what this route changes from Hermes's normal formatting.


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: tmp_path)
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_hermes_home_override(token)


@pytest.fixture
def client(home):
    app = FastAPI()
    app.include_router(api.router, prefix=MOUNT)
    with TestClient(app) as c:
        yield c


def make_profile(home: Path, name: str, text: str = "{}\n") -> Path:
    profile = home / "profiles" / name
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(text)
    return profile


def body(live: str, cascade: str) -> dict:
    return {"ok": True, "live": {"voice": live, "choices": list(GPT_LIVE_VOICES)},
            "cascade": {"voice": cascade, "choices": CASCADE}}


def test_get_defaults_when_unset(client, home):
    response = client.get(URL)
    assert response.status_code == 200
    # Hermes defaults: DEFAULT_LIVE_VOICE and DEFAULT_CONFIG tts.openai.voice.
    assert response.json() == body("marin", "alloy")
    assert len(response.json()["live"]["choices"]) == 14
    assert not (home / "config.yaml").exists()  # GET never creates the file


def test_get_reflects_set_values(client, home):
    (home / "config.yaml").write_text(SAMPLE.replace("voice: marin   #", "voice: sage   #")
                                      .replace("    voice: marin\n    instr", "    voice: cedar\n    instr"))
    assert client.get(URL).json() == body("cedar", "sage")
    assert client.get(URL + "?profile=current").json() == body("cedar", "sage")
    assert client.get(URL + "?profile=default").json() == body("cedar", "sage")


@pytest.mark.parametrize("mode,voice,path", [
    ("live", "cedar", ("voice", "gpt_live", "voice")),
    ("cascade", "coral", ("tts", "openai", "voice")),
])
def test_put_changes_only_its_key(client, home, mode, voice, path):
    config = home / "config.yaml"
    config.write_text(SAMPLE)
    before = yaml.safe_load(SAMPLE)
    response = client.put(URL, json={"mode": mode, "voice": voice})
    assert response.status_code == 200, response.text
    assert response.json() == client.get(URL).json()
    assert response.json()[mode]["voice"] == voice
    after = yaml.safe_load(config.read_text())
    expected = json.loads(json.dumps(before))
    node = expected
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = voice
    assert after == expected  # semantically identical apart from the one key
    text = config.read_text()
    assert "# user comment kept" in text and "${OPENAI_API_KEY}" in text  # no secret expansion
    old_lines, new_lines = SAMPLE.splitlines(), text.splitlines()
    changed = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
    assert len(old_lines) == len(new_lines) and len(changed) == 1  # byte-for-byte except that line


def test_put_default_valued_voice_is_kept_explicit(client, home):
    (home / "config.yaml").write_text("tts:\n  provider: openai\n")
    assert client.put(URL, json={"mode": "cascade", "voice": "alloy"}).status_code == 200
    assert yaml.safe_load((home / "config.yaml").read_text())["tts"] == {
        "provider": "openai", "openai": {"voice": "alloy"}}


def test_put_rejects_bad_mode_and_voice(client, home):
    (home / "config.yaml").write_text(SAMPLE)
    assert client.put(URL, json={"mode": "chained", "voice": "marin"}).status_code == 400
    assert client.put(URL, json={"mode": "live", "voice": "alloy"}).status_code == 400  # TTS-only voice
    assert client.put(URL, json={"mode": "cascade", "voice": "quartz"}).status_code == 400  # live-only
    assert (home / "config.yaml").read_text() == SAMPLE


def test_profile_errors(client, home):
    assert client.get(URL + "?profile=missing").status_code == 404
    assert client.put(URL + "?profile=missing", json={"mode": "live", "voice": "cedar"}).status_code == 404
    assert client.get(URL + "?profile=../escape").status_code == 400
    assert client.put(URL + "?profile=../escape", json={"mode": "live", "voice": "cedar"}).status_code == 400
    assert not (home / "profiles/missing").exists()


def test_profile_scoping(client, home):
    root = home / "config.yaml"
    root.write_text(SAMPLE)
    profile = make_profile(home, "voice-test", "tts:\n  provider: openai\n")
    response = client.put(URL + "?profile=voice-test", json={"mode": "live", "voice": "vesper"})
    assert response.status_code == 200
    assert response.json() == body("vesper", "alloy")
    assert client.put(URL + "?profile=voice-test", json={"mode": "cascade", "voice": "verse"}).json() \
        == body("vesper", "verse")
    assert root.read_text() == SAMPLE  # default profile untouched
    assert client.get(URL).json() == body("marin", "marin")
    assert yaml.safe_load((profile / "config.yaml").read_text()) == {
        "tts": {"provider": "openai", "openai": {"voice": "verse"}},
        "voice": {"gpt_live": {"voice": "vesper"}}}
    # And the reverse: writing default leaves the profile alone.
    profile_text = (profile / "config.yaml").read_text()
    assert client.put(URL + "?profile=default", json={"mode": "live", "voice": "stone"}).status_code == 200
    assert (profile / "config.yaml").read_text() == profile_text
    assert client.get(URL + "?profile=voice-test").json()["live"]["voice"] == "vesper"


def test_put_keeps_edit_made_after_get(client, home):
    config = home / "config.yaml"
    config.write_text(SAMPLE)
    assert client.get(URL).status_code == 200
    config.write_text(SAMPLE.replace("Speak slowly.", "Speak quickly."))  # another writer's edit
    assert client.put(URL, json={"mode": "cascade", "voice": "nova"}).status_code == 200
    after = yaml.safe_load(config.read_text())
    assert after["voice"]["gpt_live"]["instructions"] == "Speak quickly."
    assert after["tts"]["openai"]["voice"] == "nova"


def test_dashboard_mount_advertises_only_kept_voice_routes(home):
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("voice_mount_under_test", path)
    assert spec is not None and spec.loader is not None
    dashboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dashboard)
    app = FastAPI()
    app.include_router(dashboard.router, prefix=MOUNT)
    with TestClient(app) as client:
        routes = set(client.get(MOUNT + "/capabilities").json()["routes"])
        assert {route for route in routes if "/voice/" in route} == {
            "GET /voice/voices", "PUT /voice/voices"}
        assert client.get(URL).status_code == 200
        saved = client.put(URL, json={"mode": "cascade", "voice": "coral"})
        assert saved.status_code == 200
        assert client.get(URL).json() == saved.json()
        assert client.get(VOICE + "/glossary").status_code == 404
        assert client.post(VOICE + "/sessions/test", json={"active": True}).status_code == 404
        assert client.post(VOICE + "/transcribe", json={}).status_code == 404
        assert client.get(VOICE + "/transcripts/test").status_code == 404


def test_voice_choices_need_dashboard_sign_in(home, monkeypatch):
    from hermes_cli import web_server
    web_server.app.include_router(api.router, prefix=MOUNT)
    client = TestClient(web_server.app, base_url="http://127.0.0.1")
    config = home / "config.yaml"
    config.write_text(SAMPLE)
    for gated in (False, True):
        monkeypatch.setattr(web_server.app.state, "auth_required", gated, raising=False)
        assert client.get(URL).status_code == 401
        assert client.put(URL, json={"mode": "live", "voice": "cedar"}).status_code == 401
    assert config.read_text() == SAMPLE
