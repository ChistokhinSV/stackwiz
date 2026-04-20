---
title: "Framework: Component Model"
description: Every field on a components.yaml entry and what the engine does with it.
tags: framework, stackwiz, components-yaml, manifest, pydantic
---

# Component model

A **component** is the unit of install in stackwiz. Every entry in
`components.yaml` under `components:` becomes a pydantic `Component`
(src/stackwiz/manifest.py:143). The engine resolves the list into a
topological order and runs each in turn.

## Required fields

| Field | Purpose |
|-------|---------|
| `id` | Slug. Alphanumeric + `-` + `_`. Unique per stack. |
| `name` | Human label shown in the TUI / headless output. |
| `install` | Path to the install script, relative to the manifest. |

## Common optional fields

| Field | Purpose |
|-------|---------|
| `version` | Arbitrary string. Recorded in state; unused for ordering. |
| `required` | `true` = always installs. `false` + `default: true` = selected by default, toggleable. |
| `default` | Only meaningful when `required: false`. |
| `group` | Groups in TUI. Common values: `core`, `infra`, `tools`, `monitoring`, `integration`, `orchestration`, `provisioning`. |
| `depends` | List of component ids. Engine runs those first. |
| `uninstall` | Path to uninstall script. Falls back to a no-op. |
| `upgrade` | Optional per-component upgrade script. |
| `verify` | Bash one-liner the engine runs after install as a sanity check. |
| `repeatable` | If `true`, engine re-runs on every `wizinstall run` (e.g. `kb-seed`). Default: false — skipped if already installed. |
| `env` | Extra env vars injected into the install script. |
| `publishes` | Config keys to mirror into Consul KV under `<service_prefix>/config/<key>`. |

## Consul service registration

One of, or both of (mutually exclusive shape — see the
`_service_exclusivity` validator):

- `consul_service:` — single service block.
- `consul_services:` — list of service blocks.

Each block:

```yaml
consul_service:
  name: "oxidized"
  address: "${backup_hostname}"   # or literal container name
  port: 443
  tags: ["tls", "config-backup"]
  meta: { mcp_transport: streamable_http }
  check:
    tcp: "127.0.0.1:443"          # or `http: "..."`
    interval: "30s"
    tls_skip_verify: false
```

The engine rewrites `127.0.0.1` in check URLs to the node's LAN IP
when the consul agent is remote or containerized — this matters
only today because the server runs in a container. With a native
local agent (see consul-integration), the rewrite is skipped.

## Consul discovery of other stacks' services

```yaml
consul_discover:
  - service: "graylog-input"
    env_var: GRAYLOG_INPUT_HOST
    required: false
```

The engine looks up the named service in Consul; if found, injects
`<env_var>=<ip>:<port>` into the install script. `required: false`
(default) lets the install proceed even if the service isn't up —
typical for optional log forwarding / SSO providers.

## Cross-stack registry entries

```yaml
registry:
  - kind: mcp-server
    name: kb
    endpoint_url: http://kb-mcp:8080/mcp
    transport: streamable_http
    bearer_secret: mcp_bearer_token
    tags: [kb, core]
    description: "KB agent"
```

The engine writes each entry to:

- `stackwiz/data/registry/<kind>/<name>/config` (the doc) in Vault
- `stackwiz/data/registry/<kind>/<name>/token` (optional bearer) in Vault
- Pointer at Consul KV `stackwiz/registry/<kind>/<name>`

stackwiz-hub reconciles these — see [registry-and-hub](registry-and-hub.md).

## Long-lived runtime Vault token

```yaml
vault_runtime:
  policies: [stackwiz-shared-read]
  ttl: "720h"
```

Engine mints a renewable child token with the service policy + any
extras, writes it to
`/opt/stackwiz/runtime-tokens/<component-id>.token` (0600).
Consumers bind-mount that file into their container at
`/run/secrets/vault_token` so runtime reads survive past the 2h
install-token TTL.

## Full reference

See `src/stackwiz/manifest.py` — the pydantic models ARE the spec.
