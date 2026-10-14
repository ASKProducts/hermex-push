"""The git routes (hermex#1236) against a temp repo whose ``origin`` is a local bare remote. They
run git through hermes-agent's own helpers, so every case that reaches git skips where
hermes-agent isn't importable, as ``test_restart_needs_the_dashboard_sign_in`` does."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/plugins/hermex-push"


@pytest.fixture
def client():
    spec = importlib.util.spec_from_file_location("hermex_push_git_under_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = api
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    return TestClient(app)


@pytest.fixture
def host():
    pytest.importorskip("hermes_cli.web_git")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-qm", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path):
    """``(local, upstream)``: two clones of one bare remote, each on ``main`` with one commit."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    upstream = tmp_path / "upstream"
    git(tmp_path, "clone", "-q", str(remote), str(upstream))
    git(upstream, "switch", "-qc", "main")
    commit(upstream, "first")
    git(upstream, "push", "-q", "-u", "origin", "main")
    local = tmp_path / "local"
    git(tmp_path, "clone", "-q", str(remote), str(local))
    return local, upstream


def test_fetch_updates_the_remote_ref(client, host, repos):
    local, upstream = repos
    head = commit(upstream, "second")
    git(upstream, "push", "-q")

    response = client.post(f"{PREFIX}/git/fetch", json={"path": str(local)})
    assert response.status_code == 200 and response.json()["ok"] is True
    assert git(local, "rev-parse", "origin/main") == head
    assert git(local, "rev-parse", "HEAD") != head


def test_pull_fast_forwards(client, host, repos):
    local, upstream = repos
    head = commit(upstream, "second")
    git(upstream, "push", "-q")

    response = client.post(f"{PREFIX}/git/pull", json={"path": str(local), "remote": "origin"})
    assert response.status_code == 200 and response.json()["ok"] is True
    assert git(local, "rev-parse", "HEAD") == head


def test_a_diverged_pull_is_refused_without_a_merge_commit(client, host, repos):
    local, upstream = repos
    commit(upstream, "theirs")
    git(upstream, "push", "-q")
    ours = commit(local, "ours")

    response = client.post(f"{PREFIX}/git/pull", json={"path": str(local)})
    assert response.status_code == 400 and response.json()["detail"]
    assert git(local, "rev-parse", "HEAD") == ours


def test_branch_creates_and_switches_carrying_uncommitted_changes(client, host, repos):
    local, _ = repos
    (local / "first").write_text("edited")

    response = client.post(f"{PREFIX}/git/branch", json={"path": str(local), "name": "feature/x"})
    assert response.status_code == 200 and response.json()["ok"] is True
    assert git(local, "branch", "--show-current") == "feature/x"
    assert (local / "first").read_text() == "edited"

    assert client.post(f"{PREFIX}/git/branch", json={"path": str(local), "name": "feature/x"}).status_code == 400


def test_option_like_arguments_are_refused_before_git_runs(client, tmp_path):
    """Needs no hermes-agent: the refusal comes before the host's helpers are imported."""
    for route, body in (
        ("fetch", {"path": str(tmp_path), "remote": "--upload-pack=touch pwned"}),
        ("pull", {"path": str(tmp_path), "remote": "-q"}),
        ("branch", {"path": str(tmp_path), "name": "x", "start": "--orphan"}),
    ):
        response = client.post(f"{PREFIX}/git/{route}", json=body)
        assert response.status_code == 400, route
    assert not (tmp_path / "pwned").exists()


def test_a_branch_name_the_host_would_rewrite_is_refused(client, host, repos):
    local, _ = repos
    for name in ("-b", "--force", "a b", "a..b", "x/"):
        response = client.post(f"{PREFIX}/git/branch", json={"path": str(local), "name": name})
        assert response.status_code == 400, name
    assert git(local, "branch", "--show-current") == "main"
