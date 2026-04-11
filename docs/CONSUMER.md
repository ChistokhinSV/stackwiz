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

Write your scripts **idempotently** — stackwiz re-runs them on every `run` (skipped via a config-hash diff, but still expect them to handle "already installed" cleanly). See [Idempotency guidelines](#idempotency-guidelines) below.

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
./bootstrap.sh list              # show install order with indices + state
./bootstrap.sh run consul vault  # install only these two (by id)
./bootstrap.sh run 3 4           # install only components 3 and 4 (by index from `list`)
./bootstrap.sh uninstall nginx   # remove a single component
```

## Overriding config values before a run

Both the TUI and headless modes build up the config-screen defaults by merging three sources, highest priority first:

1. **`<state_dir>/config.yaml`** — written by the engine at the end of every successful run. Pre-populates the TUI so re-runs remember what you typed last time.
2. **`<manifest_dir>/.stackwiz.env`** — optional YAML file next to your `components.yaml`. Use this to bake in non-default values that should apply on fresh installs without editing the manifest.
3. **Manifest `config:` defaults** — the `default:` on each `ConfigField` in `components.yaml`.

Values from earlier sources override later ones. `.stackwiz.env` is a plain YAML map:

```yaml
# <manifest_dir>/.stackwiz.env
domain: mycompany.internal
node_ip: 10.0.50.20
authentik_hostname: auth.mycompany.internal
authentik_admin_email: admin@mycompany.internal
ldap_base_dn: "dc=mycompany,dc=internal"
tls_mode: auto
```

In **headless mode** (`run --auto`), `.stackwiz.env` is the *only* way to supply required config values — there's no terminal to prompt on, and required fields without a default will abort the run. In **TUI mode**, `.stackwiz.env` pre-fills the config screen so operators can review and confirm.

You can also pre-seed `<state_dir>/config.yaml` directly on the host if you want the TUI to skip prompting altogether:

```bash
# On the target VM, before running the TUI
sudo mkdir -p /var/lib/stackwiz
sudo tee /var/lib/stackwiz/config.yaml <<EOF
domain: mycompany.internal
node_ip: 10.0.50.20
authentik_hostname: auth.mycompany.internal
tls_mode: auto
EOF
```

This also works when the state dir is on a shared volume (e.g., Vagrant's synced folder) — put your `config.yaml` in the repo and point `STACKWIZ_HOST_STATE_DIR` at it.

### Vagrant-specific workflow

For the 080 consumer on a fixed-IP Vagrant VM, two options work well:

**Option A — commit the overrides to your repo** (recommended for team setups):

```bash
# On your host machine
cd 080.consul_vault_authentik
cat > .stackwiz.env <<EOF
node_ip: 192.168.56.20
authentik_hostname: auth.stackwiz.lab
ldap_base_dn: "dc=stackwiz,dc=lab"
tls_mode: self-signed
EOF
vagrant rsync
vagrant ssh -c "cd ~/080 && STACKWIZ_IMAGE=stackwiz:dev ./bootstrap.sh"
```

The TUI launches with those values pre-filled in the config screen, so you just hit Next through each screen.

**Option B — edit the manifest defaults** (for one-off labs):

```yaml
config:
  - id: authentik_hostname
    default: "auth.my-lab.internal"   # change here, re-rsync
```

Option A is better for anything you want to reproduce. Option B is better when you're the only user of the repo.

### Secrets overrides

`.stackwiz.env` only handles **non-secret config values** (the `config:` section of the manifest). Secrets come from Vault — if you need to pre-seed a specific secret instead of having stackwiz generate it, use a secret with `generate: false` and pre-populate the Vault path via `vault kv put`:

```bash
# Before the install run
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=$(sudo cat /var/lib/stackwiz/vault-token)
vault kv put stackwiz/mycompany/admin_password value="existing-password"
```

```yaml
secrets:
  - id: admin_password
    generate: false            # stackwiz won't generate; operator-provided
    vault_path: mycompany/admin_password
```

## Idempotency guidelines

**stackwiz runs your scripts again on every `run`.** Even though the engine skips components whose version + config-hash haven't changed (status: `noop`), you should still write scripts that survive being invoked multiple times against an already-configured host. This matters because:

- Operators re-run `wizinstall run` to apply config changes — that re-runs the script with `WIZ_RECONFIGURE=1`
- Selective installs (`wizinstall run consul`) may run a script whose target is already present
- Upgrades run the same `install/<id>.sh` with `WIZ_UPGRADE=1` and `WIZ_OLD_VERSION=<previous>`
- Partial failures need to be recoverable — just re-run after fixing the underlying issue

### The five rules

1. **Check before you mutate.** `useradd`, `install -d`, `mkdir -p`, `cat > file`, `systemctl enable` are idempotent. `useradd` on an existing user fails — guard with `id -u <user> >/dev/null 2>&1 || useradd ...`.
2. **`set -euo pipefail` + explicit `|| true`** on expected-to-fail commands. `grep`, `test`, and conditional operations in pipelines will kill the script via pipefail.
3. **Use `install -o user -g group -m mode`** instead of `cp` + `chown` + `chmod` — it's atomic and idempotent.
4. **`systemctl restart` over `start`** — restart is safe on an inactive unit (becomes a start); start is a no-op on an active unit. `systemctl enable --now` is similarly safe.
5. **Branch on `WIZ_ACTION` when the upgrade path differs.** A version bump might need a data migration; a reconfigure might just need a reload. Example:
   ```bash
   case "${WIZ_ACTION:-install}" in
     install)      echo "fresh install" ;;
     upgrade)      echo "upgrading from ${WIZ_OLD_VERSION} to ${WIZ_COMPONENT_VERSION}"; migrate_data ;;
     reconfigure)  echo "config changed, reloading"; systemctl reload myservice ;;
   esac
   ```

### Idempotent patterns by resource type

| Resource | Idempotent pattern |
|---|---|
| **apt package** | `apt-get install -y --no-install-recommends <pkg>` (already idempotent; skips if installed) |
| **Binary install** | Version-guard: `"$(mybin version)" != "v${WIZ_COMPONENT_VERSION}"` → download + `install -m 0755` |
| **System user** | `id -u <user> >/dev/null 2>&1 \|\| useradd --system --home /etc/mysvc --shell /bin/false <user>` |
| **Directory** | `install -d -o <user> -g <user> -m 0750 /var/lib/mysvc` |
| **Config file** | `cat > /etc/mysvc/config.hcl <<EOF ... EOF` (always overwrites — source of truth is the script) |
| **systemd unit** | `cat > /etc/systemd/system/mysvc.service <<EOF ... EOF && systemctl daemon-reload && systemctl enable mysvc && systemctl restart --no-block mysvc` |
| **Service health** | Poll an HTTP / TCP check after `restart --no-block` instead of relying on `Type=notify` |
| **docker compose stack** | `docker compose --env-file .env up -d` (recreates changed containers, leaves others alone); wait on a health endpoint rather than a fixed sleep |
| **docker volume** | Don't pre-create; let compose manage. Only `docker volume rm` on uninstall |
| **Consul service** | Let stackwiz register via `consul_services:` — don't call the Consul API from install scripts |
| **Vault secret** | Use `WIZ_SECRET_<ID>` from env — stackwiz materializes them into Vault before the script runs |
| **TLS cert** | `. "${STACKWIZ_STATE_DIR}/bin/stackwiz-tls.sh" && stackwiz_tls_ensure "$hostname"` — already idempotent (30-day checkend) |

### Verifying readiness, not just starting

Starting a systemd unit doesn't mean the service is ready to accept traffic. After `systemctl restart --no-block`, poll:

```bash
systemctl enable mysvc
systemctl restart --no-block mysvc
for i in $(seq 1 60); do
  if curl -fs http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
    echo "mysvc ready"
    exit 0
  fi
  sleep 1
done
echo "mysvc failed to become ready within 60s" >&2
journalctl -u mysvc --no-pager --since '2 minutes ago' | tail -40 >&2
exit 1
```

Two reasons to use `--no-block`:
- `systemctl start/restart` with `Type=notify` hangs forever if the service doesn't send `sd_notify` (which some binaries don't under all kernels); `--no-block` returns immediately.
- Your own HTTP poll gives you a better failure message than systemd's timeout.

### Anti-patterns

- **Don't `rm -rf /opt/<app>` unconditionally at the top of install scripts** — that wipes user data on every re-run. Uninstall scripts clean up; install scripts bring forward.
- **Don't hard-code `sleep N`** for "let it start up" — use a poll loop. Sleeps hide real slowness and fail under load.
- **Don't initialize Vault / databases twice.** Guard with status checks (`vault status -format=json | jq -e '.initialized == false'`).
- **Don't regenerate secrets on every run.** Use `immutable: true` in the manifest for anything paired with external state (admin passwords, signing keys).
- **Don't write to `/tmp` without `mktemp`.** Cross-run collisions are a real issue.
- **Don't rely on stdout ordering across `server` and `worker` containers** — docker compose starts them in parallel. Use healthchecks + `depends_on: condition: service_healthy`.

### Idempotent install script skeleton

Use this as a starting template:

```bash
#!/usr/bin/env bash
#
# Install <component>.
#
# Expected env (from stackwiz):
#   WIZ_COMPONENT_VERSION    version tag
#   WIZ_CFG_DOMAIN           public hostname
#   WIZ_SECRET_ADMIN_PASSWORD admin password
#
set -euo pipefail

VERSION="${WIZ_COMPONENT_VERSION:-1.0.0}"
DOMAIN="${WIZ_CFG_DOMAIN:?missing domain}"
ADMIN_PW="${WIZ_SECRET_ADMIN_PASSWORD:?missing admin_password}"

# 1. System user (idempotent)
if ! id -u mysvc >/dev/null 2>&1; then
  useradd --system --home /etc/mysvc --shell /bin/false mysvc
fi

# 2. Data directories (idempotent)
install -d -o mysvc -g mysvc -m 0750 /var/lib/mysvc /etc/mysvc

# 3. Binary — only re-download on version mismatch
installed_version="$(/usr/local/bin/mysvc version 2>/dev/null | awk '{print $2}' || true)"
if [ "${installed_version}" != "v${VERSION}" ]; then
  apt-get update
  apt-get install -y --no-install-recommends curl ca-certificates unzip
  tmp="$(mktemp -d)"
  curl -fsSLo "${tmp}/mysvc.zip" "https://releases.example.com/mysvc/${VERSION}/linux-amd64.zip"
  unzip -o "${tmp}/mysvc.zip" -d "${tmp}"
  install -m 0755 "${tmp}/mysvc" /usr/local/bin/mysvc
  rm -rf "${tmp}"
fi

# 4. Config + systemd unit (always rewritten — the script is the source of truth)
cat > /etc/mysvc/config.yaml <<EOF
domain: ${DOMAIN}
admin_password: ${ADMIN_PW}
EOF
chown mysvc:mysvc /etc/mysvc/config.yaml
chmod 0640 /etc/mysvc/config.yaml

cat > /etc/systemd/system/mysvc.service <<'UNIT'
[Unit]
Description=My Service
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=mysvc
Group=mysvc
ExecStart=/usr/local/bin/mysvc serve -config /etc/mysvc/config.yaml
Restart=on-failure
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable mysvc
systemctl restart --no-block mysvc

# 5. Wait for readiness (not just "started")
for i in $(seq 1 60); do
  if curl -fs "http://127.0.0.1:8080/healthz" >/dev/null 2>&1; then
    echo "mysvc ${VERSION} ready at https://${DOMAIN}"
    exit 0
  fi
  sleep 1
done
echo "mysvc did not become ready within 60s" >&2
journalctl -u mysvc --no-pager --since '2 minutes ago' | tail -40 >&2
exit 1
```

## Selective installation

You don't always want to run the whole manifest. Common cases:

- Bootstrapping a new host where Consul + Vault already exist on another VM — install only the app components
- Reconfiguring one component after rotating a credential
- Retrying a single component after a transient failure without re-running expensive upstream steps

stackwiz gives you a 061-style step list + positional selection:

```bash
./bootstrap.sh list                    # show the install order with indices + state

#   #  id                    version       state         description
# ------------------------------------------------------------------
#   1  consul                1.19.2        installed     HashiCorp Consul [consul:8500]
#   2  vault                 1.17.6        installed     HashiCorp Vault [vault:8200]
#   3  authentik             2025.10.4     installed     Authentik (SSO + LDAPS) [authentik:9000, authentik-ldap:636]
#   4  nginx                 1.0.0         pending       nginx (HTTPS reverse proxy) [nginx:443]

./bootstrap.sh run nginx               # run ONLY nginx (by id)
./bootstrap.sh run 4                   # same thing (by 1-based index)
./bootstrap.sh run authentik nginx     # run both, in topological order
./bootstrap.sh run --auto authentik    # headless single-component install
./bootstrap.sh uninstall nginx         # remove only nginx
```

**Selective install does not auto-include dependencies.** If you `run app` and `app` depends on `k3s`, you're responsible for running `k3s` first. This matches 061's `./deploy.sh 19 20` behavior: literal selection, no magic. The engine still respects topological order *within* the selected set, so `run 4 1 3` executes in the correct order regardless of how you typed it.

For fresh installs, run the whole manifest via `./bootstrap.sh run` (no args). The selective form is a surgical tool, not the default.

### When the TUI is used with pre-selected components

If you run the interactive TUI with positional args — `./bootstrap.sh run authentik nginx` — the welcome screen skips the components picker entirely and jumps straight to config (install mode) or progress (uninstall mode). Re-run without args to get the full picker back.

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
