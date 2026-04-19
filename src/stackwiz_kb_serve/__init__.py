"""stackwiz-kb-serve — source-side HTTP endpoints for the kb-hub protocol.

A tiny FastAPI router that any KB-serving container mounts to expose
its content to the framework's stackwiz-hub daemon. Three endpoints:

    GET  /.kb/health    -> {"hash": "<sha256 of tree>", "files": <int>}
    GET  /.kb/snapshot  -> application/x-tar streaming tarball
    POST /.kb/push      -> unpack client tarball into kb_dir + git commit

Replaces the SSH/git-over-ssh transport that kb-source-sync used in
earlier stackwiz generations. HTTP with bearer auth, one dependency
(FastAPI), no system user / authorized_keys / bare repos.

Usage:

    from fastapi import FastAPI
    from stackwiz_kb_serve import kb_router

    app = FastAPI()
    app.include_router(kb_router(
        kb_dir="/opt/graylog/kb",
        bearer_env="MCP_BEARER_TOKEN",
        git_commit_on_push=True,
    ))
"""

from stackwiz_kb_serve.router import kb_router, tree_sha

__all__ = ["kb_router", "tree_sha", "__version__"]
__version__ = "0.1.0"
