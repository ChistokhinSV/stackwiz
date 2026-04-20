---
title: "Framework: Overview"
description: What the stackwiz installer is, how it fits into the lab, and where to look for the rest of the framework docs.
tags: framework, stackwiz, architecture, overview
---

# Stackwiz framework overview

Stackwiz is a **TUI + headless installer** that turns a per-stack
`components.yaml` manifest into a reproducible install sequence:
probe existing Consul/Vault, prompt for config, materialise
secrets, run each component's install script, register services,
write cross-stack discovery entries, clean up scoped tokens.

It lives in `C:\HOME\1.SCRIPTS\079.sops_installer\` and publishes a
single docker image at `ghcr.io/chistokhinsv/stackwiz:latest`. Every
consumer stack (061 AWX, 077 KB agent, 081 platform, 082 config
analyzer) pulls that image at install time via `./bootstrap.sh`.

## What stackwiz owns

- **Discovery** — `probe_consul` / `probe_vault` (`src/stackwiz/discovery.py`)
  find reachable backends through env vars, DNS, or loopback.
- **Manifest model** — pydantic models (`src/stackwiz/manifest.py`)
  enforce the shape every consumer writes in its `components.yaml`.
- **Install engine** — `src/stackwiz/engine.py` resolves topological
  order, mints scoped Vault tokens, runs install scripts, registers
  Consul services, publishes cross-stack registry entries.
- **Shared helpers** — `src/stackwiz/share/*.sh` ship idempotent bash
  functions that every consumer can source: nginx TLS, snmp v3,
  consul agent, kb publish, vault-runtime token file, host
  auto-article.
- **Cross-stack hub** — `src/stackwiz_hub/` reconciles the registry:
  GETs Vault docs referenced in Consul KV, upserts MCP servers in
  MCPJungle, pulls KB source snapshots over HTTP.
- **KB-serve router** — `src/stackwiz_kb_serve/` is a drop-in
  FastAPI router consumers expose alongside their MCP so the hub
  can snapshot and push-back their KB content over HTTP.

## What stackwiz does NOT own

Application containers. Every consumer brings its own
`install/<component>.sh` + `deploy/docker-compose.yml`. Stackwiz
just threads secrets + discovery env vars through them. 077's
LibreChat config, 082's Batfish deploy, 081's Authentik schema —
all consumer-owned, framework-untouched.

## Reading order for this section

1. **[component-model](component-model.md)** — what a component is
   and the fields every YAML entry supports.
2. **[installation-flow](installation-flow.md)** — the actual order
   of operations from `bootstrap.sh run` to green services.
3. **[consul-integration](consul-integration.md)** — how
   registration, health checks, and the `127.0.0.1 → node_ip`
   rewrite work (plus when to skip it with a native local agent).
4. **[vault-integration](vault-integration.md)** — install-time
   child tokens, `vault_runtime` long-lived tokens, the three
   policy tiers.
5. **[registry-and-hub](registry-and-hub.md)** — declarative
   cross-stack discovery + the hub reconcile loop.
6. **[kb-pipeline](kb-pipeline.md)** — how source KBs flow to the
   central KB and back.
7. **[shared-helpers](shared-helpers.md)** — every `share/*.sh` the
   engine stages into `${STACKWIZ_STATE_DIR}/bin/`.

See also: architectural decisions in `architecture/adr/`.
