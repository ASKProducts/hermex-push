"""The real dashboard discovers and mounts the installed companion's terminal router."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from terminal_support import MOUNT, SHELL_URL, STATUS_URL, read_until, service
from terminal_support import home, make_service, spawned, svc  # noqa: F401

pytestmark = pytest.mark.timeout(60)


def test_real_dashboard_plugin_discovery_mount_and_ticket(home, svc, spawned, monkeypatch):
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket, _reset_for_tests

    app = mount_plugin(home, monkeypatch)

    _reset_for_tests()
    try:
        with TestClient(app) as client:
            capabilities = client.get(MOUNT + "/capabilities")
            assert capabilities.status_code == 200
            assert {"WS /terminal/shell", "GET /terminal/shell/status", "POST /restart"} <= set(capabilities.json()["routes"])
            assert client.get(STATUS_URL, params={"session_id": "real-mount"}).json() == {"running": False}
            ticket = mint_ticket(user_id="terminal-test", provider="basic")
            with client.websocket_connect(SHELL_URL + "?session_id=real-mount&ticket=" + ticket) as ws:
                ws.send_text('echo "mounted-$((7*6))"\n')
                read_until(ws, r"mounted-42")
            assert client.get(STATUS_URL, params={"session_id": "real-mount"}).json() == {"running": True}
        assert len(spawned) == 1 and not spawned[0].is_alive()
        assert svc._sessions == {}
    finally:
        _reset_for_tests()


def mount_plugin(home, monkeypatch):
    from hermes_cli import web_server, web_server_dashboard
    plugin = Path(__file__).resolve().parents[1]
    installed = home / "plugins" / "hermex-push"
    installed.parent.mkdir()
    installed.symlink_to(plugin, target_is_directory=True)
    config = home / "config.yaml"
    config.write_text(config.read_text() + '\nplugins:\n  enabled: [hermex-push]\n')
    # Only the app is replaced: discovery, trust checks, import and prefixing are Hermes's.
    app = FastAPI()
    app.state.auth_required = True
    app.state.bound_host = "testserver"
    app.state.bound_port = 443
    monkeypatch.setattr(web_server, "app", app)
    monkeypatch.setattr(web_server, "_dashboard_plugins_cache", None)
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    discovered = web_server._get_dashboard_plugins(force_rescan=True)
    entry = next(p for p in discovered if p["name"] == "hermex-push")
    assert entry["source"] == "user" and entry["_api_file"] == "plugin_api.py"
    web_server_dashboard._mount_plugin_api_routes()

    return app


@pytest.mark.parametrize("profile", ["other", "nosuch", "BAD/../x"])
def test_real_mount_rejects_profiles_before_lookup_or_secret_hydration(home, svc, monkeypatch, profile):
    from starlette.websockets import WebSocketDisconnect
    from hermes_cli import profiles
    import importlib

    (home / "profiles" / "other").mkdir(parents=True)
    (home / "profiles" / "other" / "config.yaml").write_text("{}")
    assert profiles.profile_exists("other")
    app = mount_plugin(home, monkeypatch)
    calls = []
    original = profiles.profile_exists
    def profile_exists(name):
        calls.append("profile lookup")
        return original(name)
    monkeypatch.setattr(profiles, "profile_exists", profile_exists)
    for module_name, attribute in [
        ("tui_gateway.launch_profile_policy", "activate_multi_profile_hosting"),
        ("hermes_cli.env_loader", "hydrate_profile_secret_sources"),
    ]:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue  # This host version has no profile-secret hydration module.
        if hasattr(module, attribute):
            monkeypatch.setattr(module, attribute, lambda *args, _name=attribute, **kwargs: calls.append(_name))
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(SHELL_URL + "?session_id=preauth&profile=" + profile):
                pytest.fail("Unauthenticated terminal accepted")
        assert closed.value.code == 4401
    assert calls == []


@pytest.mark.parametrize("mode,host_process,available", [
    ("in_process", None, True), ("host", None, False), ("in_process", "1", False),
])
def test_capabilities_only_offer_terminal_in_dashboard_process(monkeypatch, mode, host_process, available):
    import sys
    from types import ModuleType
    from terminal_support import api
    module = ModuleType("hermes_cli.plugin_isolation")
    module.ISOLATION_HOST = "host"
    module.HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"
    module.isolation_mode = lambda: mode
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_isolation", module)
    if host_process is None:
        monkeypatch.delenv(module.HOST_PROCESS_ENV, raising=False)
    else:
        monkeypatch.setenv(module.HOST_PROCESS_ENV, host_process)
    import json
    routes = set(json.loads(api.get_capabilities().body)["routes"])
    assert ("WS /terminal/shell" in routes) == available
    assert ("GET /terminal/shell/status" in routes) == available
    assert "GET /capabilities" in routes
