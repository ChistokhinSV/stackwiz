---
title: "Framework: Vault integration"
description: How stackwiz materializes secrets, mints install + runtime tokens, and scopes policies per component.
tags: framework, stackwiz, vault, secrets, tokens, policies
---

# Vault integration

Stackwiz uses Vault's KV v2 at mount `stackwiz/`. Each stack writes
under `<service_prefix>/` (e.g. `awx/`, `kb/`, `analyzer/`, `prod/`)
and can publish cross-stack artefacts under `shared/`.

## Secret materialization

Components declare secrets in the manifest's top-level block:

```yaml
secrets:
  - id: mcp_bearer_token
    generate: true
    type: password
    length: 48
    immutable: true

  - id: anthropic_api_key
    generate: false           # prompt / env for it
```

`src/stackwiz/secrets.py:materialize_secrets` writes one KV doc
per secret at `stackwiz/data/<service_prefix>/<secret_id>`.
Subsequent runs READ existing values (immutable ones never
rotate; mutable ones can be bumped if the declaration changes).

Values land in the install script's env as
`WIZ_SECRET_<SECRET_ID_UPPER>`.

## Three policy tiers

Stackwiz defines three Vault policies per component at install:

| Policy | Scope | Applied to |
|--------|-------|------------|
| `<prefix>-<id>-install` | RW on own path + RO on per-stack `shared/` + RO on cross-stack `stackwiz/data/shared/*` | Install-time child token (2h, non-renewable). |
| `<prefix>-<id>` (service policy) | RO on own path. | Runtime tokens (if the component declares `vault_runtime:`). |
| `stackwiz-shared-read` | RO on `stackwiz/data/shared/*` | Attached to runtime tokens that declare `policies: [stackwiz-shared-read]`. |

Plus two framework-level policies:

- `stackwiz-hub-reader` — RO on `stackwiz/data/registry/*`. Used
  by stackwiz-hub to read every consumer's registry entries.
- (historical) `kb-sync-writer` — now deprecated with the move
  to stackwiz-kb-serve HTTP push.

See `src/stackwiz/vault_client.py` for the actual HCL.

## Install-time tokens (the default)

`engine._mint_install_token` creates a child token per component:

- TTL **2 hours**, **non-renewable**.
- Bound to the install policy (RW + shared RO).
- Passed via `VAULT_TOKEN` env to the install script.
- Revoked at end of install run.

This is fine for install scripts (they run and exit).
**Dangerous** for containers: if an install script passes
`VAULT_TOKEN` through to a `docker compose up` env, the container
inherits the 2h token. After that window every Vault read returns
**404** (Vault conflates "missing" and "permission-denied").

## Runtime tokens (`vault_runtime` block)

Declared on the component:

```yaml
vault_runtime:
  policies: [stackwiz-shared-read]
  ttl: "720h"                         # 30 days, renewable
  token_file: "/opt/stackwiz/runtime-tokens/{component_id}.token"
```

`engine._mint_runtime_token` (called inside `_prepare`, BEFORE the
install script runs so `docker compose up` can mount the file):

1. Applies the component's service policy.
2. For each extra named policy: if known (`stackwiz-shared-read`),
   apply idempotently; else trust the operator.
3. Mints a renewable child token (`VaultClient.create_child_token`
   with `renewable=True`).
4. Writes to the token_file path with 0600 perms.

Consumers bind-mount that file into their container at
`/run/secrets/vault_token` and read it on startup (e.g. 077's
`deploy/snmp-mcp/vault_creds.py`). Token survives far longer than
install; operator re-runs install to rotate.

See ADR-009 for why declarative + renewable.

## Cross-stack shared writes

Consumer installs that publish artefacts to `shared/` (e.g. 081
publishing the Consul bootstrap token, 082 publishing the MCP
bearer for cross-stack reads) do so with the **project token**
(persisted at `${STACKWIZ_STATE_DIR}/vault-token` during
`wizinstall init`). The install-time child token has only RO on
shared — writes require the higher-privileged project token.

Helper pattern: `_stackwiz_snmp_vault_token` in
`share/stackwiz-snmp.sh` — project token first, install token
fallback, warn if fallback.

## Shared entries owned by 081

| Path | Writer | Reader | Purpose |
|------|--------|--------|---------|
| `shared/consul_bootstrap_token` | 081/install/consul.sh | Any engine bootstrap | Consul ACL root. |
| `shared/consul_gossip_key` | 081/install/consul.sh | 079/share/stackwiz-consul-agent.sh | Agent gossip. |
| `shared/consul_server_addr` | 081/install/consul.sh | 079/share/stackwiz-consul-agent.sh | retry_join. |
| `shared/consul_client_token` | 081/install/consul.sh | 079/share/stackwiz-consul-agent.sh | Agent default token. |
| `shared/authentik_api_token` | 081/install/authentik.sh | 061/install/awx_authentik.sh etc. | OIDC app provisioning. |

## KV path conventions

- `stackwiz/data/<prefix>/<secret_id>` — per-component secrets.
- `stackwiz/data/shared/<artefact>` — cross-stack discovery.
- `stackwiz/data/registry/<kind>/<name>/{config,token}` — declarative registry (see registry-and-hub).
- `stackwiz/data/<prefix>/shared/*` — per-stack shared (rarely used).

See also: troubleshooting/vault-secrets.md.
