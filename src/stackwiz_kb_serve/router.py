"""FastAPI router implementing the kb-hub source-side contract.

Content hash algorithm is identical to stackwiz_hub.write_back._tree_sha
so pull/push hashes compare directly. Both sides call
`_tree_sha` → sha256( sorted(relpath | len | sha256(content) \n) ).
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import subprocess
import tarfile
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger(__name__)


# --- content hash (shared with stackwiz_hub.write_back) --------------------


def tree_sha(root: Path) -> str:
    """Stable content hash of a directory tree.

    Must match stackwiz_hub.write_back._tree_sha byte-for-byte —
    both sides key dedup off it. Algorithm: for every file under root,
    sorted by relpath, emit:  <relpath>|<len>|<sha256(content)>\\n
    and sha256 the concatenation.
    """
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


# --- router factory --------------------------------------------------------


def kb_router(
    *,
    kb_dir: str | Path,
    bearer_env: str | None = None,
    git_commit_on_push: bool = True,
    git_author_name: str = "stackwiz-hub",
    git_author_email: str = "stackwiz-hub@local",
) -> APIRouter:
    """Build a router exposing GET health/snapshot + POST push.

    Args:
        kb_dir: Directory to serve. The router reads on GET, writes on POST.
        bearer_env: Env var holding the expected bearer token. When None,
                   endpoints are open. When set but the env is empty, the
                   router refuses to serve (misconfigured — better 500 than
                   silent open).
        git_commit_on_push: If True, run `git add -A && git commit -m ...`
                           in kb_dir after a successful push.
        git_author_name, git_author_email: Committer identity.
    """
    root = Path(kb_dir).resolve()
    router = APIRouter(tags=["kb"])

    def _check_bearer(auth_header: str | None) -> None:
        if bearer_env is None:
            return
        expected = os.environ.get(bearer_env, "")
        if not expected:
            # Configured to require a bearer but env is empty — fail
            # closed. Returning 500 rather than 401 nudges the operator
            # to fix the env rather than chasing an auth issue.
            raise HTTPException(
                status_code=500,
                detail=(
                    f"server misconfigured: {bearer_env} env var is empty "
                    "but bearer auth is enabled"
                ),
            )
        got = (auth_header or "").removeprefix("Bearer ").strip()
        if got != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

    @router.get("/.kb/health")
    def health(authorization: str | None = Header(None)) -> JSONResponse:
        _check_bearer(authorization)
        if not root.is_dir():
            return JSONResponse({"hash": "", "files": 0}, status_code=200)
        files = sum(1 for p in root.rglob("*") if p.is_file())
        return JSONResponse({"hash": tree_sha(root), "files": files})

    @router.get("/.kb/snapshot")
    def snapshot(authorization: str | None = Header(None)) -> StreamingResponse:
        _check_bearer(authorization)
        if not root.is_dir():
            raise HTTPException(status_code=404, detail="kb dir missing")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    tar.add(p, arcname=p.relative_to(root).as_posix())
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="application/x-tar",
            headers={
                "Content-Disposition": f'attachment; filename="{root.name}.tar"',
            },
        )

    @router.post("/.kb/push", status_code=204)
    async def push(
        request: Request,
        authorization: str | None = Header(None),
    ) -> Response:
        _check_bearer(authorization)
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="empty body")
        root.mkdir(parents=True, exist_ok=True)
        try:
            _extract_safely(body, root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if git_commit_on_push and (root / ".git").is_dir():
            _commit_changes(root, git_author_name, git_author_email)
        return Response(status_code=204)

    return router


# --- helpers ---------------------------------------------------------------


def _extract_safely(tar_bytes: bytes, target: Path) -> None:
    """Unpack a tarball into `target` with path-traversal guarding."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if ".." in parts or Path(member.name).is_absolute():
                raise ValueError(f"unsafe tar path: {member.name!r}")
        # Python 3.14+ requires filter=; "data" rejects device files
        # and re-validates member paths (defence-in-depth).
        tar.extractall(target, filter="data")  # noqa: S202


def _commit_changes(
    repo: Path,
    author_name: str,
    author_email: str,
) -> None:
    """Run `git add -A && git commit` — silently swallow if tree is clean."""
    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True,
        )

    try:
        _git("add", "-A")
        # Skip the commit if staging is empty (idempotent re-push of
        # identical content).
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", "--staged", "--quiet"],
            check=False,
        )
        if diff.returncode == 0:
            return
        _git(
            "-c", f"user.name={author_name}",
            "-c", f"user.email={author_email}",
            "commit", "-q", "-m", "kb-hub: push from central",
        )
    except subprocess.CalledProcessError as exc:
        log.warning("git commit failed: %s", exc.stderr.decode("utf-8", "replace"))
