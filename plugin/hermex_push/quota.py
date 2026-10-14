"""Provider account limits for Hermex's Usage screen (hermex#1237).

``GET /api/plugins/hermex-push/provider-quota`` answers in hermes-webui's ``/api/provider/quota``
envelope, so the app decodes one shape from either server. The limits come from hermes-agent's
``agent.account_usage.fetch_account_usage``, the source of the CLI's and gateway's ``/usage``
account lines, run inside the requested Profile's config and secret scope so its credentials are
that Profile's. The hermes-agent calls sit behind ``AgentHost``, imported only when a request
runs, so this module and its tests load without hermes-agent.
"""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# As webui's probe cache: a read within 45 s of the last good one is served from memory.
CACHE_TTL_S = 45.0
CACHE_MAX_ENTRIES = 64

# The providers hermes-agent fetches limits for itself (``_USAGE_FETCHERS``), with webui's names.
# Any other provider answers only through its provider plugin's ``fetch_account_usage`` hook.
BUILT_IN_PROVIDERS = {"openai-codex": "OpenAI Codex", "anthropic": "Anthropic", "openrouter": "OpenRouter"}


class AgentHost:
    """The hermes-agent calls the route makes, each inside the request's Profile scope."""

    def configured_provider(self) -> str:
        """The Profile's ``model.provider``; empty when unset or ``auto``, as the gateway reads it."""
        from hermes_cli.config import load_config_readonly

        model = load_config_readonly().get("model")
        provider = str(model.get("provider") or "").strip() if isinstance(model, dict) else ""
        return "" if provider.lower() == "auto" else provider

    def supports(self, provider: str) -> bool:
        """Whether hermes-agent can fetch limits for ``provider``: a built-in fetcher, or a provider
        plugin that overrides the base profile's no-op hook."""
        if provider in BUILT_IN_PROVIDERS:
            return True
        try:
            from providers import get_provider_profile
            from providers.base import ProviderProfile

            profile = get_provider_profile(provider)
        except Exception:
            return False
        return profile is not None and type(profile).fetch_account_usage is not ProviderProfile.fetch_account_usage

    def fetch(self, provider: str) -> Any:
        """An ``AccountUsageSnapshot`` or None. The agent resolves the credential itself and fails
        open to None; its HTTP calls are synchronous, which is why the route runs this on a thread."""
        from agent.account_usage import fetch_account_usage

        return fetch_account_usage(provider)


AGENT = AgentHost()


async def in_profile_scope(profile: str, fn: Callable[[], Any]) -> Any:
    """Runs ``fn`` on a worker thread inside ``profile``'s config and secret scope; an empty
    profile is the dashboard's own."""
    from hermes_cli.web_routers._common import config_scoped_to_thread

    return await config_scoped_to_thread(profile or None, fn)


class QuotaCache:
    """Serialized limits keyed by ``(profile, provider)``. A failed read never replaces a good entry,
    so a blip on refresh leaves the next plain read its last good answer."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], tuple[float, dict]] = {}

    def get(self, key: tuple[str, str]) -> Optional[dict]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if self._clock() - entry[0] > CACHE_TTL_S:
                del self._entries[key]
                return None
            return entry[1]

    def put(self, key: tuple[str, str], limits: Optional[dict]) -> None:
        if limits is None:
            return
        with self._lock:
            now = self._clock()
            self._entries[key] = (now, limits)
            for stale in [k for k, (at, _) in self._entries.items() if now - at > CACHE_TTL_S]:
                del self._entries[stale]
            while len(self._entries) > CACHE_MAX_ENTRIES:
                del self._entries[min(self._entries, key=lambda k: self._entries[k][0])]


def provider_quota(host: AgentHost, cache: QuotaCache, profile: str, provider: str, refresh: bool) -> dict:
    """The envelope for ``provider``, or the Profile's configured one when it is empty. Runs inside
    the Profile's scope. ``no_key`` and ``invalid_key`` never appear: the agent's fetchers answer
    None for both, as they do for a network failure."""
    provider = (provider or host.configured_provider()).strip().lower()
    if not provider:
        return _envelope(None, None, ok=False, supported=False, status="unavailable",
                         message="No active provider is configured.")
    name = BUILT_IN_PROVIDERS.get(provider) or provider.replace("-", " ").title()
    if not host.supports(provider):
        return _envelope(provider, name, ok=False, supported=False, status="unsupported",
                         message=f"Quota status is not available for {name}.")

    key = ((profile or "").strip(), provider)
    limits = None if refresh else cache.get(key)
    if limits is None:
        limits = serialize_snapshot(host.fetch(provider))
        cache.put(key, limits)

    if limits and limits["available"]:
        return _envelope(provider, name, ok=True, supported=True, status="available", label=limits["title"],
                         account_limits=limits, message=f"{name} account limits loaded.")
    reason = (limits or {}).get("unavailable_reason") or "Confirm provider authentication and try again."
    return _envelope(provider, name, ok=False, supported=True, status="unavailable", account_limits=limits,
                     message=f"{name} account limits are unavailable. {reason}")


def serialize_snapshot(snapshot: Any) -> Optional[dict]:
    """An ``AccountUsageSnapshot`` as webui's ``_serialize_account_usage_snapshot`` writes it."""
    if snapshot is None:
        return None
    windows = []
    for window in getattr(snapshot, "windows", ()) or ():
        label = _text(getattr(window, "label", None))
        if not label:
            continue
        used = _number(getattr(window, "used_percent", None))
        windows.append({
            "label": label,
            "used_percent": used,
            "remaining_percent": None if used is None else max(0.0, min(100.0, 100.0 - used)),
            "reset_at": _iso_utc(getattr(window, "reset_at", None)),
            "detail": _text(getattr(window, "detail", None)),
        })
    details = [text for detail in (getattr(snapshot, "details", ()) or ()) if (text := _text(detail))]
    reason = _text(getattr(snapshot, "unavailable_reason", None))
    return {
        "provider": _text(getattr(snapshot, "provider", None)),
        "source": _text(getattr(snapshot, "source", None)),
        "title": _text(getattr(snapshot, "title", None)) or "Account limits",
        "plan": _text(getattr(snapshot, "plan", None)),
        "windows": windows,
        "details": details,
        "available": bool(getattr(snapshot, "available", bool(windows or details))) and not reason,
        "unavailable_reason": reason,
        "fetched_at": _iso_utc(getattr(snapshot, "fetched_at", None)),
    }


def _envelope(provider: Optional[str], name: Optional[str], *, ok: bool, supported: bool, status: str,
              message: str, label: Optional[str] = None, account_limits: Optional[dict] = None) -> dict:
    body = {"ok": ok, "provider": provider, "display_name": name, "supported": supported, "status": status,
            "quota": None, "message": message}
    if label:
        body["label"] = label
    if account_limits is not None:
        body["account_limits"] = account_limits
    return body


def _text(value: Any) -> Optional[str]:
    return str(value or "").strip() or None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _iso_utc(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return _text(value)
