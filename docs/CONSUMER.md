# stackwiz Consumer Guide

This is the reference for anyone writing a new stackwiz-based installer — a per-project repo that ships a `components.yaml` manifest, install scripts, and a thin `bootstrap.sh` that runs the framework in a container. A complete worked example lives at [`080.consul_vault_authentik`](https://github.com/ChistokhinSV/stackwiz/tree/main/../080.consul_vault_authentik).

## What stackwiz gives you

- **Declarative manifest**: list components, their dependencies, versions, health checks, and Consul service definitions (one or many per component).
- **Five-screen TUI wizard** (Welcome → Components → Config → Progress → Summary) built on Textual, with consistent layout: sticky `Header` + scrollable `#main` + docked `#button-bar` (Back/Abort/Quit on the left, primary Next on the right) + sticky `Footer`. Works in small terminal windows — content scrolls, buttons stay visible.
- **Headless mode** (`--auto`) for CI and re-runs. Same engine + same log output as the TUI, just without the pty.
- **Lifecycle**: install, upgrade (version diff), reconfigure (config-hash diff), and uninstall with a companion script contract.
- **Selective installation**: `wizinstall run consul vault` or `wizinstall run 3 4` (by id or 1-based index from `wizinstall list`), same UX as 061's `./deploy.sh 19 20`.
- **`${domain}` cascade**: one knob in `.stackwiz.env` updates every derived hostname, admin email, and LDAP base DN. `wizinstall init-env` generates a commented scaffold from the manifest.
- **Secrets**: generated passwords materialized into Vault KVv2, with `immutable` flag so re-runs never rotate them. Per-service Vault policies applied automatically.
- **Service registry**: Consul catalog + health checks for every component. Multi-service components register multiple endpoints from a single `consul_services:` list.
- **TLS helper**: a shared `stackwiz-tls.sh` script (ported from `061.awx_installation`) that does Let's Encrypt via Cloudflare/Route53/HTTP-01 or self-signed fallback, idempotent via `openssl x509 -checkend`.
- **Forensic install log**: `/state/install.log` captures every stdout/stderr line from every script plus engine events, labeled by component, in both TUI and headless runs.
- **Info retrieval**: `wizinstall info` and the auto-written `/state/summary.md` surface installed components, URLs, and (masked) secret paths without re-entering the container.

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
  - id: admin_password       # vault path: <service_prefix>/<id>
    generate: true           # auto-generate on first install
    type: password           # (default) random alnum
    length: 16
    immutable: true          # never rotate across upgrades or re-runs

  - id: consul_gossip_key    # consul wants 32 random bytes, base64-encoded
    type: base64
    length: 32               # bytes BEFORE base64 encoding
    immutable: true

  - id: session_uuid
    type: uuid               # UUID4; `length` is ignored

  - id: hex_token
    type: hex
    length: 32               # bytes → 64 hex chars

  - id: openssl_key          # arbitrary command, stdout is the value
    type: cmd
    command: "openssl rand -hex 24"

  - id: existing_key         # not generated — operator must pre-populate
    generate: false          # via .stackwiz.secrets.env or direct vault kv put
    vault_path: custom/path/existing_key   # override default
```

**Supported `type:` values** (used only when `generate: true`):

| Type | Output | `length:` means | Typical use |
|---|---|---|---|
| `password` *(default)* | random `[A-Za-z0-9]` string | output chars | admin logins, bearer tokens |
| `hex` | random hex string | random bytes (output = 2×length) | API keys, session tokens |
| `base64` | base64 of N random bytes | random bytes | Consul gossip key (`length: 32`), AES keys |
| `uuid` | `uuid4()` | *(ignored)* | stable anonymous identifiers |
| `cmd` | stdout of `command:` (trailing whitespace stripped) | *(ignored)* | anything else — `openssl`, `consul keygen`, custom scripts |

`cmd` runs inside the stackwiz container with a 30-second timeout. Non-zero exit or empty stdout aborts the install with a clear error. If you need the command to run on the host instead of in the container, prefix it with `nsenter --target 1 --all --`.

Every materialized secret is accessible to install scripts as `WIZ_SECRET_<ID>` and its Vault path as `WIZ_SECRET_<ID>_PATH`.

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
```

The ladder tries in order: existing cert fresh for >30 days → Let's Encrypt via Cloudflare DNS-01 (`CF_DNS_API_TOKEN`) → Route53 DNS-01 (`AWS_DNS_ACCESS_KEY_ID` + `AWS_DNS_SECRET_ACCESS_KEY`) → HTTP-01 standalone (opt-in via `STACKWIZ_TLS_ALLOW_HTTP01=1`) → self-signed. Set `STACKWIZ_TLS_MODE=self-signed` to skip Let's Encrypt entirely; `STACKWIZ_TLS_MODE=letsencrypt` to fail rather than fall back.

TLS credentials are passed via a `.env` file (generated by `init-env`, sourced by `bootstrap.sh`).

### Shared nginx reverse proxy (`stackwiz-nginx.sh`)

Multiple consumers sharing one VM need a single nginx container on ports 80+443. The framework provides `stackwiz-nginx.sh` — sourced by install scripts like the TLS helper:

```bash
. "${STACKWIZ_STATE_DIR}/bin/stackwiz-nginx.sh"
. "${STACKWIZ_STATE_DIR}/bin/stackwiz-tls.sh"

stackwiz_nginx_init                                    # create shared container
stackwiz_tls_ensure "myapp.example.com"                # get TLS cert
stackwiz_nginx_add_cert "myapp.example.com" "$CERT_PATH" "$KEY_PATH"
envsubst '${MYAPP_HOST}' < template.conf.j2 \
  | stackwiz_nginx_add_conf "081" "10" "myapp"         # namespaced config
stackwiz_nginx_ensure_network stackwiz-prod            # connect to app network
stackwiz_nginx_reload                                  # nginx -t + reload
```

On uninstall, one call removes all of that consumer's configs:

```bash
stackwiz_nginx_remove_consumer "081"
# If this was the last consumer, the container is torn down.
```

**How it works:**

- A single `stackwiz-nginx` container (`nginxinc/nginx-unprivileged:alpine`) owns ports 80+443.
- Each consumer namespaces its config files: `081--10-consul.conf`, `077--00-maps.conf`, `061--00-awx.conf`.
- Configs live at `/opt/stackwiz/nginx/conf.d/`, certs at `/opt/stackwiz/nginx/tls/`.
- A `.consumers` registry tracks active consumers; the container is removed only when the last deregisters.
- The compose includes `extra_hosts: host.docker.internal:host-gateway` so nginx can reach host-only services (K3s, systemd, etc.) via `proxy_pass http://host.docker.internal:<port>`.
- `stackwiz_nginx_ensure_network <net>` connects the container to additional docker networks for container-DNS resolution (e.g. `stackwiz-prod`, `kb-net`).

**Config naming convention:** `{namespace}--{priority}-{name}.conf`. The namespace is a short consumer id (e.g. `081`, `077`, `061`). Priority controls sort order within a consumer. The double-dash separates namespace from the file's own numbering.

**Key rule:** consumer vhost templates must NOT include an HTTP→HTTPS redirect or `listen 8080` block — the framework's `00-stackwiz-default.conf` handles that globally. All consumer vhosts listen on `8443 ssl`.

All four consumers (061, 077, 081, 082) use this pattern. See their `install/nginx.sh` scripts for working examples.

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

Values from earlier sources override later ones.

### Domain-derived values via `${var}` substitution

Manifest `default:` strings can reference other field values using `${id}` syntax. The most common use is deriving per-service hostnames from a single **deployment domain**:

```yaml
# components.yaml
domain: "example.com"              # top-level; also referenceable via ${domain}

config:
  - id: authentik_hostname
    default: "auth.${domain}"        # resolves to auth.example.com
  - id: authentik_admin_email
    default: "admin@${domain}"       # resolves to admin@example.com
  - id: ldap_base_dn
    default: "${domain_dn}"          # synthetic: dc=stackwiz,dc=lab
```

Synthetic variables injected by the framework:

| Variable | Value |
|---|---|
| `${domain}` | the effective deployment domain (manifest default, overridable via `.stackwiz.env` or state) |
| `${domain_dn}` | `domain` rendered as an LDAP base DN — `example.com` → `dc=stackwiz,dc=lab` |

Plus `${<field_id>}` for any field in the `config:` section. Substitution is recursive (up to 4 hops) so chains like `a.${b}` where `b: "x.${domain}"` work. Unknown `${...}` placeholders stay literal.

### Auto-resolved `node_ip`

Any config field whose id ends in `_ip` (or is exactly `node_ip`) and whose value is `"auto"` is automatically resolved by the framework:

1. **DNS lookup** — resolve the deployment `domain` to an A record
2. **Fallback** — first non-loopback IP from `hostname -I`
3. **Error** — empty string if both fail (install script should check)

This means all consumers can declare:

```yaml
config:
  - id: node_ip
    default: "auto"
    help: "Auto-detected from DNS (domain) or hostname -I"
```

and the TUI config screen shows the resolved IP instead of "auto". Install scripts receive the actual IP in `WIZ_CFG_NODE_IP`. Operators can still override with a specific IP in `.stackwiz.env`.

**Override-once, cascade-everywhere**: an operator only needs to set `domain: mycompany.internal` in `.stackwiz.env` and all derived fields automatically become `auth.mycompany.internal`, `admin@mycompany.internal`, `dc=mycompany,dc=internal`. Both the welcome screen (for service discovery) and the config screen (for the form pre-fill) see the same resolved values.

### Generating the `.stackwiz.env` scaffold

Instead of writing `.stackwiz.env` from scratch, generate a commented template from the manifest:

```bash
wizinstall init-env --manifest components.yaml
# wrote <manifest_dir>/.stackwiz.env
# Edit it and re-run `wizinstall run` to pick up the overrides.
```

The generated file contains the deployment domain at the top (commented with an explanation), followed by one entry per `config:` field with:
- A `# <label>: <help>` header
- `#   choices: ...` if the field is a `type: select`
- `#   required` if the field is mandatory
- The current effective value (post-substitution) as the YAML value

Re-running `init-env` refuses to overwrite unless you pass `--force`. It reads any existing `.stackwiz.env` first so editing + regenerating preserves your changes.

Example output for 080:

```yaml
# stackwiz consumer config overrides for Consul + Vault + Authentik v1.1.0
#
# Edit values below. Placeholders like ${domain} are resolved at load time,
# so changing `domain:` cascades through any field that references it.

domain: "example.com"

# Private network IP of this node: Used for Consul bind_addr and Vault api_addr
#   required
node_ip: "192.168.56.20"

# Authentik public hostname: Derives from the top-level domain unless overridden
#   required
authentik_hostname: "auth.example.com"

# Authentik admin email
#   required
authentik_admin_email: "admin@example.com"

# LDAP base DN: Auto-derives from ${domain} (example.com → dc=stackwiz,dc=lab)
#   required
ldap_base_dn: "dc=stackwiz,dc=lab"

# TLS certificate mode: auto = try Cloudflare/Route53/HTTP-01 then fall back to self-signed
#   choices: self-signed, auto, letsencrypt
tls_mode: "self-signed"
```

### Raw `.stackwiz.env` example

If you prefer hand-writing the file, just put the fields you want to override:

```yaml
# <manifest_dir>/.stackwiz.env
domain: mycompany.internal
node_ip: 10.0.50.20
tls_mode: auto
```

Everything else (authentik_hostname, ldap_base_dn, etc.) is computed from `${domain}` — you don't need to list them unless you want a non-derived value.

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
authentik_hostname: auth.example.com
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

`.stackwiz.env` only handles **non-secret config values** (the `config:` section of the manifest). Secrets come from Vault. Any secret declared with `generate: false` must be supplied by the operator — stackwiz will not invent a value for it.

There are two ways to supply one:

**Option A — `.stackwiz.secrets.env`** (recommended for first-time installs, when Vault isn't up yet). `wizinstall init-env` scaffolds this file next to `.stackwiz.env` with one empty line per `generate: false` secret:

```yaml
# stackwiz user-supplied secrets for mystack v0.1.0
#
# Fill in values below, then run `wizinstall run`.
# Filled entries are uploaded to Vault and REMOVED from this file,
# so secrets do not linger on disk. Empty entries cause the run to
# hard-error with the target Vault path.

# Vault path: mycompany/admin_password
admin_password: "hunter2"
```

On the next `wizinstall run`, every filled entry is `kv put` into Vault and then stripped from the file. Once every user-supplied secret has been uploaded, the file is deleted entirely. Re-running `init-env` regenerates the skeleton for anything still pending.

Empty entries at run time hard-error with the key name and target Vault path, so you can either fill the file and retry or switch to Option B.

**Option B — direct `vault kv put`** (for re-installs where Vault is already running):

```bash
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=$(sudo cat /var/lib/stackwiz/vault-token)
vault kv put stackwiz/mycompany/admin_password value="existing-password"
```

Either way, the manifest declaration is the same:

```yaml
secrets:
  - id: admin_password
    generate: false            # stackwiz won't generate; operator-provided
    vault_path: mycompany/admin_password
```

`.stackwiz.secrets.env` is written as `chmod 600`. Both `.stackwiz.env` and `.stackwiz.secrets.env` are added to the manifest-dir `.gitignore` automatically by `wizinstall init-env` (idempotent; existing entries are preserved). If you wrote those files by hand before running `init-env`, double-check `.gitignore` yourself.

## Idempotency guidelines

**stackwiz runs your scripts again on every `run`.** Scripts MUST be idempotent — re-running them against a host that's already in the desired state should be a no-op (or a fast config reload), never break anything, and never lose user data. This is a hard requirement, not a nice-to-have, because:

- Operators re-run `wizinstall run` to apply config changes — that re-runs the script with `WIZ_RECONFIGURE=1`
- Selective installs (`wizinstall run consul`) may run a script whose target is already present
- Upgrades run the same `install/<id>.sh` with `WIZ_UPGRADE=1` and `WIZ_OLD_VERSION=<previous>`
- Partial failures need to be recoverable — just re-run after fixing the underlying issue
- CI / automation expects the same manifest applied repeatedly to converge to the same state

### What "safe to re-run" actually means

The engine pre-filters re-runs via version + config-hash diffs. Your script sees one of five `WIZ_ACTION` values, each with a different contract:

| Action | When | Your script MAY… | Your script MUST NOT… |
|---|---|---|---|
| **(not run)** | `noop` — version + config unchanged, component not `repeatable` | *script isn't invoked at all* | — |
| `install` | Component not in `/state/installed.yaml` (first run, post-uninstall, or operator removed the entry) | wipe stale data from prior failed attempts, re-create users/dirs, re-initialize databases | destroy data that the operator explicitly didn't ask to be re-initialized (e.g. don't wipe user-uploaded files) |
| `reconfigure` | Version unchanged, config-hash changed | overwrite config files, reload services, re-render blueprints | drop databases, regenerate secrets, erase volumes, force a full restart unless strictly necessary |
| `upgrade` | Version differs from `installed.yaml` | run version-specific migrations, pull new images, rewrite binaries | assume the old version's data schema is compatible without inspection |
| `refresh` | Component has `repeatable: true` in the manifest, **or** operator ran `wizinstall refresh`, **or** operator ran `wizinstall run --force` | pull upstream data (git, helm charts, templates), re-seed databases with current fixtures, re-apply idempotent manifests, hit external APIs to sync state | modify the component's version or config-hash — refresh is a read-heavy action, not a state change |

### Repeatable components + `wizinstall refresh`

Some components are inherently **repeatable** — operators *want* to re-run them on demand, not only when something changed. Classic examples from `061.awx_installation`:

- **Template provisioning**: the installer clones a git repo and runs a provisioning script. Operators push changes upstream, then want the installer to re-pull and re-apply.
- **Project sync**: AWX's `project_sync` pulls inventory/playbooks from git.
- **Helm chart upgrade**: `helm upgrade --install` is naturally idempotent and should be re-runnable at will.

stackwiz supports this with **two complementary mechanisms**:

**1. Manifest flag `repeatable: true`** — mark components that should always re-run when `wizinstall run` is invoked, regardless of config-hash. The engine replans them as `Action.REFRESH` instead of `Action.NOOP`. Use this for things the operator wants to keep in sync automatically on every run:

```yaml
components:
  - id: provision_templates
    name: "Provision AWX job templates"
    repeatable: true              # runs on every `wizinstall run`, no config change needed
    install: install/provision_templates.sh
    depends: [awx]
```

**2. CLI force-refresh** — explicit operator action for one-off re-runs on components that aren't flagged `repeatable`:

```bash
wizinstall refresh                        # refresh every installed component
wizinstall refresh awx                    # refresh only awx
wizinstall refresh provision_templates    # refresh a single step
wizinstall run --force                    # equivalent to `refresh` with the same selection
wizinstall run --force awx                # force-refresh awx only
```

`refresh` and `run --force` both set `WIZ_ACTION=refresh` + `WIZ_REFRESH=1` on the script env. Components with a pending `install` / `upgrade` / `reconfigure` action keep their normal action — refresh only overrides `noop`. Forced refresh never downgrades an UPGRADE, so you can safely chain `wizinstall run --force` without worrying about losing a version bump.

**Script pattern** for a repeatable component:

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="${WIZ_CFG_TEMPLATE_REPO:?missing template_repo}"
BRANCH="${WIZ_CFG_TEMPLATE_BRANCH:-main}"
APP_DIR="/opt/myapp/templates"

case "${WIZ_ACTION:-install}" in
  install)
    git clone --branch "${BRANCH}" "${REPO}" "${APP_DIR}"
    ;;
  reconfigure|upgrade|refresh)
    # All three re-sync from upstream; refresh is the most common in practice
    # because the repo itself updates without a version bump in components.yaml.
    cd "${APP_DIR}"
    git fetch origin "${BRANCH}"
    git reset --hard "origin/${BRANCH}"
    ;;
esac

# Apply whatever the repo contains, every time.
cd "${APP_DIR}"
./apply.sh
```

**Difference between refresh and reconfigure**: `reconfigure` implies "the operator changed a config value". `refresh` implies "the operator wants this step to run again to catch up with external state". They're often treated identically by scripts, but the action name tells you *why* the script was invoked.

### The install escape hatch

**The `install` action is the only action where data destruction is appropriate.** On a reinstall after uninstall, operators expect fresh state. On a first install, there's nothing to preserve. The authentik component in [`080`](https://github.com/ChistokhinSV/stackwiz/tree/main/../080.consul_vault_authentik/install/authentik.sh) uses this to wipe a stale postgres volume only on `WIZ_ACTION=install`:

```bash
# Scoped to the install path only — never touches data on reconfigure/upgrade.
if [ "${WIZ_ACTION:-install}" = "install" ] && \
   docker volume inspect authentik_postgres-data >/dev/null 2>&1; then
  (cd "${APP_DIR}" && docker compose down -v 2>&1 | tail -5) || true
  docker volume rm -f authentik_postgres-data authentik_redis-data 2>&1 || true
fi
```

Safe because (a) all the authentik secrets are `immutable:`, so a fresh database always initializes with the same credentials that prior runs used, and (b) the state file is the source of truth — if `installed.yaml` doesn't have this component, no user expects the existing volume to be preserved.

### Verifying idempotency before you ship

Every consumer repo should exercise these four scenarios on a clean VM:

```bash
# 1. Fresh install → everything installs once
./bootstrap.sh run --auto

# 2. Re-run with no changes → everything NOOPs. Engine skips scripts entirely.
./bootstrap.sh run --auto
# Expected: every component "skipped: noop — up to date"

# 3. Config change → scripts re-run with WIZ_RECONFIGURE=1, no data loss
$EDITOR .stackwiz.env   # change a non-required field
./bootstrap.sh run --auto
# Expected: affected components "running: reconfigure — reconfigure"; user data preserved

# 4. Force fresh install of one component → test data-wipe path
sudo sed -i '/^  nginx:/,/^  [a-z]/{/^  nginx:/d;/^    /d}' /var/lib/stackwiz/installed.yaml
./bootstrap.sh run --auto
# Expected: nginx "running: install — install"; clean slate; depends-on-authentik not re-triggered
```

If any of those four break, the script isn't idempotent yet. The `docker volume rm -f` pattern + `docker compose up -d` handles most compose-based services; `systemctl restart --no-block` + HTTP poll handles most systemd services.

### The six rules

1. **Config files + systemd units are rewritten every time from heredocs.** Treat the script as the source of truth: `cat > /etc/mysvc/config.hcl <<EOF ... EOF` always overwrites. This means changing a config value means changing the manifest or script, not hand-editing the file on the host.
2. **Check before you mutate anything that's not a heredoc overwrite.** `useradd`, `mkdir`, `docker volume create` all error on existing targets. Guard with `id -u <user> >/dev/null 2>&1 || useradd …` or use idempotent variants (`install -d`, `docker volume rm -f`).
3. **`set -euo pipefail` + explicit `|| true`** on expected-to-fail commands. `grep` returning 1 on no-match will kill a pipeline via pipefail unless you suffix `|| true`.
4. **Use `install -o user -g group -m mode`** instead of `cp` + `chown` + `chmod` — atomic, idempotent, and one syscall instead of three.
5. **`systemctl restart --no-block` + HTTP poll** for health checks. Restart is safe on inactive units (becomes start). `--no-block` avoids `Type=notify` hangs. Poll an HTTP/TCP endpoint after, don't rely on systemd status.
6. **Branch on `WIZ_ACTION` when the action contract differs.** Fresh-install data wipes belong behind `if [ "$WIZ_ACTION" = "install" ]`, version migrations behind `upgrade`, config reloads behind `reconfigure`. Example:
   ```bash
   case "${WIZ_ACTION:-install}" in
     install)
       # Scoped to the fresh-install path — safe to wipe stale failed-attempt state.
       if docker volume inspect myapp-data >/dev/null 2>&1; then
         docker compose down -v || true
         docker volume rm -f myapp-data || true
       fi
       ;;
     upgrade)
       echo "upgrading from ${WIZ_OLD_VERSION} to ${WIZ_COMPONENT_VERSION}"
       ./migrate.sh "${WIZ_OLD_VERSION}" "${WIZ_COMPONENT_VERSION}"
       ;;
     reconfigure)
       echo "config changed, reloading without downtime"
       systemctl reload myservice
       ;;
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
- **Don't regenerate secrets on every run.** Use `immutable: true` in the manifest for anything paired with external state (admin passwords, signing keys, database passwords).
- **Don't write to `/tmp` without `mktemp`.** Cross-run collisions are a real issue.
- **Don't rely on stdout ordering across `server` and `worker` containers** — docker compose starts them in parallel. Use healthchecks + `depends_on: condition: service_healthy`.
- **Don't wipe data volumes outside the `WIZ_ACTION=install` branch.** A reconfigure run should never destroy databases. The authentik postgres wipe in 080 is scoped carefully — study that pattern.

### Reference: 080's scripts as idempotency exemplars

Every file under [`080/install/`](https://github.com/ChistokhinSV/stackwiz/tree/main/../080.consul_vault_authentik/install) has been verified idempotent via the four-scenario test above:

- [`consul.sh`](https://github.com/ChistokhinSV/stackwiz/tree/main/../080.consul_vault_authentik/install/consul.sh) — binary version guard, config heredoc always rewritten, `systemctl restart --no-block` + leader-election poll. Data dir `/var/lib/consul` is never touched by the script, only by `consul.uninstall.sh`.
- [`vault.sh`](https://github.com/ChistokhinSV/stackwiz/tree/main/../080.consul_vault_authentik/install/vault.sh) — captures `vault status` JSON once with `|| true`, guards init on `.initialized == false`, guards unseal on `.sealed == true`. Root token stored in `/state/vault-token` with `chmod 600` for re-adoption on re-runs.
- [`authentik.sh`](https://github.com/ChistokhinSV/stackwiz/tree/main/../080.consul_vault_authentik/install/authentik.sh) — only example in 080 that needs the `WIZ_ACTION=install` branch (wipes stale postgres volume on fresh install). Blueprint rendering uses `envsubst` with a strict whitelist so YAML tags like `!Find` survive; the outpost-token fetch loop tolerates transient 404s from the authentik API during blueprint application.
- [`nginx.sh`](https://github.com/ChistokhinSV/stackwiz/tree/main/../080.consul_vault_authentik/install/nginx.sh) — sources `stackwiz_tls_ensure` (which is itself idempotent via the 30-day `checkend` check) so re-runs reuse an existing cert. nginx config is rewritten + `nginx -t`-validated before `restart`.

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

After a successful install, several things exist on the host under `${STATE_DIR}` (default `/var/lib/stackwiz`):

- **`summary.md`** — markdown summary of components, services, config, and (masked) secret paths. Refreshed on every install run, plus whenever `info` is called. Readable from the host without running the container.
- **`installed.yaml`** — per-component version + config-hash + timestamp + consul service names. The engine's source of truth for upgrade detection.
- **`install.log`** — every line of every script's stdout/stderr, plus engine events. See the next section.
- **`config.yaml`** — the last `config:` values saved by the engine. Template fields (defaults with `${...}`) are re-derived each run; non-template fields are pre-filled from this file.
- **`bin/`** — framework helpers staged at the start of every run (`stackwiz-tls.sh`, `stackwiz-nginx-default.conf.template`). Install scripts source these.
- **`templates/`** — mirror of your consumer repo's `templates/` directory, so host-side scripts can read it.
- **`vault-token`**, **`vault-unseal`**, **`vault-init.json`** — present only when stackwiz bootstrapped Vault itself. **Back up `vault-init.json` off the VM and delete it** — it contains the root token in cleartext.

For the richest output (queries live Consul catalog + Vault KV), use the `info` subcommand:

```bash
./bootstrap.sh info                              # masked
./bootstrap.sh info --show-secrets               # unmasked
./bootstrap.sh info --format markdown > ~/current.md
./bootstrap.sh info --format json | jq '.components[].services'
./bootstrap.sh list                              # install order + state, no backend queries
```

## Install log

`${STATE_DIR}/install.log` is the primary forensic trail. It's written by **both** the TUI and headless modes (same code path in `engine._run_script`). Each session starts with a visible marker:

```
[20:14:19 11.04.2026] INFO  stackwiz: ========================================================================
[20:14:19 11.04.2026] INFO  stackwiz: stackwiz session started [headless:install] at 2026-04-11T20:14:19+00:00
```

Everything that happens during the run is logged:

| Logger | Level | What it covers |
|---|---|---|
| `stackwiz` | INFO  | Session markers |
| `engine` | INFO  | Component lifecycle: `engine: nginx: install`, `engine: nginx: registered in consul as nginx`, `engine: install finished: 1 ok, 0 failed, 3 skipped` |
| `script.<component_id>` | INFO  | Every stdout line from that component's bash script |
| `script.<component_id>` | WARNING | Every stderr line (use this to spot potential issues without reading the full log) |

Sample from a real run:

```
[20:14:20] INFO    engine:       staged framework helpers at /state/bin
[20:14:20] INFO    engine:       staged manifest templates at /state/templates
[20:14:20] INFO    engine:       nginx: install
[20:14:20] INFO    script.nginx: → running install/nginx.sh (install)
[20:14:20] INFO    script.nginx: stackwiz-tls: generated self-signed cert for auth.example.com
[20:14:20] INFO    script.nginx: nginx: cert=/etc/stackwiz/tls/auth.example.com.crt key=...
[20:14:20] WARNING script.nginx: nginx: configuration file /etc/nginx/nginx.conf test is successful
[20:14:20] WARNING script.nginx: Created symlink /etc/systemd/system/multi-user.target.wants/nginx.service
[20:14:21] INFO    script.nginx: nginx: authentik reachable via HTTPS
[20:14:21] INFO    script.nginx: ← exit 0 (ok)
[20:14:21] INFO    engine:       nginx: registered in consul as nginx
[20:14:21] INFO    engine:       install finished: 1 ok, 0 failed, 3 skipped
[20:14:21] INFO    engine:       summary written to /state/summary.md
```

Useful greps from the host:

```bash
sudo tail -f /var/lib/stackwiz/install.log       # live during a run
sudo grep '^\[.*\] WARNING' /var/lib/stackwiz/install.log  # stderr lines across all runs
sudo grep 'script.nginx' /var/lib/stackwiz/install.log     # one component only
sudo grep -A1 'session started' /var/lib/stackwiz/install.log  # session start times
```

The log **appends** across sessions — there's no rotation, so if you're running in a long-lived VM delete or truncate the file occasionally. `sudo truncate -s 0 /var/lib/stackwiz/install.log` is safe mid-run; the handler keeps writing to the same inode.

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
- [`080.consul_vault_authentik`](https://github.com/ChistokhinSV/stackwiz/tree/main/../080.consul_vault_authentik) — lab-grade exemplar: Consul + Vault + Authentik (with LDAPS) self-bootstrapping on a single Debian 12 VM. Consul/Vault run as systemd binaries, Authentik via docker-compose, self-signed TLS.
- [`081.consul_vault_authentik_docker`](https://github.com/ChistokhinSV/stackwiz/tree/main/../081.consul_vault_authentik_docker) — production-grade exemplar: everything in containers on a shared docker network. Consul with ACL+gossip, Vault with raft storage, Authentik 2025.12 (no Redis), nginx reverse proxy with Authentik ForwardAuth gating the Vault+Consul UIs, and a daily backup systemd timer. `auto` TLS mode tries Let's Encrypt first with self-signed fallback. Use this as the starting point for a real production deploy.
