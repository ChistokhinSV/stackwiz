# SOPS Installer — Modular TUI Installer Framework

Reusable TUI-based installer framework with Consul service registry integration. Each project (AWX, monitoring, CI/CD) defines a `components.yaml` manifest; the framework provides the interactive installation experience.

## Problem

Infrastructure projects like AWX deployment (061) use monolithic bash scripts (3,300+ lines). Adding a new project means duplicating the installation pattern. No service discovery between components on different VMs.

## Solution

A shared pip-installable framework that:
- Reads a per-repo `components.yaml` manifest
- Shows a TUI for component selection and configuration
- Runs install scripts with real-time progress
- Registers services in Consul for cross-VM discovery
- Stores secrets in Consul KV

## Architecture

```
sops-installer (this repo)           Per-project repo (e.g. 061.awx)
├── installer/                       ├── components.yaml
│   ├── app.py         (Textual)     ├── install/
│   ├── screens/                     │   ├── k3s.sh
│   │   ├── welcome.py               │   ├── awx.sh
│   │   ├── components.py            │   ├── graylog.sh
│   │   ├── config.py                │   └── ...
│   │   ├── progress.py              └── templates/
│   │   └── summary.py                   └── *.j2
│   ├── engine.py      (orchestration)
│   ├── consul_client.py (registration + discovery)
│   └── manifest.py    (YAML parser)
├── pyproject.toml
└── README.md
```

## TUI Flow

```
Welcome → Component Selection → Configuration → Install Progress → Summary
```

1. **Welcome**: System info, Consul connection status
2. **Components**: Checkboxes with required/optional, dependency resolution
3. **Config**: Prompts from manifest (domain, IP, TLS mode, etc.)
4. **Progress**: Real-time per-component status (pending → running → done/failed)
5. **Summary**: Credentials, URLs, Consul service health

## Technology Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| TUI framework | **Textual** (Python) | Modern look, async, SelectionList/ProgressBar widgets |
| Manifest format | **YAML** | Declarative, typed, supports dependencies |
| Service registry | **Consul** | Service discovery + KV store + health checks |
| Secret storage | **Consul KV** (+ Vault later) | Encrypted, accessible cross-VM |
| Package manager | **UV** | Fast, pyproject.toml native |
| Install scripts | **Bash** | Reuse existing scripts from deploy.sh |

## Component Manifest Schema (v1)

```yaml
name: awx-platform
display_name: "AWX Automation Platform"
version: "1.0"

consul:
  required: true
  service_prefix: "awx"

components:
  - id: k3s
    name: "K3s Kubernetes"
    required: true
    group: core
    depends: []
    install: install/k3s.sh
    verify: "sudo k3s kubectl get nodes"
    consul_service:
      name: "k3s"
      port: 6443
      check:
        http: "https://localhost:6443/healthz"
        interval: "30s"

  - id: graylog
    name: "Graylog (Log Aggregation)"
    required: false
    default: true
    group: observability
    install: install/graylog.sh
    consul_service:
      name: "graylog"
      port: 9000
      tags: ["logging"]
    consul_discover:
      - service: "awx"
        env_var: AWX_HOST

config:
  - id: awx_domain
    label: "AWX domain name"
    type: text
    default: ""
  - id: tls_mode
    label: "TLS certificate mode"
    type: select
    choices: ["self-signed", "auto", "manual-dns"]
    default: "self-signed"

secrets:
  - id: awx_admin_password
    generate: true
    length: 16
  - id: graylog_password_secret
    generate: true
    length: 96
    immutable: true
```

## Consul Integration

### Service Registration
Each installed component registers in Consul with health checks:
```python
consul.register("awx", port=30080, check={"http": "http://localhost:30080/api/v2/ping/"})
```

### Cross-VM Discovery
Another repo's manifest discovers services:
```yaml
consul_discover:
  - service: "awx"        # Find AWX from Consul catalog
    env_var: AWX_URL       # Pass to install script as env var
```

### Secret Storage
Generated secrets stored in Consul KV:
```
awx/secrets/admin_password → encrypted value
awx/config/domain → awx.example.com
```

## Existing Projects That Will Use This

| Project | Repo | Components |
|---------|------|------------|
| AWX Automation Platform | 061.awx_installation | K3s, AWX, nginx, Graylog, Kestra, MCP |
| Consul + Authentik | (new) | Consul server, Authentik SSO |
| Monitoring Stack | (future) | Prometheus, Grafana, Alertmanager |
| CI/CD Pipeline | (future) | GitLab Runner, ArgoCD |

## Migration Path from deploy.sh

1. Extract heredoc install steps → `install/*.sh` scripts
2. Extract .env.template → `components.yaml` config + secrets sections
3. Extract nginx templates → `templates/*.j2`
4. Keep deploy.sh as fallback (deprecated but functional)
5. New entry point: `sops-install` CLI command

## Implementation Order

1. **Core framework**: manifest parser, engine, CLI entry point
2. **TUI screens**: welcome, components, config, progress, summary
3. **Consul client**: register, discover, KV get/put
4. **AWX manifest**: components.yaml for 061 repo
5. **Install scripts**: extract from deploy.sh
6. **Testing**: Vagrant VM end-to-end test
7. **Consul+Authentik manifest**: first cross-repo test

## Open Questions

- Whiptail fallback for minimal systems (no Python)?
- Consul ACL tokens for multi-tenant secret isolation?
- How to handle component upgrades (not just fresh install)?
- Should the framework support uninstall/rollback?
