"""The provider-quota route: webui's envelope for each status, the Profile scope, and the cache."""
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermex_push import quota

ROOT = Path(__file__).resolve().parents[1]
URL = "/api/plugins/hermex-push/provider-quota"


def _window(label, used, reset_at=None, detail=None):
    return SimpleNamespace(label=label, used_percent=used, reset_at=reset_at, detail=detail)


def _snapshot(provider="openai-codex", windows=(), details=(), plan=None, unavailable_reason=None):
    return SimpleNamespace(provider=provider, source="usage_api", title="Account limits", plan=plan, windows=windows,
                           details=details, unavailable_reason=unavailable_reason,
                           available=bool(windows or details) and not unavailable_reason,
                           fetched_at=datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc))


CODEX = _snapshot(windows=(_window("Session", 42.5, datetime(2026, 10, 13, 17, 0, tzinfo=timezone.utc)),
                           _window("Weekly", 7)), details=("Credits balance: $5.00",), plan="Plus")


class FakeHost:
    """hermes-agent as the route sees it: ``answers`` maps a provider to the snapshots it returns in turn."""

    def __init__(self, configured="openai-codex", answers=None, hooked=()):
        self.configured, self.answers, self.hooked, self.fetched = configured, dict(answers or {}), set(hooked), []

    def configured_provider(self):
        return self.configured

    def supports(self, provider):
        return provider in quota.BUILT_IN_PROVIDERS or provider in self.hooked

    def fetch(self, provider):
        self.fetched.append(provider)
        answers = self.answers.get(provider) or [None]
        return answers.pop(0) if len(answers) > 1 else answers[0]


@pytest.fixture
def route(monkeypatch):
    """The route with ``host`` standing in for hermes-agent and the scope recording each Profile."""
    host, scopes = FakeHost(), []

    async def scope(profile, fn):
        scopes.append(profile)
        return fn()

    monkeypatch.setattr(quota, "AGENT", host)
    monkeypatch.setattr(quota, "in_profile_scope", scope)
    spec = importlib.util.spec_from_file_location("hermex_push_quota_under_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = api
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/hermex-push")
    return SimpleNamespace(client=TestClient(app), host=host, scopes=scopes)


def test_available_limits_in_webuis_envelope(route):
    route.host.answers = {"openai-codex": [CODEX]}
    response = route.client.get(URL, params={"provider": "openai-codex", "profile": "work"})

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "ok": True, "provider": "openai-codex", "display_name": "OpenAI Codex", "supported": True,
        "status": "available", "label": "Account limits", "quota": None,
        "message": "OpenAI Codex account limits loaded.",
        "account_limits": {
            "provider": "openai-codex", "source": "usage_api", "title": "Account limits", "plan": "Plus",
            "windows": [
                {"label": "Session", "used_percent": 42.5, "remaining_percent": 57.5,
                 "reset_at": "2026-10-13T17:00:00Z", "detail": None},
                {"label": "Weekly", "used_percent": 7.0, "remaining_percent": 93.0, "reset_at": None, "detail": None},
            ],
            "details": ["Credits balance: $5.00"], "available": True, "unavailable_reason": None,
            "fetched_at": "2026-10-13T12:00:00Z",
        },
    }
    assert route.scopes == ["work"]


def test_a_snapshot_with_a_reason_and_no_snapshot_are_unavailable(route):
    refused = _snapshot("anthropic", unavailable_reason="Only OAuth-backed Claude accounts have limits.")
    route.host.answers = {"anthropic": [refused], "openrouter": [None]}

    body = route.client.get(URL, params={"provider": "anthropic"}).json()
    assert (body["ok"], body["status"], body["supported"]) == (False, "unavailable", True)
    assert body["message"] == "Anthropic account limits are unavailable. Only OAuth-backed Claude accounts have limits."
    assert body["account_limits"]["available"] is False

    body = route.client.get(URL, params={"provider": "openrouter"}).json()
    assert (body["status"], body["message"]) == (
        "unavailable", "OpenRouter account limits are unavailable. Confirm provider authentication and try again.")
    assert "account_limits" not in body


def test_a_provider_without_a_fetcher_or_hook_is_unsupported_and_never_fetched(route):
    route.host.hooked = {"acme-cloud"}
    route.host.answers = {"acme-cloud": [_snapshot("acme-cloud", windows=(_window("Monthly", 10),))]}

    body = route.client.get(URL, params={"provider": "deepseek"}).json()
    assert (body["ok"], body["supported"], body["status"], body["display_name"]) == (
        False, False, "unsupported", "Deepseek")
    assert route.host.fetched == []

    assert route.client.get(URL, params={"provider": "acme-cloud"}).json()["status"] == "available"


def test_an_omitted_provider_is_the_profiles_configured_one(route):
    route.host.configured = "Anthropic"
    route.host.answers = {"anthropic": [_snapshot("anthropic", windows=(_window("Current week", 20),))]}

    body = route.client.get(URL, params={"profile": "work"}).json()
    assert (body["provider"], body["status"]) == ("anthropic", "available")

    route.host.configured = ""
    body = route.client.get(URL).json()
    assert (body["provider"], body["status"], body["message"]) == (None, "unavailable", "No active provider is configured.")
    assert route.scopes == ["work", ""]


def test_a_read_within_45_s_is_cached_per_profile_and_refresh_skips_it(route):
    route.host.answers = {"openai-codex": [CODEX]}

    for _ in range(2):
        route.client.get(URL, params={"provider": "openai-codex", "profile": "work"})
    assert route.host.fetched == ["openai-codex"]

    route.client.get(URL, params={"provider": "openai-codex", "profile": "home"})
    route.client.get(URL, params={"provider": "openai-codex", "profile": "work", "refresh": "1"})
    assert route.host.fetched == ["openai-codex"] * 3


def test_a_failed_read_keeps_the_last_good_limits(route):
    route.host.answers = {"openai-codex": [CODEX, None]}
    params = {"provider": "openai-codex"}

    assert route.client.get(URL, params=params).json()["status"] == "available"
    assert route.client.get(URL, params={**params, "refresh": "1"}).json()["status"] == "unavailable"
    assert route.client.get(URL, params=params).json()["status"] == "available"
    assert len(route.host.fetched) == 2


def test_the_cache_expires_and_keeps_at_most_64_entries():
    now = [0.0]
    cache = quota.QuotaCache(clock=lambda: now[0])
    cache.put(("", "anthropic"), {"available": True})
    now[0] = 45.5
    assert cache.get(("", "anthropic")) is None

    for index in range(quota.CACHE_MAX_ENTRIES + 1):
        now[0] += 1
        cache.put((str(index), "openai-codex"), {"available": True})
    assert cache.get(("0", "openai-codex")) is None and cache.get(("64", "openai-codex")) is not None


def test_capabilities_lists_the_route(route):
    routes = route.client.get("/api/plugins/hermex-push/capabilities").json()["routes"]
    assert "GET /provider-quota" in routes


def test_the_agent_host_knows_the_built_in_fetchers():
    """Against hermes-agent when it is importable: the fetchers this module names are the agent's."""
    account_usage = pytest.importorskip("agent.account_usage")
    assert set(quota.BUILT_IN_PROVIDERS) == set(account_usage._USAGE_FETCHERS)
    assert not quota.AgentHost().supports("not-a-provider")
