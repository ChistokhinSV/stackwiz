# stackwiz-snmp.sh — SNMPv3 agent (authPriv) for every stackwiz VM.
#
# Source from consumer install scripts:
#     . "${STACKWIZ_STATE_DIR}/bin/stackwiz-snmp.sh"
#     stackwiz_snmp_install
#
# Installs snmpd with SNMPv3 authPriv (SHA + AES). Credentials are
# stored in Vault at stackwiz/data/shared/snmpv3 — generated on first
# run, reused across all VMs so a single credential set monitors the
# entire fleet.
#
# Requires: VAULT_ADDR set, vault-token readable in state dir.

STACKWIZ_SNMP_USER="stackwiz"
STACKWIZ_SNMP_VAULT_PATH="shared/snmpv3"

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

_stackwiz_snmp_vault_get() {
    local addr token
    addr="$(_stackwiz_snmp_vault_addr)"
    token="$(_stackwiz_snmp_vault_token)"
    [ -z "$token" ] && return 1
    curl -sfk -H "X-Vault-Token: ${token}" \
        "${addr}/v1/stackwiz/data/${STACKWIZ_SNMP_VAULT_PATH}" 2>/dev/null \
        | python3 -c 'import sys,json
d=json.load(sys.stdin).get("data",{}).get("data",{})
if d.get("auth_key"):
    for k in ("snmp_user","auth_protocol","auth_key","priv_protocol","priv_key"):
        print(d.get(k,""))
' 2>/dev/null
}

_stackwiz_snmp_vault_put() {
    local user="$1" auth_key="$2" priv_key="$3"
    local addr token
    addr="$(_stackwiz_snmp_vault_addr)"
    token="$(_stackwiz_snmp_vault_token)"
    [ -z "$token" ] && { echo "stackwiz-snmp: no vault token — cannot store keys" >&2; return 1; }
    curl -sfk -X PUT -H "X-Vault-Token: ${token}" \
        -H "Content-Type: application/json" \
        "${addr}/v1/stackwiz/data/${STACKWIZ_SNMP_VAULT_PATH}" \
        -d "{\"data\":{\"snmp_user\":\"${user}\",\"auth_protocol\":\"SHA\",\"auth_key\":\"${auth_key}\",\"priv_protocol\":\"AES\",\"priv_key\":\"${priv_key}\"}}" \
        >/dev/null 2>&1
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
        echo "stackwiz-snmp: using existing credentials from Vault"
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
agentAddress udp:161
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
        127.0.0.1 sysDescr.0 >/dev/null 2>&1; then
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
