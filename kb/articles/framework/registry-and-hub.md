---
title: "Framework: Registry + Hub"
description: Declarative cross-stack discovery — components write registry entries, stackwiz-hub reconciles them into MCPJungle / kb-repo.
tags: framework, stackwiz, registry, stackwiz-hub, mcpjungle, cross-stack
---

# Registry + Hub

Cross-stack discovery used to be a pile of conventions: docker
labels, consul tag strings, Vault paths, SSH keys. That led to
nine silent-failure surfaces (wrong label name → MCP never
registered; bearer at wrong Vault path → 401 on handshake; etc.).

The replacement is a **declarative registry** + one **hub daemon**.

## Registry schema

One YAML shape regardless of what's being discovered:

```yaml
registry:
  - kind: mcp-server            # or `kb-source`
    name: graylog-mcp           # unique within kind
    endpoint_url: http://graylog-mcp:8000/mcp
    transport: streamable_http  # http | streamable_http | sse
    paths:                      # only for kb-source
      pull: /.kb/snapshot
      push: /.kb/push
      health: /.kb/health
    bearer_secret: mcp_bearer_token   # refers to `secrets:` block
    tags: [logging, ops, ai-agent]
    description: "Graylog log queries"
```

`src/stackwiz/manifest.py:RegistryEntry` defines the shape.

## What the engine writes

For each entry, `engine._publish_registry`:

1. **Vault config doc** at
   `stackwiz/data/registry/<kind>/<name>/config` — the full
   RegistryEntry as JSON + `schema_version: 1` + owner (manifest
   name) + `component_id`.
2. **Vault bearer** at `stackwiz/data/registry/<kind>/<name>/token`
   (only when `bearer_secret:` set).
3. **Consul KV pointer** at
   `stackwiz/registry/<kind>/<name>` — a small JSON
   `{config_vault_path: ...}` so the hub can watch with one
   blocking query.

## stackwiz-hub reconcile loop

`src/stackwiz_hub/` is a framework-shipped daemon
(`ghcr.io/chistokhinsv/stackwiz-hub:latest`). One instance per
host. Deployed via `share/stackwiz-hub.sh` — consumers or the
framework's own bring-up call `stackwiz_hub_ensure`.

```python
while True:
    pointers = consul.watch("stackwiz/registry/", blocking=True)
    for p in pointers:
        doc = vault.read(p.config_vault_path)
        bearer = vault.read(doc.auth.token_ref) if doc.auth.mode == "bearer" else None
        if doc.kind == "mcp-server":
            mcpjungle.upsert_server(doc, bearer)
        elif doc.kind == "kb-source":
            kb_source.pull_if_changed(doc, bearer)  # into kb-repo
    write_back.maybe_push_local_commits()           # central → source via /.kb/push
```

Key properties:

- **Blocking queries** — hub wakes only on KV changes (sub-second
  reaction to a new registration or bearer rotation).
- **Single Vault token** — uses `stackwiz-hub-reader` policy,
  which has RO on `stackwiz/data/registry/*`. No per-stack
  token juggling.
- **Network-attached** — hub joins `stackwiz-shared` (framework
  network) + `kb-agent_kb-net` (077's network) so it can reach
  kb-mcpjungle for upserts.
- **Idempotent retry** — failed MCPJungle upserts stay out of
  `_known_mcp_names` so next cycle retries.
- **Backoff on error** — exponential, capped at the safety-poll
  interval. A dead Consul doesn't hammer logs.

## Consumer responsibilities

- Declare `registry:` entries. That's it.
- Ship a `vault_runtime:` block if the service reads secrets at
  runtime (so the MCP has a renewable token).
- Expose `kb-serve` HTTP endpoints (framework library
  `stackwiz_kb_serve`) if the component contributes content to the
  central KB.

## Writer-side reference

One call path for everything cross-stack-discoverable:

1. Component dev adds `registry:` to components.yaml.
2. `wizinstall run <component>` — engine writes Vault + Consul KV.
3. Hub wakes up on the KV write, reads Vault doc + bearer, calls
   the appropriate reconciler.
4. MCPs appear in LibreChat via MCPJungle within seconds. KB
   sources appear under `kb-repo/_sources/<name>/` on next pull
   cycle.

No docker labels. No manual bearer publishes to `shared/`. No SSH
keys. No kb-publish satellites.

## Dropped conventions (for context)

Pre-rework artifacts that registry entries replaced. Still visible
in old commits, but no current code depends on them:

- `label: mcp.enabled=true` on docker containers.
- `consul_services: tags: [kb-source]` + meta keys.
- `vault_bearer_path: shared/analyzer_mcp_bearer_token` in manifests.
- `/opt/stackwiz/kb-publish/*.git` bare repos + `kb-sync` SSH user.

See ADR-007 for why the hub subsumed per-stack sidecars.
