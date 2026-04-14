# stackwiz-kb-host-article.sh — auto-generate a KB article describing
# the current host: system info, containers, Consul services, websites.
#
# Source from consumer install scripts:
#     . "${STACKWIZ_STATE_DIR}/bin/stackwiz-kb-host-article.sh"
#     stackwiz_generate_host_article /opt/awx-mcp/kb
#
# Creates/overwrites: <kb_dir>/articles/hosts/<hostname>.md
# Tagged: host, infrastructure, auto-generated, <project-tag>
#
# Designed to be called at the end of every provision run so the
# article always reflects current state. The kb-source-sync sidecar
# pushes it to the central KB automatically.

STATE_DIR="${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}"

_host_article_vault_token() {
    if [ -n "${VAULT_TOKEN:-}" ]; then echo "$VAULT_TOKEN"; return 0; fi
    for f in "${STATE_DIR}/vault-token" "${STATE_DIR}"/*/vault-token; do
        [ -f "$f" ] && cat "$f" && return 0
    done
    echo ""
}

stackwiz_generate_host_article() {
    local kb_dir="${1:?usage: stackwiz_generate_host_article <kb_dir>}"
    local hostname project_name project_tag node_ip domain

    hostname="$(hostname -s 2>/dev/null || hostname)"
    project_name="${WIZ_MANIFEST_NAME:-$(basename "${WIZ_MANIFEST_DIR:-unknown}")}"
    project_tag="$(echo "$project_name" | tr '[:upper:] ' '[:lower:]-' | sed 's/[^a-z0-9-]//g')"
    node_ip="${WIZ_CFG_NODE_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
    domain="${WIZ_CFG_DOMAIN:-${DOMAIN:-unknown}}"

    local article_dir="${kb_dir}/articles/hosts"
    local article_file="${article_dir}/${hostname}.md"
    mkdir -p "$article_dir"

    # Collect system info.
    local os_info kernel uptime_str
    os_info="$(cat /etc/os-release 2>/dev/null | grep '^PRETTY_NAME=' | cut -d= -f2- | tr -d '"' || echo 'unknown')"
    kernel="$(uname -r 2>/dev/null || echo 'unknown')"
    uptime_str="$(uptime -p 2>/dev/null || uptime | sed 's/.*up /up /' || echo 'unknown')"

    # Collect containers.
    local containers=""
    if command -v docker >/dev/null 2>&1; then
        containers="$(docker ps --format '| {{.Names}} | {{.Image}} | {{.Status}} |' 2>/dev/null || true)"
    fi

    # Collect Consul services on this node.
    local consul_services=""
    local consul_addr="${CONSUL_HTTP_ADDR:-}"
    local consul_token
    consul_token="$(_host_article_vault_token)"
    if [ -n "$consul_addr" ] && [ -n "$consul_token" ]; then
        consul_services="$(curl -sf \
            -H "X-Consul-Token: ${consul_token}" \
            "${consul_addr}/v1/catalog/node/${hostname}" 2>/dev/null \
            | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    for svc in (data.get("Services") or {}).values():
        name = svc.get("Service", "")
        port = svc.get("Port", 0)
        tags = ", ".join(svc.get("Tags") or [])
        print(f"| {name} | {port} | {tags} |")
except Exception:
    pass
' 2>/dev/null || true)"
    fi

    # Collect configured hostnames/websites.
    local websites=""
    for var in $(env | grep -oP 'WIZ_CFG_\w*_HOSTNAME' | sort -u); do
        local val="${!var:-}"
        if [ -n "$val" ]; then
            local label="${var#WIZ_CFG_}"
            label="${label%_HOSTNAME}"
            label="$(echo "$label" | tr '[:upper:]_' '[:lower:] ')"
            websites="${websites}| ${label} | https://${val} |
"
        fi
    done

    # Collect Vault credential paths.
    local vault_keys=""
    if [ -n "${VAULT_ADDR:-}" ] && [ -n "$consul_token" ]; then
        vault_keys="$(curl -sf \
            -H "X-Vault-Token: ${consul_token}" \
            "${VAULT_ADDR}/v1/stackwiz/data/shared/hosts/${hostname}" 2>/dev/null \
            | python3 -c '
import json, sys
try:
    keys = list(json.load(sys.stdin)["data"]["data"].keys())
    print(", ".join(keys))
except Exception:
    pass
' 2>/dev/null || true)"
    fi

    # Write the article.
    cat > "$article_file" <<ARTICLE
---
title: "Host: ${hostname}"
description: "${project_name} on ${node_ip}"
tags: host, infrastructure, auto-generated, ${project_tag}
---

# Host: ${hostname}

| Field | Value |
|-------|-------|
| Hostname | ${hostname} |
| IP | ${node_ip} |
| Project | ${project_name} |
| Domain | ${domain} |
| OS | ${os_info} |
| Kernel | ${kernel} |
| Uptime | ${uptime_str} |

ARTICLE

    if [ -n "$consul_services" ]; then
        cat >> "$article_file" <<SECTION
## Consul Services

| Service | Port | Tags |
|---------|------|------|
${consul_services}

SECTION
    fi

    if [ -n "$containers" ]; then
        cat >> "$article_file" <<SECTION
## Containers

| Name | Image | Status |
|------|-------|--------|
${containers}

SECTION
    fi

    if [ -n "$websites" ]; then
        cat >> "$article_file" <<SECTION
## Websites

| Name | URL |
|------|-----|
${websites}
SECTION
    fi

    if [ -n "$vault_keys" ]; then
        cat >> "$article_file" <<SECTION
## Vault Credentials

| Path | Keys |
|------|------|
| shared/hosts/${hostname} | ${vault_keys} |

SECTION
    fi

    cat >> "$article_file" <<SECTION
## See also

- [Infrastructure Hosts](../platform/infrastructure-hosts)
SECTION

    echo "stackwiz-kb-host-article: generated ${article_file}"
}
