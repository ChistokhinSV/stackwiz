# stackwiz-consul-attach.sh — idempotent helper: attach the `consul`
# container to a consumer-owned docker network so its health-check
# goroutines can resolve that network's container names via docker DNS.
#
# Use case: 081's consul compose already declares stackwiz-prod +
# stackwiz-shared (framework-level nets). When a consumer stack
# introduces its OWN docker network (e.g. 077's `kb-agent_kb-net`,
# created by 077's deploy/docker-compose.yml), something still has to
# attach consul to that net so HTTP/TCP checks against kb-mcpjungle /
# kb-research / etc. resolve. 081 can't declare unknown future
# consumer nets; the consumer must claim the linkage at install time.
#
# Source from a consumer install script (or _compose_env.sh):
#     . "${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}/bin/stackwiz-consul-attach.sh"
#     stackwiz_consul_attach_network kb-agent_kb-net
#     stackwiz_consul_attach_network kb-net        # alias probe
#
# Behavior:
#   * If the target network doesn't exist yet: silent no-op (wait for
#     compose to create it; re-run on next install catches it).
#   * If consul isn't running: no-op (install order may put consumer
#     before consul; hub/server catches up on next install).
#   * If consul is already attached: no-op (docker network connect
#     returns "already exists" — swallowed).
#   * Otherwise: `docker network connect <net> consul`.
#
# Safe to call at the top of every install script; the cost is a few
# docker CLI calls.

stackwiz_consul_attach_network() {
    local net="${1:?usage: stackwiz_consul_attach_network <network>}"

    if ! command -v docker >/dev/null 2>&1; then
        return 0
    fi
    # Bail quietly if consul isn't up yet (common on fresh install
    # before 081 has run).
    if ! docker ps --filter 'name=^consul$' --format '{{.Names}}' 2>/dev/null \
            | grep -qx consul; then
        return 0
    fi
    # Bail if the target network doesn't exist (common when the
    # consumer that creates this net hasn't run compose-up yet).
    if ! docker network inspect "${net}" >/dev/null 2>&1; then
        return 0
    fi
    # Already attached?
    if docker network inspect "${net}" \
        --format '{{range $k,$v := .Containers}}{{(index $v).Name}} {{end}}' \
        2>/dev/null \
        | tr ' ' '\n' | grep -qx consul; then
        return 0
    fi
    if docker network connect "${net}" consul >/dev/null 2>&1; then
        echo "stackwiz-consul-attach: consul joined ${net}"
    else
        # Race or permission problem — log but don't fail the
        # install. Consul's checks may 404 temporarily; the next
        # compose-up in this script will retry.
        echo "stackwiz-consul-attach: WARNING: failed to attach consul to ${net}" >&2
    fi
}
