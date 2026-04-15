# stackwiz-host.sh — cross-cutting helpers for use inside install scripts.
#
# Staged by the engine into ${STACKWIZ_STATE_DIR}/bin/stackwiz-host.sh.
# Source from any install script:
#     . "${STACKWIZ_STATE_DIR}/bin/stackwiz-host.sh"
#
# Convention: each helper either succeeds or returns non-zero with a clear
# message on stderr. None of them print to stdout unless documented — install
# scripts capture their output into config or envsubst pipelines.

# Returns 0 if the named systemd unit exists (loaded or not), 1 otherwise.
# Use before `systemctl enable/start` to keep install scripts idempotent.
sw_host_has_systemd_unit() {
    local unit="${1:?sw_host_has_systemd_unit: unit name required}"
    systemctl list-unit-files "${unit}" --no-legend 2>/dev/null \
        | grep -q "^${unit//./\\.}[[:space:]]"
}

# Render a template file by substituting an allowlist of environment vars
# via envsubst, writing to a destination. Always whitelists the vars so
# YAML tags (!Find, !KeyOf) and shell $VARs in the template are not mangled.
#
# Usage:
#     sw_host_render_template \
#         "${STACKWIZ_STATE_DIR}/templates/authentik/ldap.yaml" \
#         /opt/authentik/blueprints/ldap.yaml \
#         AUTHENTIK_HOSTNAME LDAP_BASE_DN
sw_host_render_template() {
    local src="${1:?sw_host_render_template: source template required}"
    local dst="${2:?sw_host_render_template: destination path required}"
    shift 2
    local vars=""
    local v
    for v in "$@"; do
        vars="${vars}\${${v}} "
    done
    if [ -z "$vars" ]; then
        echo "sw_host_render_template: pass at least one var name to whitelist" >&2
        return 2
    fi
    local tmp; tmp="$(mktemp)"
    # shellcheck disable=SC2016  # literal ${…} is intentional for envsubst
    envsubst "${vars}" < "$src" > "$tmp" || { rm -f "$tmp"; return 1; }
    install -m 0644 "$tmp" "$dst"
    rm -f "$tmp"
}

# Register a service with Consul. Reads CONSUL_HTTP_ADDR and, if set,
# CONSUL_HTTP_TOKEN. JSON payload is passed on stdin.
#
# Usage:
#     cat <<JSON | sw_host_consul_register
#     {"ID":"authentik","Name":"authentik","Port":9000,
#      "Check":{"HTTP":"http://127.0.0.1:9000/-/health/ready/","Interval":"30s"}}
#     JSON
sw_host_consul_register() {
    local addr="${CONSUL_HTTP_ADDR:?sw_host_consul_register: CONSUL_HTTP_ADDR not set}"
    local token="${CONSUL_HTTP_TOKEN:-}"
    local headers=()
    [ -n "$token" ] && headers=(-H "X-Consul-Token: ${token}")
    curl -sSf "${headers[@]}" --data-binary @- \
        "${addr}/v1/agent/service/register" >/dev/null
}

# Vault token fallback chain — exports VAULT_TOKEN to the caller's env.
# Resolution order (first non-empty wins):
#   1. current VAULT_TOKEN env var
#   2. <manifest_dir>/.env  (VAULT_TOKEN=… line)
#   3. /var/lib/stackwiz/stackwiz-tokens/<prefix>.token   (per-consumer)
#   4. /var/lib/stackwiz/vault-token                      (shared fallback)
#
# Usage:
#     sw_host_vault_token "awx" || { echo "no vault token available" >&2; exit 1; }
#     # VAULT_TOKEN is now exported
sw_host_vault_token() {
    local prefix="${1:-${WIZ_CONSUL_SERVICE_PREFIX:-}}"
    local state_dir="${STACKWIZ_HOST_STATE_DIR:-/var/lib/stackwiz}"
    local manifest_dir="${WIZ_MANIFEST_DIR:-${STACKWIZ_HOST_MANIFEST_DIR:-}}"

    if [ -n "${VAULT_TOKEN:-}" ]; then
        export VAULT_TOKEN
        return 0
    fi

    if [ -n "$manifest_dir" ] && [ -f "${manifest_dir}/.env" ]; then
        local v
        v=$(grep -E '^VAULT_TOKEN=' "${manifest_dir}/.env" 2>/dev/null | tail -n1 | cut -d= -f2-)
        v="${v%\"}"; v="${v#\"}"
        if [ -n "$v" ]; then
            VAULT_TOKEN="$v"; export VAULT_TOKEN; return 0
        fi
    fi

    if [ -n "$prefix" ] && [ -f "${state_dir}/stackwiz-tokens/${prefix}.token" ]; then
        VAULT_TOKEN="$(cat "${state_dir}/stackwiz-tokens/${prefix}.token")"
        export VAULT_TOKEN
        [ -n "$VAULT_TOKEN" ] && return 0
    fi

    if [ -f "${state_dir}/vault-token" ]; then
        VAULT_TOKEN="$(cat "${state_dir}/vault-token")"
        export VAULT_TOKEN
        [ -n "$VAULT_TOKEN" ] && return 0
    fi

    return 1
}
