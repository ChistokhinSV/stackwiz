---
title: "Component: consul_agent (framework)"
description: Native Consul client agent shipped to every non-server host.
tags: component, framework, consul, discovery, shared
---

# consul_agent — Consul client agent

Framework-level component. Installed on every host that ISN'T
the consul server's host. Consumer stacks (061, 077, 082) declare
it via a 5-line wrapper that sources
`share/stackwiz-consul-agent.sh`.

## What it runs

Native `consul agent -config-dir=/etc/consul.d` under systemd
(`stackwiz-consul-agent.service`). Consul binary fetched from
HashiCorp releases.

Skips silently on hosts where the consul server container is
running — the server is already this node's agent.

## Ports

- 8500/TCP (client HTTP, bind 0.0.0.0 for local registrations)
- 8301/TCP + UDP (gossip, needed for cluster membership)

## Config

None in components.yaml.

## Secrets

Read at install time from Vault `shared/`:

- `shared/consul_gossip_key` — symmetric gossip encryption.
- `shared/consul_server_addr` — `<ip>:8300` for retry_join.
- `shared/consul_client_token` — ACL token with
  `stackwiz-agent` policy (node:write + service:write +
  agent:read + session:write).

All three published by 081's `install/consul.sh`.

## Depends on

- 081 consul (server) deployed first — the shared Vault entries
  must exist.
- Vault reachable.

## Marker file

On success, writes `${STACKWIZ_STATE_DIR}/local-consul-agent`.
The stackwiz engine checks this marker and skips the
`127.0.0.1 → node_ip` check-target rewrite (ADR-008).

## Verify

```bash
sudo systemctl status stackwiz-consul-agent
consul members | grep "$(hostname -s)"
```

Expect the local hostname listed as `alive`.

## See also

- ADR-008 (native agent topology)
- `framework/consul-integration.md`
- `troubleshooting/consul-discovery.md`
