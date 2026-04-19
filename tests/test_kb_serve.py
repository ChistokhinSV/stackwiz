"""Tests for stackwiz-kb-serve's HTTP endpoints.

Covers:
  * /.kb/health returns a stable hash that matches tree_sha.
  * /.kb/snapshot returns a tarball containing every file.
  * /.kb/push unpacks a tarball, commits to git, refuses path traversal.
  * Bearer enforcement (401 on missing/wrong, 500 on missing env).
  * Round-trip: pull_if_changed equivalent → push → same hash.
"""
from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from stackwiz_kb_serve import kb_router, tree_sha


@pytest.fixture
def kb_tree(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    (root / "readme.md").write_text("hello")
    (root / "sub").mkdir()
    (root / "sub" / "deep.md").write_text("deeper")
    return root


@pytest.fixture
def app_client(kb_tree: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("KB_BEARER", "secret-xyz")
    app = FastAPI()
    app.include_router(kb_router(
        kb_dir=kb_tree,
        bearer_env="KB_BEARER",
        git_commit_on_push=False,  # avoid git dep in tests unless explicit
    ))
    return TestClient(app)


def _auth(token: str = "secret-xyz") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- /.kb/health -----------------------------------------------------------


def test_health_returns_hash_and_count(app_client: TestClient, kb_tree: Path) -> None:
    resp = app_client.get("/.kb/health", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert data["files"] == 2
    assert data["hash"] == tree_sha(kb_tree)


def test_health_requires_bearer(app_client: TestClient) -> None:
    assert app_client.get("/.kb/health").status_code == 401
    assert app_client.get(
        "/.kb/health", headers=_auth("wrong"),
    ).status_code == 401


def test_health_hash_changes_on_edit(
    app_client: TestClient, kb_tree: Path,
) -> None:
    h1 = app_client.get("/.kb/health", headers=_auth()).json()["hash"]
    (kb_tree / "readme.md").write_text("CHANGED")
    h2 = app_client.get("/.kb/health", headers=_auth()).json()["hash"]
    assert h1 != h2


# --- /.kb/snapshot ---------------------------------------------------------


def test_snapshot_returns_full_tree(app_client: TestClient) -> None:
    resp = app_client.get("/.kb/snapshot", headers=_auth())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-tar"
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r") as tar:
        names = sorted(tar.getnames())
    assert names == ["readme.md", "sub/deep.md"]


def test_snapshot_missing_kb_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_BEARER", "t")
    app = FastAPI()
    app.include_router(kb_router(
        kb_dir=tmp_path / "never",
        bearer_env="KB_BEARER",
    ))
    with TestClient(app) as c:
        assert c.get("/.kb/snapshot", headers={"Authorization": "Bearer t"}).status_code == 404


# --- /.kb/push -------------------------------------------------------------


def _tar_of(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_push_writes_files(app_client: TestClient, kb_tree: Path) -> None:
    tar = _tar_of({"new.md": "fresh", "sub/existing.md": "rewritten"})
    resp = app_client.post(
        "/.kb/push", content=tar,
        headers={**_auth(), "content-type": "application/x-tar"},
    )
    assert resp.status_code == 204
    assert (kb_tree / "new.md").read_text() == "fresh"
    assert (kb_tree / "sub" / "existing.md").read_text() == "rewritten"


def test_push_rejects_path_traversal(app_client: TestClient) -> None:
    tar = _tar_of({"../escape.md": "evil"})
    resp = app_client.post(
        "/.kb/push", content=tar,
        headers={**_auth(), "content-type": "application/x-tar"},
    )
    assert resp.status_code == 400
    assert "unsafe tar path" in resp.json()["detail"]


def test_push_rejects_empty_body(app_client: TestClient) -> None:
    resp = app_client.post(
        "/.kb/push", content=b"",
        headers={**_auth(), "content-type": "application/x-tar"},
    )
    assert resp.status_code == 400


def test_push_requires_bearer(app_client: TestClient) -> None:
    tar = _tar_of({"x.md": "x"})
    resp = app_client.post(
        "/.kb/push", content=tar,
        headers={"content-type": "application/x-tar"},
    )
    assert resp.status_code == 401


# --- bearer config sanity --------------------------------------------------


def test_server_refuses_when_bearer_env_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bearer_env configured but env var is empty → 500 (fail closed)."""
    monkeypatch.delenv("KB_BEARER", raising=False)
    app = FastAPI()
    app.include_router(kb_router(kb_dir=tmp_path, bearer_env="KB_BEARER"))
    with TestClient(app) as c:
        # Sending any bearer triggers the check; env is empty → 500.
        resp = c.get("/.kb/health", headers={"Authorization": "Bearer whatever"})
        assert resp.status_code == 500
        assert "misconfigured" in resp.json()["detail"]


def test_open_mode_no_bearer(tmp_path: Path) -> None:
    """bearer_env=None → endpoints served anonymously."""
    app = FastAPI()
    app.include_router(kb_router(kb_dir=tmp_path, bearer_env=None))
    with TestClient(app) as c:
        assert c.get("/.kb/health").status_code == 200


# --- end-to-end round-trip (hub pull <-> source push) ----------------------


def test_round_trip_push_then_health_matches_hash(
    app_client: TestClient, kb_tree: Path,
) -> None:
    """A push + health must reflect the pushed content: the hub can then
    verify its write-back landed by comparing its local SHA to the
    source's post-push /.kb/health hash."""
    tar = _tar_of({"readme.md": "pushed-content", "sub/deep.md": "also-pushed"})
    app_client.post(
        "/.kb/push", content=tar,
        headers={**_auth(), "content-type": "application/x-tar"},
    )
    new_hash = app_client.get("/.kb/health", headers=_auth()).json()["hash"]
    assert new_hash == tree_sha(kb_tree)
    assert (kb_tree / "readme.md").read_text() == "pushed-content"


# --- git commit integration (requires git on PATH) -------------------------


def test_push_commits_when_git_enabled(
    kb_tree: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When git_commit_on_push=True and kb_dir is a git repo, a push
    lands as a commit."""
    # Skip cleanly if git isn't available (Windows dev without git on
    # PATH).
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git not on PATH")

    # Initialise kb_tree as a git repo.
    subprocess.run(["git", "-C", str(kb_tree), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(kb_tree), "-c", "user.name=t",
         "-c", "user.email=t@t", "commit", "--allow-empty", "-q",
         "-m", "init"],
        check=True,
    )

    monkeypatch.setenv("KB_BEARER", "s")
    app = FastAPI()
    app.include_router(kb_router(
        kb_dir=kb_tree,
        bearer_env="KB_BEARER",
        git_commit_on_push=True,
    ))
    client = TestClient(app)

    tar = _tar_of({"new-file.md": "from-push"})
    resp = client.post(
        "/.kb/push", content=tar,
        headers={"Authorization": "Bearer s", "content-type": "application/x-tar"},
    )
    assert resp.status_code == 204

    # Verify the commit landed.
    log_out = subprocess.run(
        ["git", "-C", str(kb_tree), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "kb-hub" in log_out
