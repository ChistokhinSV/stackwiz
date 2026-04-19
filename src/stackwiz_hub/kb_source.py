"""KB content pull (source → central) via HTTP.

Speaks to an endpoint that implements the stackwiz-kb-serve contract:

    GET  /.kb/health    -> {"hash": "<sha256 of tree>", "files": <int>}
    GET  /.kb/snapshot  -> application/x-tar stream of the tree

The hub caches the last-seen hash per source; pulls only when the
source's hash changes. New content is extracted into
{kb_repo}/_sources/<name>/ replacing whatever was there, then git-
committed by the reconciler's outer loop.
"""
from __future__ import annotations

import hashlib
import io
import logging
import shutil
import tarfile
from pathlib import Path

import httpx

from stackwiz_hub.models import RegistryDoc

log = logging.getLogger(__name__)


class KBSourceClient:
    def __init__(self, *, kb_repo: Path, timeout_s: int = 60) -> None:
        self.kb_repo = kb_repo
        self._client = httpx.Client(timeout=timeout_s, verify=False)
        # hash-cache lives in-memory so a hub restart re-pulls every
        # source once — idempotent but marginally wasteful. Persisting
        # to {kb_repo}/.hub-state.json would avoid the re-pull; left
        # as an optimisation for Phase 2.1.
        self._last_hash: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def remote_hash(self, doc: RegistryDoc, bearer: str | None) -> str | None:
        """GET /.kb/health; return the hash field or None on error."""
        health = doc.endpoint.paths.get("health", "/.kb/health")
        url = _join(doc.endpoint.url, health)
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        try:
            resp = self._client.get(url, headers=headers)
            if resp.status_code != 200:
                log.warning("kb health %s: HTTP %d", doc.name, resp.status_code)
                return None
            return resp.json().get("hash")
        except Exception as exc:  # noqa: BLE001
            log.warning("kb health %s: %s", doc.name, exc)
            return None

    def pull_if_changed(self, doc: RegistryDoc, bearer: str | None) -> bool:
        """Fetch snapshot only when the source's hash differs from our cache.

        Returns True when content was replaced. Signals to the
        reconciler that a git commit is needed.
        """
        remote = self.remote_hash(doc, bearer)
        if remote is None:
            return False
        if self._last_hash.get(doc.name) == remote:
            return False

        snapshot_path = doc.endpoint.paths.get("pull", "/.kb/snapshot")
        url = _join(doc.endpoint.url, snapshot_path)
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        target = self.kb_repo / "_sources" / doc.name

        try:
            resp = self._client.get(url, headers=headers)
            if resp.status_code != 200:
                log.warning(
                    "kb snapshot %s: HTTP %d", doc.name, resp.status_code,
                )
                return False
            _replace_tree_from_tar(target, resp.content)
        except Exception as exc:  # noqa: BLE001
            log.warning("kb snapshot %s: %s", doc.name, exc)
            return False

        self._last_hash[doc.name] = remote
        log.info("kb synced %s (hash=%s) -> %s", doc.name, remote[:12], target)
        return True


# --- helpers ---------------------------------------------------------------


def _join(base: str, path: str) -> str:
    """Robust URL join — no surprises from urljoin's scheme-interaction."""
    return base.rstrip("/") + "/" + path.lstrip("/")


def _replace_tree_from_tar(target: Path, tar_bytes: bytes) -> None:
    """Overwrite `target` with the contents of the tarball.

    Atomic-ish: new content extracted alongside, then swapped in.
    Leaves no half-written state even if the extract fails.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".staging")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
        # Reject anything that escapes the staging dir — defence
        # against a malicious source putting ../../etc/shadow in its
        # tarball. Can't use .lstrip("./") for normalisation because
        # it also strips a leading "." from "../escape" — use
        # Path.parts directly which treats ".." explicitly.
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if ".." in parts or Path(member.name).is_absolute():
                raise ValueError(f"refusing unsafe tar path: {member.name!r}")
        # Python 3.14+ requires `filter=`; "data" rejects device
        # files and re-validates member paths. Safe for tree content.
        tar.extractall(staging, filter="data")  # noqa: S202
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
