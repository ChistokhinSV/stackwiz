"""KB write-back (central → source) via HTTP POST.

Scans kb-repo/_sources/<name>/ for local commits not yet written back,
bundles the tree as a tarball, and POSTs to the source's /.kb/push.

State tracking: the reconciler records the last write-back git-tree
SHA in kb-repo/.hub-writeback-state.json. On each reconcile it diffs
the current tree against the last-written one; if they differ, ship
a new tarball. The source-side handler unpacks + commits to its
local git, so upstream projects see the edits in their own history.

Failures are logged but not fatal — the next reconcile retries.
"""
from __future__ import annotations

import io
import json
import logging
import tarfile
from pathlib import Path

import httpx

from stackwiz_hub.models import RegistryDoc

log = logging.getLogger(__name__)


class WriteBackClient:
    def __init__(self, *, kb_repo: Path, timeout_s: int = 60) -> None:
        self.kb_repo = kb_repo
        self._client = httpx.Client(timeout=timeout_s, verify=False)
        self._state_path = kb_repo / ".hub-writeback-state.json"

    def close(self) -> None:
        self._client.close()

    def _load_state(self) -> dict[str, str]:
        try:
            return json.loads(self._state_path.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self, state: dict[str, str]) -> None:
        try:
            self._state_path.write_text(json.dumps(state, indent=2))
        except Exception as exc:  # noqa: BLE001
            log.warning("writeback state save: %s", exc)

    def maybe_push(self, doc: RegistryDoc, bearer: str | None) -> bool:
        """Push the source's tree if its content SHA has changed.

        Uses `git hash-object` on the tree (via `git write-tree` in a
        worktree-less call? — simpler: compute a content SHA via tar
        of the current tree and hash it). That's what the source's
        /.kb/health returns, but locally we don't have the source's
        algorithm; we approximate with a stable sha256 over sorted
        file list + contents, matching what stackwiz-kb-serve emits.
        """
        push_path = doc.endpoint.paths.get("push")
        if not push_path:
            # Source didn't advertise a push endpoint — central is
            # read-only for this source. Silently skip.
            return False
        source_dir = self.kb_repo / "_sources" / doc.name
        if not source_dir.is_dir():
            return False

        # State key: track the tree hash we last PUSHED. We only
        # re-push when the *local* tree diverges from that anchor —
        # the source's hash (set during pull) doesn't help because
        # an edit central → push → source-commit → next-pull will
        # change the source's hash and we'd ship the same bytes back
        # in a loop.
        current_hash = _tree_sha(source_dir)
        state = self._load_state()
        if state.get(doc.name) == current_hash:
            return False

        tarball = _tar_tree(source_dir)
        url = _join(doc.endpoint.url, push_path)
        headers = {"Content-Type": "application/x-tar"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        try:
            resp = self._client.post(url, content=tarball, headers=headers)
            if resp.status_code >= 400:
                log.warning(
                    "kb push %s: HTTP %d %s",
                    doc.name, resp.status_code, resp.text[:200],
                )
                return False
        except Exception as exc:  # noqa: BLE001
            log.warning("kb push %s: %s", doc.name, exc)
            return False

        state[doc.name] = current_hash
        self._save_state(state)
        log.info("kb pushed_back %s (hash=%s)", doc.name, current_hash[:12])
        return True


# --- helpers ---------------------------------------------------------------


def _join(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def _tree_sha(root: Path) -> str:
    """Content hash of a directory tree.

    Algorithm: sorted-by-path stream of (relpath|filesize|filesha256)
    — matches what stackwiz-kb-serve emits at /.kb/health. Stable
    across platforms; insensitive to mtime/permissions.
    """
    import hashlib

    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix().encode("utf-8")
        data = p.read_bytes()
        h.update(rel)
        h.update(b"|")
        h.update(str(len(data)).encode("ascii"))
        h.update(b"|")
        h.update(hashlib.sha256(data).digest())
        h.update(b"\n")
    return h.hexdigest()


def _tar_tree(root: Path) -> bytes:
    """Pack `root` into an in-memory tarball (uncompressed)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                tar.add(p, arcname=p.relative_to(root).as_posix())
    return buf.getvalue()
