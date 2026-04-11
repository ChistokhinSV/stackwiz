# stackwiz — Modular TUI Installer Framework

A reusable installer framework that reads a per-project `components.yaml` manifest, drives an interactive Textual TUI wizard, runs install scripts on the host, and stores service metadata in Consul + secrets in HashiCorp Vault.

- **One image**, many projects. Each consumer repo ships a tiny `bootstrap.sh` that pulls `ghcr.io/chistokhinsv/stackwiz:latest`.
- **No Python on the host.** The installer runs entirely inside a container; bash scripts are piped to host PID 1 via `nsenter` so they see real systemd, apt, k3s and docker while the TUI stays isolated.
- **Declarative components.** YAML manifest lists the components, their dependencies, their versions, their Consul service definitions (one or many per component), and what secrets they need.
- **Service discovery built in.** Consul + Vault are found at `consul.<domain>` / `vault.<domain>` via DNS, with environment overrides and a local-fallback probe. Probes use the *effective* domain — an operator override in `.stackwiz.env` changes where the installer looks for backends.
- **Template-driven config with `${domain}` cascade.** One knob (the deployment domain) in `.stackwiz.env` updates every derived hostname, admin email, and LDAP DN in the manifest.
- **Install, upgrade, reconfigure, uninstall.** State is tracked in `/state/installed.yaml`; re-runs detect version bumps and config changes. Uninstall runs in reverse topological order, deregisters Consul services, and cleans non-immutable Vault secrets.
- **Selective runs.** `wizinstall run consul vault` or `wizinstall run 3 4` installs only a subset (by id or 1-based index), same UX as 061's `./deploy.sh 19 20`.
- **Full forensic log.** Every stdout/stderr line from every install script lands in `/state/install.log` in both TUI and headless modes — `grep 'script.nginx' /state/install.log` gives you every nginx-component line across all runs.

## Quickstart for a consumer project

Your repo needs three things: `components.yaml`, `install/*.sh` scripts, and a `bootstrap.sh`.

### 1. Write `components.yaml`

```yaml
name: awx-platform
display_name: "AWX Automation Platform"
version: "1.0.0"
domain: "example.internal"           # drives consul.<domain> / vault.<domain>
                                     # and every ${domain}-templated field below

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
    consul_services:                     # plural: one component, multiple endpoints
      - name: "awx-api"
        port: 30080
        tags: ["http"]
        check:
          http: "http://127.0.0.1:30080/api/v2/ping/"
          interval: "30s"
      - name: "awx-metrics"
        port: 30091
        tags: ["metrics"]
        check:
          tcp: "127.0.0.1:30091"
          interval: "30s"
    consul_discover:
      - service: "graylog"
        env_var: GRAYLOG_HOST

config:
  - id: awx_hostname
    label: "AWX public hostname"
    type: text
    default: "awx.${domain}"             # cascades from the top-level domain
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
    immutable: true                      # preserved across re-installs
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

Copy the template from [`docs/CONSUMER.md → Bootstrap.sh template`](docs/CONSUMER.md#bootstrapsh-template). It's ~50 lines and handles:

- Installing Docker if missing
- Pulling `ghcr.io/chistokhinsv/stackwiz:latest` (or a local `stackwiz:dev` build)
- TTY detection: headless subcommands (`run --auto`, `validate`, `list`, `info`, `init-env`) don't request a pty, so they work under `vagrant ssh -c` or CI
- Read-write mount for `init-env` (which writes to your manifest dir), read-only for everything else
- Passing `CF_DNS_API_TOKEN` / `AWS_DNS_*` / `CERTBOT_EMAIL` through to the TLS helper
- Chowning `init-env` output back to the invoking user

### 4. Use the installer

On the target Ubuntu/Debian VM:

```bash
git clone <your-project-repo>
cd <your-project-repo>

./bootstrap.sh init-env        # (optional) generate a commented .stackwiz.env scaffold
                                #           edit the domain — everything derives from it
$EDITOR .stackwiz.env

./bootstrap.sh                 # interactive TUI (default subcommand is `run`)
./bootstrap.sh run --auto      # headless install, no TUI
./bootstrap.sh run nginx       # install only the `nginx` component
./bootstrap.sh run 3 4         # install only components 3 and 4 (by index from `list`)
./bootstrap.sh list            # show install order with indices + current state
./bootstrap.sh info            # show what's installed + URLs + (masked) secret paths
./bootstrap.sh uninstall       # TUI teardown, reverse topological order
./bootstrap.sh uninstall nginx # remove a single component
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

1. **Welcome** screen probes `consul.<effective_domain>` then `127.0.0.1:8500`, same for Vault. The *effective* domain comes from `.stackwiz.env` > `state/config.yaml` > manifest `domain:` — operator overrides take effect before discovery fires. Reachable backends are adopted; missing ones can be installed as part of the run if the manifest defines `consul` or `vault` components.
2. **Components** screen shows `SelectionList` grouped by `group:`, with dependency auto-include and per-component status badges (`[install]` / `[upgrade 1.2→1.3]` / `[ok]`). Skipped entirely if the CLI already locked a selection via positional args.
3. **Config** screen builds a dynamic form from the manifest's `config:` section, pre-filled from the effective values (with `${var}` substitution applied so `auth.${domain}` is already `auth.your-lab.internal` by the time you see it). Values are saved to `/state/config.yaml` on Next — but template-backed fields (with `${...}` in their manifest default) are always re-derived on the next run, not clobbered by the cache.
4. **Progress** screen streams each install script's stdout/stderr into a `RichLog` while a `DataTable` tracks per-component status. Simultaneously, every line is logged to `/state/install.log` via the `stackwiz.script.<component_id>` logger so headless runs get the same forensic trail.
5. **Summary** screen lists installed components, shows the Vault paths for generated secrets, and points at the log + `/state/summary.md`.

All screens share the same layout: `Header` (sticky top) + `VerticalScroll#main` (flex, scrolls on overflow) + `Horizontal#button-bar` (3 rows, `Back`/`Quit`/`Abort` on the left, `Next` on the right) + `Footer` (sticky bottom). Buttons stay visible even in small terminal windows.

## State directory layout

```
/state/                  (host path: /var/lib/stackwiz by convention)
├── install.log          # every session's script stdout/stderr + engine events
│                         # Session markers: "[tui:install]" / "[headless:install]" / etc.
├── installed.yaml       # per-component: version, installed_at, config_hash, consul_services
├── config.yaml          # last-used config values (state cache, beaten by .stackwiz.env)
├── summary.md           # Markdown snapshot of the last install — components, URLs, secret paths
├── bin/                 # host helpers staged by the engine (stackwiz-tls.sh, nginx template, …)
├── templates/           # manifest templates/ mirrored from the consumer repo
├── vault-token          # (written by vault bootstrap) root token for Vault re-adoption
├── vault-unseal         # (written by vault bootstrap) unseal key — back up + delete
└── vault-init.json      # (first-run only) full init output — back up + delete
```

`install.log` is the first place to look when something fails. It's a rolling append — sessions are separated by a `========` marker and a `stackwiz session started [<mode>]` line. Every line tagged `script.<component_id>` is a raw stdout/stderr line from that component's bash script. `stderr` lines use `WARNING` level so `grep WARNING install.log` finds potential issues fast.

## CLI

`wizinstall` is a subcommand group. Every subcommand accepts `--manifest`, `--state`, and (for `run`/`uninstall`) `--auto` for headless operation.

```
wizinstall run                      # interactive TUI install (default)
wizinstall run --auto               # headless install, no TUI
wizinstall run consul vault         # install only these two (by id, dependency order preserved)
wizinstall run 3 4                  # install only components 3 and 4 (by index from `list`)
wizinstall run --force              # force re-run all required+default components (Action.REFRESH)
wizinstall run --force provision    # force re-run one component even if nothing changed
wizinstall refresh                  # re-run everything installed with Action.REFRESH
wizinstall refresh provision        # re-run one installed component with Action.REFRESH
wizinstall uninstall                # TUI teardown (reverse topological order)
wizinstall uninstall --auto         # headless teardown
wizinstall uninstall nginx          # remove a single component
wizinstall list                     # print install order with indices + current state
wizinstall validate                 # parse the manifest, print the install order, exit
wizinstall init-env                 # generate a commented .stackwiz.env scaffold from the manifest
wizinstall info                     # show installed components + URLs + masked secret paths
wizinstall info --show-secrets      # unmask Vault values
wizinstall info --format {text|markdown|json}
```

`run` and `uninstall` accept component ids *or* 1-based indices as positional args — same UX as 061's `./deploy.sh 19 20`. Selective mode does not auto-include dependencies; you're responsible for running prerequisites first (topological order *within* the selection is preserved).

`refresh` and `run --force` are for **repeatable steps** — git-synced provisioning, helm upgrades, template re-rendering, anything where "run this again" is a legitimate operator request. The target components run with `WIZ_ACTION=refresh` and `WIZ_REFRESH=1` set. Mark naturally-repeatable components with `repeatable: true` in the manifest so plain `wizinstall run` picks them up automatically. See [`docs/CONSUMER.md → Repeatable components`](docs/CONSUMER.md#repeatable-components--wizinstall-refresh) for the full pattern.

`wizinstall info` also atomically refreshes `/state/summary.md` on every call, and the engine writes it at the end of every successful install run. Read it with `sudo cat /var/lib/stackwiz/summary.md` — no need to re-enter the container.

## Writing a new consumer

See [docs/CONSUMER.md](docs/CONSUMER.md) for the full reference (manifest schema, install-script env contract, TLS helper usage, templates, bootstrap.sh template, CI/CD, uninstall contract, gotchas). A complete worked example lives at [`080.consul_vault_authentik`](https://github.com/ChistokhinSV/stackwiz/tree/main/../080.consul_vault_authentik).

## Developing locally

```bash
uv sync --extra dev
uv run wizinstall validate --manifest tests/manifest_valid.yaml
uv run wizinstall list --manifest tests/manifest_valid.yaml
uv run pytest -q           # 49 passing, 2 skipped (executor tests need bash + nsenter)
uv run ruff check src tests
```

Running the TUI locally (outside a container, on Linux): set `STACKWIZ_EXECUTOR_MODE=direct` so scripts run in the current namespace instead of trying to `nsenter` into host PID 1.

## Manifest reference

See `src/stackwiz/manifest.py` for the authoritative Pydantic models. Top-level keys:

| Key | Required | Notes |
|---|---|---|
| `name` | yes | slug |
| `display_name` | yes | human-readable |
| `version` | yes | manifest semver |
| `domain` | yes | deployment domain — drives `consul.<domain>` / `vault.<domain>` and is the primary target for `${domain}` substitution |
| `consul_host`, `vault_host` | no | override auto-discovery with a specific hostname |
| `consul` | yes | `{ required, service_prefix }` — prefix is used for both Vault KV (`<prefix>/<secret_id>`) and Consul KV (`<prefix>/config/<key>`) |
| `components` | yes | list of Component entries (each supports `consul_service` *or* `consul_services:` list) |
| `config` | no | list of ConfigField entries — TUI surfaces these; defaults can use `${domain}` / `${domain_dn}` / `${other_field_id}` |
| `secrets` | no | list of Secret entries — generated into Vault KVv2; `immutable: true` preserves across re-runs |

**`${var}` substitution** in field `default:` values is resolved recursively (up to 4 hops) against the merged config map. Operators only need to set `domain` once in `.stackwiz.env` and every derived field (`auth.${domain}`, `${domain_dn}`, `admin@${domain}`) updates automatically. See [`docs/CONSUMER.md`](docs/CONSUMER.md) for the full syntax and `wizinstall init-env` workflow.

## License

MIT
