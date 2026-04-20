# stackwiz-consul-agent.sh — framework helper: install a local Consul
# client agent on this host, joined to the central server cluster.
#
# Source from consumer install scripts:
#     . "${STACKWIZ_STATE_DIR}/bin/stackwiz-consul-agent.sh"
#     stackwiz_consul_agent_install
#
# What it does:
#   1. If the local host already runs a `consul` docker container as
#      the SERVER (081 runs here): skip — the server IS this node's
#      agent, no client needed. Prints "skipped: local-server".
#   2. Fetches the consul binary (native install, systemd-managed —
#      matches stackwiz-snmp.sh's non-docker pattern).
#   3. Reads gossip key + server address + client ACL token from
#      stackwiz/data/shared/consul_{gossip_key,server_addr,client_token}
#      (published by 081 at install time).
#   4. Renders /etc/consul.d/agent.hcl from the bundled template.
#   5. Installs + starts a systemd unit `stackwiz-consul-agent.service`.
#   6. Waits ≤30s for `consul members` to list this node.
#
# Uninstall mirror: stop unit, `consul leave`, remove config.
#
# Why native (not docker): gossip (port 8301 TCP+UDP) needs
# host-network semantics for reliable multi-node discovery. Running
# the agent in a bridge-network container requires macvlan or
# host-mode, both of which defeat the point of having a lightweight
# per-host daemon.

STACKWIZ_CONSUL_AGENT_BIN="${STACKWIZ_CONSUL_AGENT_BIN:-/usr/local/bin/consul}"
STACKWIZ_CONSUL_AGENT_VERSION="${STACKWIZ_CONSUL_AGENT_VERSION:-1.22.6}"
STACKWIZ_CONSUL_AGENT_CONFIG_DIR="/etc/consul.d"
STACKWIZ_CONSUL_AGENT_DATA_DIR="/var/lib/consul-agent"
STACKWIZ_CONSUL_AGENT_UNIT="stackwiz-consul-agent.service"
STACKWIZ_CONSUL_AGENT_USER="consul-agent"

_consul_agent_have_local_server() {
    # Returns 0 if a consul container is running on this host AND
    # that container is the server (bootstrap_expect=1 or similar).
    # In practice: if *any* container named exactly `consul` is up,
    # the local server IS present and running as the agent too.
    if ! command -v docker >/dev/null 2>&1; then
        return 1
    fi
    docker ps --filter 'name=^consul$' --format '{{.Names}}' 2>/dev/null \
        | grep -qx consul
}

_consul_agent_vault_token() {
    # Same fallback order as stackwiz-snmp.sh: project token on disk
    # over the install-scoped env token (which is RO on shared/*,
    # enough for reads but not writes — agents only read).
    local state="${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}"
    for f in "${state}/vault-token" "${state}"/*/vault-token; do
        [ -f "$f" ] && cat "$f" && return 0
    done
    if [ -n "${VAULT_TOKEN:-}" ]; then echo "$VAULT_TOKEN"; return 0; fi
    echo ""
}

_consul_agent_vault_curl_tls() {
    if [ "${STACKWIZ_VAULT_VERIFY:-true}" = "false" ]; then
        echo "-k"
    fi
}

_consul_agent_read_shared() {
    # $1 = key name under shared/, e.g. consul_gossip_key
    local addr token key url
    addr="${VAULT_ADDR:?stackwiz-consul-agent: VAULT_ADDR required}"
    token="$(_consul_agent_vault_token)"
    [ -z "$token" ] && { echo "stackwiz-consul-agent: no vault token" >&2; return 1; }
    key="$1"
    url="${addr}/v1/stackwiz/data/shared/${key}"
    # shellcheck disable=SC2046
    curl -sf $(_consul_agent_vault_curl_tls) \
        -H "X-Vault-Token: ${token}" "${url}" 2>/dev/null \
        | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)['data']['data']
    print(d.get('value', ''))
except Exception:
    pass
"
}

_consul_agent_ensure_binary() {
    if [ -x "${STACKWIZ_CONSUL_AGENT_BIN}" ]; then
        local have
        have="$("${STACKWIZ_CONSUL_AGENT_BIN}" version 2>/dev/null | awk 'NR==1{print $2}' | tr -d v)"
        if [ "${have}" = "${STACKWIZ_CONSUL_AGENT_VERSION}" ]; then
            return 0
        fi
        echo "stackwiz-consul-agent: replacing consul ${have} → ${STACKWIZ_CONSUL_AGENT_VERSION}"
    fi
    local arch url tmp
    case "$(uname -m)" in
        x86_64) arch=amd64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) echo "stackwiz-consul-agent: unsupported arch $(uname -m)" >&2; return 1 ;;
    esac
    url="https://releases.hashicorp.com/consul/${STACKWIZ_CONSUL_AGENT_VERSION}/consul_${STACKWIZ_CONSUL_AGENT_VERSION}_linux_${arch}.zip"
    tmp="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '${tmp}'" EXIT
    if ! command -v unzip >/dev/null 2>&1; then
        apt-get update -qq >/dev/null 2>&1
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq unzip >/dev/null 2>&1
    fi
    curl -fsSL "${url}" -o "${tmp}/consul.zip"
    unzip -q "${tmp}/consul.zip" -d "${tmp}"
    install -m 0755 "${tmp}/consul" "${STACKWIZ_CONSUL_AGENT_BIN}"
    echo "stackwiz-consul-agent: installed consul ${STACKWIZ_CONSUL_AGENT_VERSION} at ${STACKWIZ_CONSUL_AGENT_BIN}"
}

_consul_agent_ensure_user() {
    if ! id -u "${STACKWIZ_CONSUL_AGENT_USER}" >/dev/null 2>&1; then
        useradd --system --no-create-home --shell /usr/sbin/nologin \
            "${STACKWIZ_CONSUL_AGENT_USER}"
    fi
    install -d -o "${STACKWIZ_CONSUL_AGENT_USER}" -g "${STACKWIZ_CONSUL_AGENT_USER}" \
        -m 0750 "${STACKWIZ_CONSUL_AGENT_DATA_DIR}"
    install -d -o root -g "${STACKWIZ_CONSUL_AGENT_USER}" -m 0750 \
        "${STACKWIZ_CONSUL_AGENT_CONFIG_DIR}"
}

_consul_agent_write_config() {
    local gossip_key server_addr client_token hostname node_ip
    gossip_key="$(_consul_agent_read_shared consul_gossip_key)"
    server_addr="$(_consul_agent_read_shared consul_server_addr)"
    client_token="$(_consul_agent_read_shared consul_client_token)"
    if [ -z "${gossip_key}" ] || [ -z "${server_addr}" ]; then
        echo "stackwiz-consul-agent: shared/consul_{gossip_key,server_addr} missing in Vault — 081 needs to run first" >&2
        return 1
    fi
    hostname="$(hostname -s 2>/dev/null || hostname)"
    node_ip="${WIZ_CFG_NODE_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
    # Strip ":port" if server_addr already carries one — retry_join
    # wants host-only entries in the join list.
    local join_host="${server_addr%%:*}"
    local template_dir="${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}/bin"
    local template="${template_dir}/stackwiz-consul-client.hcl.j2"
    if [ ! -f "${template}" ]; then
        echo "stackwiz-consul-agent: template not found at ${template}" >&2
        return 1
    fi
    local tmp
    tmp="$(mktemp)"
    # Token may be empty if 081 didn't publish one yet — render an
    # empty `acl.tokens.default` stanza in that case.
    local acl_token_stanza=""
    if [ -n "${client_token}" ]; then
        acl_token_stanza="tokens { default = \"${client_token}\" }"
    fi
    CONSUL_NODE_NAME="${hostname}" \
    CONSUL_ADVERTISE_ADDR="${node_ip}" \
    CONSUL_GOSSIP_KEY="${gossip_key}" \
    CONSUL_RETRY_JOIN="${join_host}" \
    CONSUL_DATA_DIR="${STACKWIZ_CONSUL_AGENT_DATA_DIR}" \
    CONSUL_ACL_TOKEN_STANZA="${acl_token_stanza}" \
        envsubst '${CONSUL_NODE_NAME} ${CONSUL_ADVERTISE_ADDR} ${CONSUL_GOSSIP_KEY} ${CONSUL_RETRY_JOIN} ${CONSUL_DATA_DIR} ${CONSUL_ACL_TOKEN_STANZA}' \
        < "${template}" > "${tmp}"
    install -m 0640 -o root -g "${STACKWIZ_CONSUL_AGENT_USER}" \
        "${tmp}" "${STACKWIZ_CONSUL_AGENT_CONFIG_DIR}/agent.hcl"
    rm -f "${tmp}"
}

_consul_agent_write_systemd() {
    cat > "/etc/systemd/system/${STACKWIZ_CONSUL_AGENT_UNIT}" <<EOF
[Unit]
Description=Stackwiz Consul Client Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=${STACKWIZ_CONSUL_AGENT_USER}
Group=${STACKWIZ_CONSUL_AGENT_USER}
ExecStart=${STACKWIZ_CONSUL_AGENT_BIN} agent -config-dir=${STACKWIZ_CONSUL_AGENT_CONFIG_DIR}
ExecReload=/bin/kill -HUP \$MAINPID
KillSignal=SIGTERM
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
}

stackwiz_consul_agent_install() {
    if _consul_agent_have_local_server; then
        echo "stackwiz-consul-agent: skipped (local consul server container is running — it acts as this node's agent)"
        return 0
    fi
    _consul_agent_ensure_binary
    _consul_agent_ensure_user
    _consul_agent_write_config
    _consul_agent_write_systemd
    systemctl enable --now "${STACKWIZ_CONSUL_AGENT_UNIT}"
    local i
    for i in $(seq 1 30); do
        if "${STACKWIZ_CONSUL_AGENT_BIN}" members 2>/dev/null | grep -q "$(hostname -s)"; then
            echo "stackwiz-consul-agent: joined — consul members lists $(hostname -s)"
            # Marker file read by the stackwiz engine (_consul_client_kwargs
            # in engine.py) to skip the 127.0.0.1→node_ip check rewrite —
            # with a native local agent, 127.0.0.1 already resolves to the
            # same loopback services bind to.
            install -d -m 0755 "${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}"
            : > "${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}/local-consul-agent"
            return 0
        fi
        sleep 1
    done
    echo "stackwiz-consul-agent: WARNING: did not appear in 'consul members' within 30s" >&2
    echo "stackwiz-consul-agent: last 20 journal lines:" >&2
    journalctl -u "${STACKWIZ_CONSUL_AGENT_UNIT}" -n 20 --no-pager >&2 || true
    return 1
}

stackwiz_consul_agent_uninstall() {
    if _consul_agent_have_local_server; then
        echo "stackwiz-consul-agent: no agent to uninstall (local server runs here)"
        return 0
    fi
    if systemctl is-active --quiet "${STACKWIZ_CONSUL_AGENT_UNIT}" 2>/dev/null; then
        # Clean leave so the server deregisters this node immediately
        # instead of waiting for gossip timeout.
        "${STACKWIZ_CONSUL_AGENT_BIN}" leave 2>/dev/null || true
    fi
    systemctl disable --now "${STACKWIZ_CONSUL_AGENT_UNIT}" 2>/dev/null || true
    rm -f "/etc/systemd/system/${STACKWIZ_CONSUL_AGENT_UNIT}"
    systemctl daemon-reload
    rm -f "${STACKWIZ_CONSUL_AGENT_CONFIG_DIR}/agent.hcl"
    rm -f "${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}/local-consul-agent"
    echo "stackwiz-consul-agent: uninstalled"
}

stackwiz_consul_agent_status() {
    if _consul_agent_have_local_server; then
        echo "skipped: local-server"
        return 0
    fi
    if systemctl is-active --quiet "${STACKWIZ_CONSUL_AGENT_UNIT}" 2>/dev/null; then
        echo "running"
    else
        echo "stopped"
    fi
}
