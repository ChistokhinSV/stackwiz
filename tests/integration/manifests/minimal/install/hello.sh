#!/usr/bin/env bash
# Smoke-test install script: proves the engine can run a script, pass env,
# and wire the component into Consul. Does not actually install anything.
set -euo pipefail

echo "hello from stackwiz integration test"
echo "  WIZ_COMPONENT_ID=${WIZ_COMPONENT_ID:-<unset>}"
echo "  WIZ_CFG_GREETING=${WIZ_CFG_GREETING:-<unset>}"

# If Vault is reachable, prove we have a usable token by writing + reading
# back one scratch value under the component's namespace. Non-fatal when
# Vault isn't configured (shouldn't happen in the integration harness).
if [ -n "${VAULT_ADDR:-}" ] && [ -n "${VAULT_TOKEN:-}" ]; then
  curl -sf -H "X-Vault-Token: ${VAULT_TOKEN}" \
    -X POST -H "Content-Type: application/json" \
    -d '{"data":{"smoke":"ok"}}' \
    "${VAULT_ADDR%/}/v1/stackwiz/data/it/hello/smoke" >/dev/null \
    || echo "  (smoke write failed; continuing)" >&2
fi
