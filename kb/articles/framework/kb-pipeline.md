---
title: "Framework: KB pipeline"
description: How content flows from per-stack kb/ trees into the central kb-repo and back.
tags: framework, stackwiz, kb, kb-mcp, kb-serve, knowledge-base
---

# KB pipeline

The lab's chat agent (077's LibreChat + kb-mcp) answers from a
**single git-backed knowledge base** at `/data/kb-repo`. Per-stack
KB content flows in from satellite sources + from framework docs,
and operator edits flow back out.

## Layout of the central KB

```
/data/kb-repo/                          # docker volume on 077's host
  README.md
  articles/
    framework/                          # from 079 image (this article)
    kb-agent/                           # from 077's kb fixture
    platform/                           # from 077's seed
    hosts/                              # auto-generated per host
  runbooks/
  troubleshooting/
  architecture/
    adr/
  _sources/                             # satellites pulled by the hub
    awx-kb/                             # from 061 (via kb-source registry)
    graylog-kb/                         # from 081
    config-analyzer-kb/                 # from 082
  inbox/                                # GITIGNORED — drop-zone for ingest
  tftp/                                 # GITIGNORED — firmware (not content)
  .gitignore                            # .kb/, inbox/, tftp/
```

kb-mcp indexes everything under `/data/kb-repo` (excluding
gitignored paths) and serves semantic search + article CRUD to
LibreChat.

## Three pathways in

### A. Per-stack fixtures (seed-kb.sh)

077's `deploy/seed-kb.sh` rsyncs
`077/kb/{articles,architecture,runbooks,troubleshooting,agent-memories}`
into `/kb` on first install (or `--force`). Content is the 077
repo's own kb/ tree — written by hand, committed to the repo,
version-controlled.

### B. Framework articles (079 image)

`install/kb-seed.sh` does `docker cp` of `/framework-kb/` out of
the stackwiz engine image into `/opt/stackwiz/framework-kb` on
host. `seed-kb.sh` mounts that dir read-only into the alpine seed
container and rsyncs into `/kb/articles/framework/`. Always
overwrites — framework-owned content, safe to refresh.

**This article ships via pathway B.** Bump the stackwiz image
tag → next kb-seed refreshes.

### C. Satellite sources (stackwiz-hub)

Other stacks (061 AWX, 081 graylog, 082 analyzer) declare
`registry.kind: kb-source` entries (see
[registry-and-hub](registry-and-hub.md)). Each exposes three HTTP
endpoints via the `stackwiz_kb_serve` framework library:

- `GET /.kb/health` — returns `{hash, files}` (content hash).
- `GET /.kb/snapshot` — tarball of the tree.
- `POST /.kb/push` — accepts a tarball, unpacks + git commits.

stackwiz-hub's `kb_source.pull_if_changed` checks the health hash
every reconcile cycle; when it differs, pulls the snapshot and
unpacks into `/kb/repo/_sources/<name>/`.

## Write-back

kb-mcp commits from operator edits (LibreChat asks, agent
writes, etc.) land in the central git repo. The hub's
`write_back.maybe_push` scans each `_sources/<name>/` dir for new
commits and POSTs the tarball back to the source's `/.kb/push`.

The source's `stackwiz_kb_serve` router:

1. Verifies the bearer (from env var, supplied by the component's
   `vault_runtime:` token file or compose env).
2. Unpacks the tarball safely (path-traversal guards,
   Python-3.14 `filter="data"`).
3. Optionally `git add -A && git commit` in the source's dir.

Transport is HTTP over `stackwiz-shared` docker network —
authenticated with a per-source bearer read from Vault. No SSH
keys, no bare git repos.

## Gitignores

`seed-kb.sh` ensures `.gitignore` on the kb-repo volume contains:

- `.kb/` — kb-mcp embedding cache.
- `inbox/` — kb-ingest drop-zone; files move out to
  `documents/` after processing, so tracking them clutters history.
- `tftp/` — defensive, in case operator symlinks the TFTP export.

Plus an idempotent `git rm -r --cached --ignore-unmatch inbox/
tftp/ .kb/` to untrack anything committed in the past.

## Wiki.js sync

Wiki.js Git storage module pulls from
`/data/kb-bare.git` (bare repo on same host). kb-mcp's
post-commit hook pushes to that bare repo. So:

1. Operator / agent edits article via FileBrowser or kb-mcp.
2. kb-mcp commits to `/data/kb-repo`.
3. Post-commit pushes to `/data/kb-bare.git`.
4. Wiki.js pulls from bare repo on its schedule (or instant-sync
   via `storage.executeAction(git,sync)` mutation).

## Who calls what

| Actor | Responsibility |
|-------|-----------------|
| 077 kb-mcp | Source of truth for `/data/kb-repo`. Indexes + commits. |
| 077 kb-filebrowser | Web UI; writes via umask 0000 for kb-mcp's uid 10001. |
| 077 kb-ingest | Watches `inbox/`, converts to `.md` in `documents/`. |
| 077 kb-wikijs | Renders the repo as a browsable wiki. |
| stackwiz-hub | Reconciles kb-source registry + pulls tarballs + pushes back commits. |
| Per-stack kb-serve (lib or sidecar) | Exposes `/.kb/{health,snapshot,push}` for the hub. |

See also: troubleshooting/kb-sync.md.
