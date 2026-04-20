---
title: "Framework: MCP Add/Remove Framework"
description: How to add or remove MCP servers in any stackwiz stack using components.yaml + bootstrap run/uninstall, with no code changes to the framework.
tags: framework, stackwiz, mcp, mcpjungle, librechat, registry, stackwiz-hub
---

# MCP Add/Remove Framework

MCP servers in stackwiz are just components. There is **no
separate MCP manifest** — the same `components.yaml` entry that
an operator toggles on and off drives the whole pipeline:

```
components.yaml (declarative)
       │
       ▼
engine writes/deletes:
  stackwiz/data/registry/mcp-server/<name>/config   (Vault KV)
  stackwiz/data/registry/mcp-server/<name>/token    (Vault KV, optional)
  stackwiz/registry/mcp-server/<name>               (Consul KV pointer)
       │
       ▼  Consul KV change wakes stackwiz-hub's blocking query
       ▼
stackwiz-hub reconciler:
  POST   /api/v0/servers/<name>   (install)
  DELETE /api/v0/servers/<name>   (uninstall)
  on kb-mcpjungle
       │
       ▼
LibreChat tool list refreshes on next turn
```

The only thing an operator touches is the `components.yaml`
entry. Engine + hub handle the rest.

## Adding a new MCP

Three files, all in the consumer stack (e.g. 077):

**1. `components.yaml` — component entry + secret declarations.**

```yaml
- id: kb-<name>-mcp
  name: "<Vendor> MCP Server"
  version: "1.0.0"
  required: false
  default: false               # operators opt in explicitly
  group: tools
  depends: [kb-core]
  install: install/kb-<name>-mcp.sh
  uninstall: install/kb-<name>-mcp.uninstall.sh
  consul_service:
    name: "kb-<name>"
    address: "kb-<name>"        # container name on kb-net
    port: 9000                  # server port inside the container
    tags: [mcp, <vendor>]
  vault_runtime:                # optional — only if the server
    policies: [stackwiz-shared-read]   # reads shared Vault data
  registry:
    - kind: mcp-server
      name: <name>              # unique within kind
      endpoint_url: http://kb-<name>:9000/mcp
      transport: streamable_http
      tags: [<vendor>, ai-agent]
      description: "<what the tools do, shown in MCPJungle + LibreChat>"
```

And in the top-level `secrets:` block, one entry per env var the
container needs (all `optional: true, generate: false` for
user-supplied credentials):

```yaml
- id: <name>_api_token
  generate: false
  optional: true
```

**2. `deploy/docker-compose.yml` — service definition.**

Patterns to copy verbatim from the existing MCPs in 077:

- `networks: [kb-net]` — MCPJungle reaches every server on this
  docker net by container name.
- `restart: unless-stopped`.
- Mount `/opt/stackwiz/runtime-tokens/kb-<name>-mcp.token` into
  the container at `/run/secrets/vault_token:ro` only if the
  component declares `vault_runtime:`.
- If the server needs a CLI flag to enable streamable-http (e.g.
  `mcp/atlassian`), put it in `command:` — the image's default
  entrypoint still applies, only `CMD` is overridden.
- **Do NOT publish ports to the host** — MCPJungle and LibreChat
  talk to the container on the docker net.

**3. `install/kb-<name>-mcp.sh` + `install/kb-<name>-mcp.uninstall.sh`.**

Install is a 5-liner copy of `install/kb-snmp-mcp.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
. "${WIZ_MANIFEST_DIR:?}/install/_compose_env.sh"
cd "${WIZ_MANIFEST_DIR:?}/deploy"
docker compose up -d <service-name>
```

Uninstall is a 3-liner:
```bash
#!/usr/bin/env bash
set -euo pipefail
docker rm -f kb-<name> 2>/dev/null || true
```

## Removing an MCP

```bash
./bootstrap.sh uninstall kb-<name>-mcp
```

Engine runs `uninstall.sh`, then the framework's post-publish
cleanup does three things in order:

1. `engine._post_component_unpublish(component)` — for each
   registry entry, deletes the Consul KV pointer (the wake
   signal — stackwiz-hub's blocking query returns immediately,
   the reconciler computes `desired - current` and calls
   MCPJungle's `DELETE /api/v0/servers/<name>`) and then deletes
   the Vault `registry/<kind>/<name>/{config,token}` KV docs.
2. `consul.deregister_service(component)` — removes the
   service registration.
3. `vault.revoke_service_policy` + `vault.revoke_install_policy`
   — removes the component's Vault policies.

Data volumes are preserved (they're owned by the compose
project, not the component lifecycle). Re-installing restores
the same container against the same data.

## Toggling default-on/off

Per-operator opt-in is just `default: true` vs `default: false`
in components.yaml. An operator can also flip it at runtime:

- **Enable:** `./bootstrap.sh run --auto kb-<name>-mcp`
- **Disable:** `./bootstrap.sh uninstall kb-<name>-mcp`

Fresh installs respect `default:` values in the manifest. Known
credentials must be set in `.env` (stackwiz prompts for missing
required secrets but silently skips optional ones).

## Why Consul KV as the wake signal

The stackwiz-hub daemon keeps a `Consul.kv.get(prefix,
index=X, wait=5m)` blocking query open. Any add/update/delete
under `stackwiz/registry/mcp-server/` returns immediately with
the new index. The hub then re-reads every pointer, fetches the
corresponding Vault config/token docs, computes the delta
against MCPJungle's current `/api/v0/servers/`, and issues the
right POSTs and DELETEs.

The Consul KV value is a **pointer**, not the full config,
because:

- Consul KV has a 512KB per-key cap; config docs with bearer
  tokens and many paths can exceed that.
- Bearer rotation becomes a single Vault write — every consumer
  re-reads Vault on the next KV wake, so the rotated bearer
  propagates without touching Consul.

## Failure modes this prevents

- **Phantom MCPs** — pre-framework uninstall left the Consul KV
  pointer in place. The hub's next blocking-query wake (triggered
  by an unrelated install) re-read the stale pointer, saw
  MCPJungle was missing that server, and re-POSTed it.
  `_post_component_unpublish` deletes the pointer so the reconcile
  loop correctly computes `desired − current = {name}` and issues
  the DELETE.
- **Stale bearer in MCPJungle** — without Vault cleanup, the
  `/token` doc persisted after uninstall. A subsequent
  `components.yaml` entry with the same `registry.name` would
  inherit the old token. Now token is deleted on uninstall.
- **Config drift** — a compose file hand-edited in production to
  add an MCP disappears on the next 077 re-install because
  `docker compose up -d` only creates services in the manifest.
  Putting the service in components.yaml is the only way to keep
  it across re-installs.

## Reference implementations

- **kb-snmp-mcp** (077) — minimal component with `vault_runtime`
  and a `registry:` entry. Good template for most MCPs.
- **kb-atlassian-mcp** (077) — shows the `command:` pattern for
  images that require CLI flags to select transport.
- **awx-mcp** (061) — shows the cross-stack pattern: registry
  entry in 061, consumed by 077's hub daemon in a different
  compose project.
- **graylog-mcp** (081) — same pattern, running on a different
  host entirely (081's VM, discovered via Consul).
