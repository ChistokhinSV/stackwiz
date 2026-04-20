---
title: "Framework: Shared helpers"
description: Every script under 079's src/stackwiz/share/, what it does, how consumers source it.
tags: framework, stackwiz, shared-helpers, idempotent, bash
---

# Shared helpers

Consumer install scripts source these from
`${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}/bin/`. The engine stages
them on every run from `/usr/local/share/stackwiz/` (baked into the
stackwiz docker image at build time). All helpers are idempotent
and side-effect-free on no-op.

## stackwiz-host.sh

Host detection + misc. `_host_vault_token` used by several helpers
to pick the project token over the install-scoped one (some
writes to `shared/` require elevated privileges).

## stackwiz-tls.sh

`stackwiz_tls_ensure <hostname>` — provisions a TLS cert for
`<hostname>` based on `STACKWIZ_TLS_MODE`:

- `self-signed` — generates self-signed via openssl, writes to
  `/opt/stackwiz/tls/<hostname>.{crt,key}`.
- `letsencrypt` — runs certbot against port 80.
- `auto` — chooses self-signed for RFC1918 IPs, LE for public.

Sets `CERT_PATH` / `KEY_PATH` in caller scope.

## stackwiz-nginx.sh

`stackwiz_nginx_init` — ensures a shared `stackwiz-nginx`
container is running with a consumer-writable `conf.d/` volume.

`stackwiz_nginx_add_cert <hostname> <crt-path> <key-path>` — mounts
a cert pair into the container.

`stackwiz_nginx_add_vhost <hostname> <config-fragment>` — appends
an nginx server block. Typical consumer pattern: template a
`proxy_pass http://upstream-container:port` block with optional
ForwardAuth via Authentik.

## stackwiz-snmp.sh

`stackwiz_snmp_install` — apt-installs snmpd, generates SNMPv3
authPriv credentials, writes them to Vault at
`stackwiz/data/shared/hosts/<hostname>`. Idempotent: reuses
existing creds on re-runs. Writes go through the project token
(install token lacks write on shared/*).

Keys written: `snmp_user`, `snmp_auth_protocol` (SHA),
`snmp_auth_key`, `snmp_priv_protocol` (AES), `snmp_priv_key`.
The `snmp_` prefix coexists with `ssh_*` on the same host entry.

## stackwiz-consul-agent.sh

Native Consul client-agent install via systemd. Reads
`shared/consul_{gossip_key,server_addr,client_token}` from Vault,
renders `/etc/consul.d/agent.hcl` from
`stackwiz-consul-client.hcl.j2`, starts systemd unit
`stackwiz-consul-agent.service`.

Skips silently if a local `consul` container is detected (server
host doesn't need a separate client). Writes marker file
`${STACKWIZ_STATE_DIR}/local-consul-agent` on success — picked up
by the engine to skip the `127.0.0.1 → node_ip` check rewrite.

## stackwiz-consul-attach.sh

`stackwiz_consul_attach_network <net>` — idempotently attaches the
`consul` container to a consumer-owned docker network so its
embedded DNS resolves container names. Used by 077's
`_compose_env.sh` to keep consul on `kb-agent_kb-net` across
consul recreates.

## stackwiz-hub.sh

`stackwiz_hub_ensure` — brings up the `stackwiz-hub` container
from `stackwiz-hub-compose.yml` (also shipped in share/). Reads
Vault addr, issues a token with `stackwiz-hub-reader` policy,
passes via env. Auto-attaches to known consumer nets
(kb-agent_kb-net, etc.) via `_stackwiz_hub_attach_extra_networks`.

## stackwiz-kb-publish.sh (legacy)

Maintained for older consumers still pushing their kb/ to a bare
repo for kb-mcp-registrar discovery. The replacement is the HTTP
tarball path (`stackwiz_kb_serve` library + hub's `kb_source`
reconciler). New consumers should NOT call `stackwiz_kb_publish`.

## stackwiz-kb-host-article.sh

`stackwiz_generate_host_article <kb-dir>` — writes a live
`articles/hosts/<hostname>.md` describing this host: IP, running
containers, Consul services, web endpoints, Vault keys under
`shared/hosts/<hostname>`. Called at the end of each install so
the central KB's host table is always current.

## bootstrap/stackwiz-bootstrap.sh

Framework-managed consumer bootstrap library. Each consumer's
`bootstrap.sh` sources this and calls `sw_bootstrap_main`. Does:

- Loads `bootstrap.conf.sh` per-project overrides.
- Detects `--auto` / subcommand → sets `SW_HEADLESS`.
- Pulls the stackwiz image.
- `docker run` the engine with the right mounts + env.

To refresh the bootstrap on a consumer:
`docker run --rm -v "$PWD:/out" ghcr.io/chistokhinsv/stackwiz:latest
extract-bootstrap --output /out --force`.

## Where to find the source

`C:\HOME\1.SCRIPTS\079.sops_installer\src\stackwiz\share\*.sh` —
canonical. Never edit the staged copies at
`${STACKWIZ_STATE_DIR}/bin/`; they're overwritten on every install.

See also the compose snippets + HCL templates in the same dir:
`stackwiz-hub-compose.yml`, `stackwiz-nginx-compose.yml`,
`stackwiz-nginx-default.conf(.template)`,
`stackwiz-consul-client.hcl.j2`.
