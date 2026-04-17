# stackwiz-snmp.sh — SNMPv3 agent (authPriv) for every stackwiz VM.
#
# Source from consumer install scripts:
#     . "${STACKWIZ_STATE_DIR}/bin/stackwiz-snmp.sh"
#     stackwiz_snmp_install
#
# Installs snmpd with SNMPv3 authPriv (SHA + AES). Credentials are
# stored per-host in Vault at stackwiz/data/shared/hosts/<hostname>
# alongside any other host-scoped secrets (ssh_* etc.) — each VM has
# its own set, so the monitoring system must track per-host creds.
#
# Requires: VAULT_ADDR set, vault-token readable in state dir.

STACKWIZ_SNMP_USER="stackwiz"

# TLS-verify options for curl against Vault:
#   VAULT_CACERT=/path    → curl --cacert /path
#   STACKWIZ_VAULT_VERIFY=false → curl -k (opt-out, warn)
#   otherwise            → default (system CA trust)
_stackwiz_snmp_curl_tls() {
    if [ -n "${VAULT_CACERT:-}" ]; then
        printf -- '--cacert %s' "${VAULT_CACERT}"
    elif [ "${STACKWIZ_VAULT_VERIFY:-true}" = "false" ] \
      || [ "${STACKWIZ_VAULT_VERIFY:-true}" = "0" ] \
      || [ "${STACKWIZ_VAULT_VERIFY:-true}" = "no" ]; then
        printf -- '-k'
    fi
}

_stackwiz_snmp_vault_addr() {
    local addr="${VAULT_ADDR:-}"
    if [ -z "$addr" ]; then
        # Try to discover from state
        for f in "${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}"/*/vault-token \
                 "${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}/vault-token"; do
            [ -f "$f" ] && break
        done
        addr="https://127.0.0.1:8200"
    fi
    echo "$addr"
}

_stackwiz_snmp_vault_token() {
    # Prefer VAULT_TOKEN env var (set by the engine for every install script).
    if [ -n "${VAULT_TOKEN:-}" ]; then echo "$VAULT_TOKEN"; return 0; fi
    # Fallback: read from state dir (for manual invocations).
    local state="${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}"
    for f in "${state}/vault-token" "${state}"/*/vault-token; do
        if [ -f "$f" ]; then cat "$f"; return 0; fi
    done
    echo ""
}

_stackwiz_snmp_host_path() {
    local hostname
    hostname="$(hostname -s 2>/dev/null || hostname)"
    echo "shared/hosts/${hostname}"
}

# Reads SNMP creds from the per-host entry and emits 5 lines:
#   snmp_user / auth_protocol / auth_key / priv_protocol / priv_key
# Falls back to un-prefixed legacy keys to support entries written by
# pre-prefix versions; the next write migrates them.
_stackwiz_snmp_vault_get() {
    local addr token host_path
    addr="$(_stackwiz_snmp_vault_addr)"
    token="$(_stackwiz_snmp_vault_token)"
    [ -z "$token" ] && return 1
    host_path="$(_stackwiz_snmp_host_path)"
    # shellcheck disable=SC2046  # intentional word-split of TLS opts
    curl -sf $(_stackwiz_snmp_curl_tls) -H "X-Vault-Token: ${token}" \
        "${addr}/v1/stackwiz/data/${host_path}" 2>/dev/null \
        | python3 -c 'import sys,json
d=json.load(sys.stdin).get("data",{}).get("data",{})
def pick(new, old):
    v = d.get(new)
    return v if v not in (None, "") else d.get(old, "")
auth_key = pick("snmp_auth_key", "auth_key")
if auth_key:
    print(d.get("snmp_user", ""))
    print(pick("snmp_auth_protocol", "auth_protocol"))
    print(auth_key)
    print(pick("snmp_priv_protocol", "priv_protocol"))
    print(pick("snmp_priv_key", "priv_key"))
' 2>/dev/null
}

# Merge SNMP creds into the per-host secret. Preserves unrelated keys
# (ssh_*, etc.) and drops any legacy un-prefixed SNMP keys.
_stackwiz_snmp_vault_put() {
    local user="$1" auth_key="$2" priv_key="$3"
    local addr token host_path
    addr="$(_stackwiz_snmp_vault_addr)"
    token="$(_stackwiz_snmp_vault_token)"
    [ -z "$token" ] && { echo "stackwiz-snmp: no vault token — cannot store keys" >&2; return 1; }
    host_path="$(_stackwiz_snmp_host_path)"

    local snmp_data="{\"snmp_user\":\"${user}\",\"snmp_auth_protocol\":\"SHA\",\"snmp_auth_key\":\"${auth_key}\",\"snmp_priv_protocol\":\"AES\",\"snmp_priv_key\":\"${priv_key}\"}"

    local existing
    # shellcheck disable=SC2046
    existing="$(curl -sf $(_stackwiz_snmp_curl_tls) -H "X-Vault-Token: ${token}" \
        "${addr}/v1/stackwiz/data/${host_path}" 2>/dev/null \
        | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin).get("data",{}).get("data",{})))' 2>/dev/null || echo '{}')"

    local merged
    merged="$(python3 -c "
import json,sys
existing = json.loads('''${existing}''')
snmp = json.loads('''${snmp_data}''')
for k in ('auth_protocol','auth_key','priv_protocol','priv_key'):
    existing.pop(k, None)
existing.update(snmp)
print(json.dumps(existing))
" 2>/dev/null || echo "$snmp_data")"

    # shellcheck disable=SC2046
    curl -sf $(_stackwiz_snmp_curl_tls) -X PUT -H "X-Vault-Token: ${token}" \
        -H "Content-Type: application/json" \
        "${addr}/v1/stackwiz/data/${host_path}" \
        -d "{\"data\":${merged}}" \
        >/dev/null 2>&1 && \
        echo "stackwiz-snmp: stored credentials at ${host_path}" || \
        echo "stackwiz-snmp: WARNING: failed to store per-host credentials" >&2
}

_stackwiz_snmp_generate_key() {
    openssl rand -hex 16
}

stackwiz_snmp_install() {
    # 1. Install snmpd
    if ! command -v snmpd >/dev/null 2>&1; then
        echo "stackwiz-snmp: installing snmpd..."
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq >/dev/null
        apt-get install -y -qq snmpd snmp libsnmp-dev >/dev/null
    fi

    # 2. Read or generate credentials
    local snmp_user auth_proto auth_key priv_proto priv_key
    local vault_data
    vault_data="$(_stackwiz_snmp_vault_get 2>/dev/null || true)"
    if [ -n "$vault_data" ]; then
        snmp_user="$(echo "$vault_data" | sed -n '1p')"
        auth_proto="$(echo "$vault_data" | sed -n '2p')"
        auth_key="$(echo "$vault_data" | sed -n '3p')"
        priv_proto="$(echo "$vault_data" | sed -n '4p')"
        priv_key="$(echo "$vault_data" | sed -n '5p')"
        echo "stackwiz-snmp: using existing per-host credentials from Vault"
        # Re-write so any legacy un-prefixed keys are migrated out.
        _stackwiz_snmp_vault_put "$snmp_user" "$auth_key" "$priv_key" >/dev/null 2>&1 || true
    else
        snmp_user="${STACKWIZ_SNMP_USER}"
        auth_proto="SHA"
        auth_key="$(_stackwiz_snmp_generate_key)"
        priv_proto="AES"
        priv_key="$(_stackwiz_snmp_generate_key)"
        if _stackwiz_snmp_vault_put "$snmp_user" "$auth_key" "$priv_key"; then
            echo "stackwiz-snmp: generated new credentials, stored in Vault"
        else
            echo "stackwiz-snmp: generated credentials (Vault unavailable — stored locally only)" >&2
        fi
    fi

    # 3. Stop snmpd before modifying user database
    systemctl stop snmpd 2>/dev/null || true

    # 4. Configure snmpd
    cat > /etc/snmp/snmpd.conf <<EOF
# stackwiz-managed — do not edit manually.
# SNMPv3 only (authPriv), no v1/v2c community strings.
#
# Listen on UDP:161 (the real SNMP transport used by monitoring systems)
# AND TCP:161 (so Consul's tcp health check can probe liveness — Consul
# has no native UDP check). net-snmp binds both from a single agentd.
agentAddress udp:161,tcp:161
sysLocation  stackwiz-managed
sysContact   admin@localhost

# Disable v1/v2c
#rocommunity  (intentionally absent)
#rwcommunity  (intentionally absent)

# Access for the SNMPv3 user
rouser ${snmp_user} priv

# Standard MIBs
view all included .1
EOF

    # 5. Create SNMPv3 user.
    # net-snmp persists users in /var/lib/snmp/snmpd.conf as hashed usmUser
    # entries. On first start, it converts cleartext `createUser` lines from
    # /etc/snmp/snmpd.conf into these hashed entries and removes the cleartext.
    # On re-runs, the hashed entry already exists and createUser is silently
    # ignored OR conflicts. Wipe the persistent DB entirely so createUser
    # always takes effect cleanly.
    rm -f /var/lib/snmp/snmpd.conf
    echo "createUser ${snmp_user} ${auth_proto} \"${auth_key}\" ${priv_proto} \"${priv_key}\"" \
        >> /etc/snmp/snmpd.conf

    # 6. Start
    systemctl enable snmpd >/dev/null 2>&1
    systemctl start snmpd

    # 7. Verify (snmpd needs a moment to process createUser on first start)
    sleep 3
    if snmpget -v3 -u "${snmp_user}" -l authPriv \
        -a "${auth_proto}" -A "${auth_key}" \
        -x "${priv_proto}" -X "${priv_key}" \
        127.0.0.1 .1.3.6.1.2.1.1.1.0 >/dev/null 2>&1; then
        echo "stackwiz-snmp: verified OK (user=${snmp_user}, authPriv ${auth_proto}+${priv_proto})"
    else
        echo "stackwiz-snmp: snmpd running but verify failed (may need a moment to settle)" >&2
    fi
}

stackwiz_snmp_uninstall() {
    systemctl disable --now snmpd 2>/dev/null || true
    # Clean config but preserve the package (other stacks may need it).
    rm -f /etc/snmp/snmpd.conf
    echo "stackwiz-snmp: stopped (Vault keys preserved for other VMs)"
}
