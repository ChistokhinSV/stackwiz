# stackwiz — Modular TUI Installer Framework

A reusable installer framework that reads a per-project `components.yaml` manifest, drives an interactive Textual TUI wizard, runs install scripts on the host, and stores service metadata in Consul + secrets in HashiCorp Vault.

- **One image**, many projects. Each consumer repo ships a tiny `bootstrap.sh` that pulls `ghcr.io/chistokhinsv/stackwiz:latest`.
- **No Python on the host.** The installer runs entirely inside a container; bash scripts are run on the host via `nsenter`.
- **Declarative components**. YAML manifest lists the components, their dependencies, their versions, their Consul service definitions, and what secrets they need.
- **Service discovery built in**. Consul + Vault are found at `consul.<domain>` / `vault.<domain>` via DNS, with environment overrides and a local-fallback probe.
- **Install, upgrade, reconfigure, uninstall**. State is tracked in `/state/installed.yaml`; re-runs detect version bumps and config changes.

## Quickstart for a consumer project

Your repo needs three things: `components.yaml`, `install/*.sh` scripts, and a `bootstrap.sh`.

### 1. Write `components.yaml`

```yaml
name: awx-platform
display_name: "AWX Automation Platform"
version: "1.0.0"
domain: "example.internal"           # drives consul.<domain> / vault.<domain>

consul:
  required: true
  service_prefix: "awx"

components:
  - id: k3s
    name: "K3s Kubernetes"
    version: "1.30.0"
    required: true
    group: core
    install: install/k3s.sh
    uninstall: install/k3s.uninstall.sh
    verify: "sudo k3s kubectl get nodes"
    consul_service:
      name: "k3s"
      port: 6443
      check:
        http: "https://127.0.0.1:6443/healthz"
        interval: "30s"

  - id: awx
    name: "AWX"
    version: "24.0.0"
    required: true
    group: core
    depends: [k3s]
    install: install/awx.sh
    uninstall: install/awx.uninstall.sh
    consul_service:
      name: "awx"
      port: 30080
      check:
        http: "http://127.0.0.1:30080/api/v2/ping/"
        interval: "30s"
    consul_discover:
      - service: "graylog"
        env_var: GRAYLOG_HOST

config:
  - id: awx_domain
    label: "AWX public hostname"
    type: text
    required: true
  - id: tls_mode
    label: "TLS mode"
    type: select
    choices: ["self-signed", "auto", "manual-dns"]
    default: "self-signed"

secrets:
  - id: admin_password
    generate: true
    length: 16
  - id: cluster_token
    generate: true
    length: 96
    immutable: true
```

### 2. Write install scripts

Each `install/<component>.sh` is a plain bash script. stackwiz sets these environment variables before invoking it:

| Variable | Meaning |
|---|---|
| `WIZ_COMPONENT_ID` | the component id |
| `WIZ_COMPONENT_VERSION` | manifest version for this component |
| `WIZ_ACTION` | `install` \| `upgrade` \| `reconfigure` \| `uninstall` |
| `WIZ_UPGRADE=1` | set during upgrades; also `WIZ_OLD_VERSION` |
| `WIZ_RECONFIGURE=1` | set when config changed but version didn't |
| `WIZ_CFG_<FIELD>` | every value from the `config:` section (uppercased) |
| `WIZ_SECRET_<ID>` | each generated secret's value |
| `WIZ_SECRET_<ID>_PATH` | the Vault KV path where it lives |
| `CONSUL_HTTP_ADDR` | reachable Consul agent |
| `VAULT_ADDR` | reachable Vault |
| `STACKWIZ_STATE_DIR` | host-side state directory (where `stackwiz-tls.sh` and other helpers are staged) |
| any variables from `consul_discover:` mappings | looked up from Consul catalog before the script runs |

Write idempotent scripts so re-runs do the right thing.

Companion `install/<component>.uninstall.sh` handles teardown; optional `install/<component>.upgrade.sh` can handle complex version migrations (otherwise the regular install script runs with `WIZ_UPGRADE=1`).

### 3. Write `bootstrap.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

sudo mkdir -p /var/lib/stackwiz
sudo docker pull ghcr.io/chistokhinsv/stackwiz:latest
sudo docker run --rm -it \
  --privileged --pid=host --network=host \
  -v "$PWD:/manifest:ro" \
  -v /var/lib/stackwiz:/state \
  -e CONSUL_HTTP_ADDR="${CONSUL_HTTP_ADDR:-}" \
  -e VAULT_ADDR="${VAULT_ADDR:-}" \
  -e VAULT_TOKEN="${VAULT_TOKEN:-}" \
  ghcr.io/chistokhinsv/stackwiz:latest "$@"
```

Then on the target Ubuntu/Debian VM:

```bash
git clone <your-project-repo>
cd <your-project-repo>
./bootstrap.sh             # launches the TUI
./bootstrap.sh --uninstall # reverse teardown
```

## How it works

```
Host VM (Ubuntu/Debian)
  bootstrap.sh → docker run ──▶ stackwiz container
                                   │ Textual TUI
                                   │
                                   │ nsenter --target 1 --all -- bash install/<script>.sh
                                   ▼
                                 host systemd / apt / k3s / docker
```

1. **Welcome** screen probes `consul.<domain>` then `127.0.0.1:8500`, same for Vault. Reachable backends are adopted; missing ones can be installed as part of the run if the manifest defines `consul` or `vault` components.
2. **Components** screen shows `SelectionList` grouped by `group:`, with dependency auto-include and per-component status badges (`[install]` / `[upgrade 1.2→1.3]` / `[ok]`).
3. **Config** screen builds a dynamic form from the manifest's `config:` section. Values are saved to `/state/config.yaml` for re-runs.
4. **Progress** screen streams each install script's stdout/stderr into a `RichLog` while a `DataTable` tracks per-component status. `/state/install.log` captures the full transcript.
5. **Summary** screen lists installed components, shows the Vault paths for generated secrets, and points at the log.

## State directory layout

```
/state/
├── install.log         # full log from every session
├── installed.yaml      # {component: version, installed_at, config_hash, consul_service}
├── config.yaml         # last-used config values (for pre-fill on re-run)
└── vault-init.json     # ONLY on first-run Vault bootstrap; back up then delete
```

## CLI

```
wizinstall [OPTIONS]

  --manifest PATH     components.yaml [default: /manifest/components.yaml]
  --state PATH        state dir [default: /state]
  --uninstall         reverse teardown
  --validate          validate manifest and exit
  -V, --version
  -h, --help
```

## Developing locally

```bash
uv sync --extra dev
uv run wizinstall --validate --manifest tests/manifest_valid.yaml
uv run pytest -q
```

Running the TUI locally (outside a container, on Linux): set `STACKWIZ_EXECUTOR_MODE=direct` so scripts run in the current namespace instead of trying to `nsenter` into host PID 1.

## Manifest reference

See `src/stackwiz/manifest.py` for the authoritative Pydantic models. Top-level keys:

| Key | Required | Notes |
|---|---|---|
| `name` | yes | slug |
| `display_name` | yes | human-readable |
| `version` | yes | manifest semver |
| `domain` | yes | drives `consul.<domain>` / `vault.<domain>` |
| `consul_host`, `vault_host` | no | override auto-discovery |
| `consul` | yes | `{ required, service_prefix }` |
| `components` | yes | list of Component entries |
| `config` | no | list of ConfigField entries |
| `secrets` | no | list of Secret entries |

## License

MIT
