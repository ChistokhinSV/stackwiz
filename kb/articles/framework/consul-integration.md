---
title: "Framework: Consul integration"
description: How stackwiz registers services, runs health checks, handles LAN vs loopback, and plugs in native client agents.
tags: framework, stackwiz, consul, service-discovery, health-checks
---

# Consul integration

Every component can publish zero or more services into Consul's
catalog. The engine handles registration, health-check URL
rewriting, and cross-stack address resolution.

## Registration

`src/stackwiz/consul_client.py:register_service` (called from
`engine._post_component_publish`). Flow per service:

1. Deregister the service id first (`{name}-{component.id}`).
   Consul keeps check-goroutines running across re-register; a
   fresh register without this leaves stale checks firing on old
   URLs.
2. If `address:` is set on the service and contains `${...}`, the
   engine interpolates it against the effective config values
   (so `address: "${backup_hostname}"` becomes
   `backup.example.com`).
3. Register with name, id, address, port, tags, meta, optional
   check.

## Address selection — the `192.168.0.101` footgun

If `consul_service.address` is unset, the engine defaults to the
node's LAN IP. **That's a lie** for services that bind loopback
only or live on a docker network. Best practice:

- **External/LAN services** (nginx:443, snmp-agent:161, ftp:21,
  k3s:6443): leave `address:` unset — node IP is correct.
- **nginx-fronted services**: `address: "${<component>_hostname}"`
  (FQDN). Consumers resolve via DNS → nginx, get TLS + auth.
- **Internal-only docker services**: `address: "<container-name>"`
  (container DNS). Consumers on the same docker network resolve
  directly.

See ADR-010 for the full rationale.

## Health checks

Two shapes in `ConsulServiceCheck`:

```yaml
check:
  http: "http://127.0.0.1:8080/health"  # HTTP GET, 2xx = passing
  interval: "15s"
  timeout: "5s"
  tls_skip_verify: false
```

```yaml
check:
  tcp: "127.0.0.1:69"                   # TCP connect succeeds
  interval: "30s"
```

## The `127.0.0.1 → node_ip` rewrite

`register_service._rewrite`: if the check URL contains
`127.0.0.1` and the engine's `node_address` is something else,
replace. Reason: consul-server runs in a container with its own
netns; its loopback isn't the host's. For host-bound services the
remote consul agent needs the host's external IP.

**Skipped when** `is_local_native_agent=True` on the ConsulClient.
That flag is set when `${STACKWIZ_STATE_DIR}/local-consul-agent`
exists — dropped by `stackwiz_consul_agent_install` on hosts
that run a native client agent (see
[shared-helpers](shared-helpers.md)).

## Native local agent deployment

The `consul_agent` component (installed from 079's
`share/stackwiz-consul-agent.sh`) runs a native Consul binary
under systemd and joins the remote server cluster.

Per-host behavior:

- Host that runs the consul SERVER container (081's VM today):
  the helper detects the container and skips. Server IS the agent.
- Remote host (future 082 / 061 split): fetches the `consul`
  binary, reads `shared/consul_gossip_key` + `shared/consul_server_addr`
  + `shared/consul_client_token` from Vault, renders
  `/etc/consul.d/agent.hcl`, starts a systemd unit.

Once the marker exists, subsequent installs on that host register
services against `127.0.0.1:8500` (the local agent) and the engine
skips the rewrite → `127.0.0.1` in checks stays `127.0.0.1`, which
is correct (local agent + local service share the host's loopback).

## Server-side published materials (081)

081's `install/consul.sh` writes three bootstrap artefacts to
Vault `shared/`:

| Path | Value | Consumer |
|------|-------|----------|
| `shared/consul_bootstrap_token` | Root ACL token (UUID) | Engine bootstrap on any stack. |
| `shared/consul_gossip_key` | Base64 gossip key | Framework client-agent install. |
| `shared/consul_server_addr` | `<node_ip>:8300` | Framework client-agent retry_join. |
| `shared/consul_client_token` | ACL token w/ `stackwiz-agent` policy | Framework client-agent's default token. |

## Cross-stack network attachment

Consul's embedded DNS can only resolve container names on networks
it's joined. The compose declares `stackwiz-prod` + `stackwiz-shared`
— framework networks. For consumer-specific networks (077's
`kb-agent_kb-net`), consumers call
`stackwiz_consul_attach_network kb-agent_kb-net` from their
install scripts (see
`src/stackwiz/share/stackwiz-consul-attach.sh`).

See also: troubleshooting/consul-discovery.md.
