---
title: "Component: snmp (framework)"
description: SNMPv3 authPriv agent shipped to every stackwiz host via the shared helper.
tags: component, framework, snmp, monitoring, shared
---

# snmp — SNMPv3 agent

Framework-level component. Every consumer stack (061, 077, 081,
082) declares it with a 5-line thin wrapper that sources
`share/stackwiz-snmp.sh` and calls `stackwiz_snmp_install`.

## What it runs

Native `snmpd` via apt — not a container. Systemd manages it.
Per-host SNMPv3 user + auth/priv keys generated on first install,
reused on subsequent installs.

## Port + network

- UDP 161 on node IP (all interfaces)
- No TLS (SNMPv3 authPriv is the protocol-level encryption)

## Config

None in components.yaml. Fully automatic.

## Secrets

Written by the helper (not declared in manifest):

- `stackwiz/data/shared/hosts/<hostname>` with keys `snmp_user`,
  `snmp_auth_protocol` (SHA), `snmp_auth_key`, `snmp_priv_protocol`
  (AES), `snmp_priv_key`.

Reads via the project token (install token has RO on shared;
writes need elevated privilege). Helper picks project token from
`${STACKWIZ_STATE_DIR}/vault-token`.

## Depends on

Vault reachable + the project token on disk. If 081 isn't
installed yet, the helper warns and skips cred storage (snmpd
runs with transient creds that never persist — useless but
non-fatal).

## Verify

```bash
snmpget -v3 -u <user> -l authPriv \
  -a SHA -A "$snmp_auth_key" \
  -x AES -X "$snmp_priv_key" \
  127.0.0.1 .1.3.6.1.2.1.1.1.0
```

Creds via `vault kv get stackwiz/shared/hosts/<hostname>`.

## See also

- `framework/shared-helpers.md`
- `troubleshooting/snmp-credentials.md`
- `troubleshooting/vault-secrets.md`
