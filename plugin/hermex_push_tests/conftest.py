import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermex_push.keys import Keys  # noqa: E402
from hermex_push.relay import RelaySender  # noqa: E402


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Hermes also resolves the root from the OS user's home. Isolate both,
    # including when pytest's temp directory lives beneath the real ~/.hermes.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMEX_PUSH_RELAY_URL", raising=False)
    return home


@pytest.fixture
def keys():
    return Keys(install_key="ab" * 32, preview_key=bytes(range(32)))


class CapturingSender(RelaySender):
    """Records events instead of posting; delivery is synchronous and every event gets the
    relay answer ``result`` (a test sets ``no_activity`` for a session no phone watches)."""

    def __init__(self):
        super().__init__(post=lambda url, body: (200, "accepted"))
        self.events = []
        self.result = "accepted"

    def enqueue(self, url, event, on_result=None):
        self.events.append((url, event))
        if on_result is not None:
            on_result(self.result)
        return True


@pytest.fixture
def sender():
    return CapturingSender()
