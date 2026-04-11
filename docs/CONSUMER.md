# stackwiz Consumer Guide

This is the reference for anyone writing a new stackwiz-based installer — a per-project repo that ships a `components.yaml` manifest, install scripts, and a thin `bootstrap.sh` that runs the framework in a container. A complete worked example lives at [`080.consul_vault_authentik`](https://github.com/ChistokhinSV/stackwiz/tree/main/../080.consul_vault_authentik).

## What stackwiz gives you

- **Declarative manifest**: list components, their dependencies, versions, health checks, and Consul service definitions.
- **Five-screen TUI wizard** (Welcome → Components → Config → Progress → Summary) for interactive runs, plus a `--auto` headless mode for CI.
- **Lifecycle**: install, upgrade (version diff), reconfigure (config-hash diff), and uninstall with a companion script contract.
- **Secrets**: generated passwords materialized into Vault KVv2, with `immutable` flag so re-runs never rotate them.
- **Service registry**: Consul catalog + health checks for every component.
- **TLS helper**: a shared `stackwiz-tls.sh` script (ported from `061.awx_installation`) that does Let's Encrypt via Cloudflare/Route53/HTTP-01 or self-signed fallback, idempotent via `openssl x509 -checkend`.
- **Info retrieval**: `wizinstall info` prints a text/markdown/JSON summary of all installed components, URLs, and secret paths.

## Prerequisites on the target host

Ubuntu 22.04+, Debian 12+, or RHEL-family systemd. Required:

- `docker` (bootstrap.sh installs it via `curl get.docker.com` if missing)
- `bash`
- `curl` / `ca-certificates`
- network access to `ghcr.io`

## Repo layout

```
<your-project-repo>/
├── bootstrap.sh            # ~40 lines; docker pull + docker run stackwiz
├── components.yaml         # manifest
├── install/                # per-component bash scripts
│   ├── consul.sh
│   ├── consul.uninstall.sh
│   ├── ...
└── templates/              # optional: per-consumer files (jinja, yaml, conf)
    └── authentik/
        └── *.yaml          # staged by the engine into /state/templates/
```

The only files stackwiz *requires* are `components.yaml` and at least one `install/<component>.sh`. Everything else is per-consumer convention.

## Manifest reference

See [`src/stackwiz/manifest.py`](../src/stackwiz/manifest.py) for the authoritative Pydantic models. Top-level keys:

| Key | Required | Notes |
|---|---|---|
| `name` | yes | slug, no spaces |
| `display_name` | yes | human-readable |
| `version` | yes | manifest version (semver) |
| `domain` | yes | drives `consul.<domain>` / `vault.<domain>` discovery |
| `consul_host`, `vault_host` | no | override auto-discovery with a static hostname |
| `consul` | yes | `{ required: bool, service_prefix: str }` — prefix for Vault KV + Consul KV keys |
| `components` | yes | list of Component entries |
| `config` | no | list of ConfigField entries — TUI surfaces these on the config screen |
| `secrets` | no | list of Secret entries — generated + stored in Vault |

### Component

```yaml
components:
  - id: authentik                   # slug, alphanumeric+_-
    name: "Authentik (SSO + LDAPS)" # display
    version: "2025.10.4"             # tracked in /state/installed.yaml for upgrade detection
    required: false                  # true = locked on in UI
    default: true                    # true = pre-checked in UI
    group: apps                      # groups components in the selection list
    depends: [vault]                 # dependencies auto-included on Next
    install: install/authentik.sh    # path relative to /manifest (your repo)
    uninstall: install/authentik.uninstall.sh
    upgrade: install/authentik.upgrade.sh   # optional; otherwise install.sh runs with WIZ_UPGRADE=1
    verify: "curl -fs http://127.0.0.1:9000/-/health/ready/"   # optional one-liner run after install
    consul_services:                 # OR `consul_service:` for a single service
      - name: "authentik"
        port: 9000
        tags: ["sso", "auth", "http"]
        check:
          http: "http://127.0.0.1:9000/-/health/ready/"
          interval: "30s"
      - name: "authentik-ldap"
        port: 636
        tags: ["sso", "ldaps"]
        check:
          tcp: "127.0.0.1:636"
          interval: "30s"
    consul_discover:                 # optional — look up other services, inject as env
      - service: "vault"
        env_var: VAULT_UPSTREAM
    env:                             # optional — extra env vars for this script only
      CUSTOM_TIMEOUT: "60"
```

### ConfigField

```yaml
config:
  - id: ldap_base_dn
    label: "LDAP base DN"
    type: text                       # text | select | bool | int | password
    default: "dc=example,dc=local"
    required: true
    help: "Used to render the LDAP provider blueprint"
  - id: tls_mode
    label: "TLS mode"
    type: select
    choices: ["self-signed", "auto", "letsencrypt"]
    default: "self-signed"
```

### Secret

```yaml
secrets:
  - id: admin_password     # vault path: <service_prefix>/<id>
    generate: true         # auto-generate on first install
    length: 16
    immutable: true        # never rotate across upgrades or re-runs
  - id: existing_key       # not generated — operator must pre-populate in Vault
    generate: false
    vault_path: custom/path/existing_key   # override default
```

Every secret is accessible to install scripts as `WIZ_SECRET_<ID>` and its Vault path as `WIZ_SECRET_<ID>_PATH`.

## Install script env contract

When the engine runs `install/<component>.sh`, it pipes the script content to `bash -s` via `nsenter --target 1 --all` (host PID 1's namespaces) and sets:

| Variable | Meaning |
|---|---|
| `WIZ_COMPONENT_ID` | component id |
| `WIZ_COMPONENT_VERSION` | manifest `version:` for this component |
| `WIZ_ACTION` | `install` / `upgrade` / `reconfigure` / `uninstall` |
| `WIZ_UPGRADE=1` | set during upgrades; also `WIZ_OLD_VERSION` with the previous one |
| `WIZ_RECONFIGURE=1` | set when config changed but version didn't |
| `WIZ_CFG_<FIELD>` | every value from the `config:` section (upper-cased key) |
| `WIZ_SECRET_<ID>` | each generated secret's value (in cleartext) |
| `WIZ_SECRET_<ID>_PATH` | the Vault KV path where it lives |
| `CONSUL_HTTP_ADDR` | reachable Consul agent (if any) |
| `VAULT_ADDR` | reachable Vault (if any) |
| `STACKWIZ_STATE_DIR` | **host-side** state dir, e.g. `/var/lib/stackwiz` — scripts should use this for anything persistent |
| env vars from `consul_discover:` mappings | looked up from Consul catalog before the script runs |

Write your scripts **idempotently** — stackwiz re-runs them on every `run` (skipped via a config-hash diff, but still expect them to handle "already installed" cleanly).

### Example: minimal install script

```bash
#!/usr/bin/env bash
set -euo pipefail

VERSION="${WIZ_COMPONENT_VERSION:-1.0.0}"
DOMAIN="${WIZ_CFG_DOMAIN:?missing domain}"
ADMIN_PW="${WIZ_SECRET_ADMIN_PASSWORD:?missing admin_password}"

# ... install + configure ...

echo "myservice ready at https://${DOMAIN}"
```

## Using the TLS helper

The engine stages `stackwiz-tls.sh` and a default nginx template into `${STACKWIZ_STATE_DIR}/bin/` at the start of every run. Source it from any install script that needs an HTTPS certificate:

```bash
. "${STACKWIZ_STATE_DIR}/bin/stackwiz-tls.sh"
stackwiz_tls_ensure "auth.${WIZ_CFG_DOMAIN}"
# $CERT_PATH and $KEY_PATH are now set

# Optional: render a reverse-proxy config from the framework template
stackwiz_nginx_render "${STACKWIZ_STATE_DIR}/bin/stackwiz-nginx-default.conf.template" \
  "auth.${WIZ_CFG_DOMAIN}" "$CERT_PATH" "$KEY_PATH" "127.0.0.1:9000" \
  > /etc/nginx/sites-available/auth.conf
ln -sf /etc/nginx/sites-available/auth.conf /etc/nginx/sites-enabled/auth.conf
rm -f /etc/nginx/sites-enabled/default
systemctl reload nginx
```

The ladder tries in order: existing cert fresh for >30 days → Let's Encrypt via Cloudflare DNS-01 (`CF_DNS_API_TOKEN`) → Route53 DNS-01 (`AWS_DNS_ACCESS_KEY_ID` + `AWS_DNS_SECRET_ACCESS_KEY`) → HTTP-01 standalone (opt-in via `STACKWIZ_TLS_ALLOW_HTTP01=1`) → self-signed. Set `STACKWIZ_TLS_MODE=self-signed` to skip Let's Encrypt entirely; `STACKWIZ_TLS_MODE=letsencrypt` to fail rather than fall back.

Pass the credentials through from your shell via `bootstrap.sh`:

```bash
# bootstrap.sh
exec sudo docker run --rm -it \
  --privileged --pid=host --network=host \
  -v "$PWD:/manifest:ro" \
  -v "${STATE_DIR}:/state" \
  -e STACKWIZ_HOST_STATE_DIR="${STATE_DIR}" \
  -e CF_DNS_API_TOKEN="${CF_DNS_API_TOKEN:-}" \
  -e AWS_DNS_ACCESS_KEY_ID="${AWS_DNS_ACCESS_KEY_ID:-}" \
  -e AWS_DNS_SECRET_ACCESS_KEY="${AWS_DNS_SECRET_ACCESS_KEY:-}" \
  -e CERTBOT_EMAIL="${CERTBOT_EMAIL:-}" \
  ghcr.io/chistokhinsv/stackwiz:latest "$@"
```

## Using templates/

Any directory under your repo's `templates/` is staged to `${STACKWIZ_STATE_DIR}/templates/` at the start of every run. Install scripts can read from it:

```bash
envsubst '${AUTHENTIK_HOSTNAME} ${LDAP_BASE_DN}' \
  < "${STACKWIZ_STATE_DIR}/templates/authentik/ldap-provider.yaml" \
  > /opt/authentik/blueprints/ldap-provider.yaml
```

Always whitelist your envsubst variables — bare `envsubst` mangles YAML tags like `!Find` and `!KeyOf`.

## Bootstrap.sh template

```bash
#!/usr/bin/env bash
set -euo pipefail

STACKWIZ_IMAGE="${STACKWIZ_IMAGE:-ghcr.io/chistokhinsv/stackwiz:latest}"
STATE_DIR="${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}"

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo mkdir -p "${STATE_DIR}"

if ! sudo docker image inspect "${STACKWIZ_IMAGE}" >/dev/null 2>&1; then
  sudo docker pull "${STACKWIZ_IMAGE}"
fi

# Interactive TUI needs a pty; --auto / --validate run headless.
headless=0
for arg in "$@"; do
  case "$arg" in --auto|--validate) headless=1 ;; esac
done
if [ "${headless}" -eq 1 ]; then
  docker_flags=(--rm)
else
  docker_flags=(--rm -it)
fi

exec sudo docker run "${docker_flags[@]}" \
  --privileged --pid=host --network=host \
  -v "$PWD:/manifest:ro" \
  -v "${STATE_DIR}:/state" \
  -e STACKWIZ_HOST_STATE_DIR="${STATE_DIR}" \
  -e CONSUL_HTTP_ADDR="${CONSUL_HTTP_ADDR:-}" \
  -e VAULT_ADDR="${VAULT_ADDR:-}" \
  -e VAULT_TOKEN="${VAULT_TOKEN:-}" \
  -e CF_DNS_API_TOKEN="${CF_DNS_API_TOKEN:-}" \
  -e AWS_DNS_ACCESS_KEY_ID="${AWS_DNS_ACCESS_KEY_ID:-}" \
  -e AWS_DNS_SECRET_ACCESS_KEY="${AWS_DNS_SECRET_ACCESS_KEY:-}" \
  -e CERTBOT_EMAIL="${CERTBOT_EMAIL:-}" \
  "${STACKWIZ_IMAGE}" "$@"
```

Usage:

```bash
./bootstrap.sh                  # interactive TUI
./bootstrap.sh run --auto       # headless install
./bootstrap.sh uninstall        # TUI teardown
./bootstrap.sh uninstall --auto # headless teardown
./bootstrap.sh validate         # validate manifest and exit
./bootstrap.sh info             # print what's installed
./bootstrap.sh info --show-secrets --format json
```

## Retrieving installed artifacts

After a successful install, two things exist on the host:

- `${STATE_DIR}/summary.md` — markdown summary of components, services, config, and (masked) secret paths. Refreshed on every install run, plus whenever `info` is called. Readable from the host without running the container.
- `${STATE_DIR}/installed.yaml` — per-component version + config-hash + timestamp + consul service names.

For the richest output (queries live Consul catalog + Vault KV), use the `info` subcommand:

```bash
./bootstrap.sh info                              # masked
./bootstrap.sh info --show-secrets                # unmasked
./bootstrap.sh info --format markdown > ~/current.md
./bootstrap.sh info --format json | jq '.components[].services'
```

## Uninstall contract

Each `install/<id>.sh` should have a companion `install/<id>.uninstall.sh` that:

1. Stops and disables systemd services it created
2. Removes systemd unit files, `systemctl daemon-reload`, `systemctl reset-failed <svc>`
3. Deletes config files, data dirs, binaries it installed
4. Removes users it created (`userdel <user>`)
5. For dockerized services: `docker compose down -v` + `docker volume rm <...>`
6. Removes anything written to `${STACKWIZ_STATE_DIR}`

The engine runs uninstall in **reverse topological order** of `depends:`, so your nginx component's uninstall runs before authentik's. After all `*.uninstall.sh` scripts succeed, the engine deregisters Consul services, removes non-immutable secrets from Vault, and clears the `<prefix>/config/*` Consul KV tree.

Immutable secrets are preserved so a reinstall keeps the same admin password.

## CI/CD for consumer repos

Consumer repos are just bash + YAML. Minimum CI:

```yaml
name: validate
on: [push, pull_request]
jobs:
  manifest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          docker run --rm -v "$PWD:/manifest:ro" \
            ghcr.io/chistokhinsv/stackwiz:latest validate
```

For end-to-end testing, vagrantfile + a real VM is the gold standard — see [`080/Vagrantfile`](https://github.com/ChistokhinSV/stackwiz/tree/main/../080.consul_vault_authentik/Vagrantfile) for a VMware Workstation Pro example.

## Gotchas + tips

- **Line endings**: add `.gitattributes` with `*.sh text eol=lf` so Windows dev machines don't produce CRLF scripts.
- **envsubst whitelist**: always pass a quoted variable list (`envsubst '${VAR1} ${VAR2}' < in > out`); bare `envsubst` expands *everything*, which breaks `$$FOO` in docker-compose healthchecks and YAML tags.
- **systemd `Type=notify` on custom binaries**: prefer `Type=exec` + HTTP-poll for readiness. Consul/Vault in particular don't reliably send `NOTIFY_SOCKET` in some kernels.
- **`pipefail` + `grep`**: `foo=$(grep ... | cut ...)` kills your script on no-match. Add `|| true`.
- **`docker compose down -v` before `rm -rf` on the app dir**: removing the compose file doesn't take volumes with it.
- **Authentik specifically**: set `AUTHENTIK_BOOTSTRAP_PASSWORD` / `_TOKEN` / `_EMAIL` on first boot to skip the manual setup flow, use bundled blueprints under `/blueprints/custom` for declarative provider/app creation, and fetch outpost-specific API tokens via `/api/v3/core/tokens/<identifier>/view_key/` post-boot.

## Where to look

- [`src/stackwiz/manifest.py`](../src/stackwiz/manifest.py) — Pydantic models (source of truth for the schema).
- [`src/stackwiz/engine.py`](../src/stackwiz/engine.py) — install/upgrade/uninstall orchestrator.
- [`src/stackwiz/executor.py`](../src/stackwiz/executor.py) — how scripts actually run (`bash -s` via `nsenter`).
- [`src/stackwiz/share/stackwiz-tls.sh`](../src/stackwiz/share/stackwiz-tls.sh) — the TLS helper (adapted from `061/remote/nginx/generate-cert.sh`).
- [`080.consul_vault_authentik`](https://github.com/ChistokhinSV/stackwiz/tree/main/../080.consul_vault_authentik) — end-to-end worked example: Consul + Vault + Authentik (with LDAPS) + nginx, self-bootstrapping on a single Debian 12 VM.
