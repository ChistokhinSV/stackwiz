---
title: "Framework: Installation Flow"
description: Step-by-step: what happens from `./bootstrap.sh run` to green services.
tags: framework, stackwiz, installation, bootstrap, engine
---

# Installation flow

Every consumer stack runs `./bootstrap.sh run [--auto] [COMPONENTS...]`.
Here's what happens, in order.

## 1. Bootstrap wrapper (consumer-side)

`bootstrap.sh` lives in the consumer repo. It sources
`stackwiz-bootstrap.sh` (a framework-managed library vendored by
`extract-bootstrap`) and calls `sw_bootstrap_main`.

Responsibilities:

- Load `bootstrap.conf.sh` if present (per-project SW_* overrides).
- Decide headless vs TUI based on args (`--auto`, `validate`, etc.
  → headless; no args → TUI).
- `docker pull ghcr.io/chistokhinsv/stackwiz:latest` (image freshness).
- `docker run` the image, mounting `$PWD` → `/manifest` (read-only
  by default; writable when the subcommand needs it) and the host
  state dir → `/state`.
- Inject env: `VAULT_ADDR`, `VAULT_TOKEN`, `CONSUL_HTTP_ADDR`,
  `CONSUL_HTTP_TOKEN`, `STACKWIZ_STATE_DIR`, `STACKWIZ_HOST_MANIFEST_DIR`.

The engine container's `ENTRYPOINT ["wizinstall"]` takes it from
there.

## 2. Discovery (engine)

`src/stackwiz/discovery.py`:

- `probe_consul` tries, in order: `CONSUL_HTTP_ADDR` env →
  `consul.<domain>:8500` → `127.0.0.1:8500`.
- `probe_vault` mirrors the same pattern with `VAULT_ADDR`.

If either is reachable, the engine builds a client
(`src/stackwiz/tokens.py:build_backends`). If not, components that
declare `consul.required: true` or depend on Vault-backed secrets
fail fast with a clear message ("install 081 first").

## 3. Manifest load + validation

`src/stackwiz/manifest.py:load_manifest` parses YAML into
`Manifest` + list of `Component`. Pydantic enforces shape. Unknown
keys log a warning (forward-compat) via `_LeafModel._warn_on_extras`.

## 4. Config resolution

Config values come from, in priority order:
1. `.stackwiz.env` (operator-set overrides, see
   `src/stackwiz/config_overrides.py`).
2. TUI prompts (`src/stackwiz/screens/config.py`) or `--auto` defaults.
3. Manifest field `default:` values.
4. Interpolation of `${other_field}` inside default strings.

## 5. Planning

`engine.plan_actions` decides per component:

- `INSTALL` — not yet installed.
- `REFRESH` — installed but config hash changed (or `--force`).
- `UPGRADE` — `version` bumped in manifest vs recorded state AND
  an `upgrade:` script exists.
- `NOOP` — installed, config unchanged.

## 6. Per-component execution

For each step in topological order:

1. **Materialize secrets** — `src/stackwiz/secrets.py` reads from
   or generates into Vault at
   `stackwiz/data/<service_prefix>/<secret_id>`.
2. **Mint install-token** — `create_install_policy` +
   `create_child_token` with TTL 2h, non-renewable. The install
   script gets it via `VAULT_TOKEN` env.
3. **Mint runtime-token** (new, when `vault_runtime:` is declared) —
   renewable, 30d default, written to
   `/opt/stackwiz/runtime-tokens/<id>.token`.
4. **Build env** — all `WIZ_CFG_*` / `WIZ_SECRET_*` / discovery
   results → install script's env.
5. **Run install script** via `src/stackwiz/executor.py`. Script
   runs on the HOST via nsenter (not inside the engine container)
   so it can touch docker / apt / systemd.
6. **Verify** — run `verify:` one-liner if set. Failure is a
   warning, not fatal.
7. **Post-publish** — register consul services, publish registry
   entries, mirror config KV, apply runtime service policy.
8. **Lazy adopt consul/vault** — if this component just installed
   consul or vault, re-probe and build clients for later components
   in the same run.

## 7. Cleanup

After all steps, engine revokes install tokens and their policies
(runtime tokens + their policies stay — they're used by containers).

Headless mode prints a summary of what installed / was skipped /
got refreshed + a list of secrets resolved (paths only, values never
leak).

## Debugging a stuck install

- `docker logs <engine-container>` — the engine's stderr. Every
  component prints one `[cid] action: reason` line before the
  script runs and one `[cid] done` / `ERR` after.
- `sudo cat /var/lib/stackwiz/state.yaml` — recorded state.
- `STACKWIZ_LOG_LEVEL=DEBUG ./bootstrap.sh ...` for per-call Vault
  + Consul logging.

See also: troubleshooting/installation-idempotency.md.
