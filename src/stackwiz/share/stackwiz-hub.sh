# stackwiz-hub.sh — framework helper: ensure stackwiz-hub daemon is up.
#
# Source from consumer install scripts (or call from engine's auto-
# ensure path):
#
#     . "${STACKWIZ_STATE_DIR}/bin/stackwiz-hub.sh"
#     stackwiz_hub_ensure
#
# One hub per host. Reads registry entries from Consul KV at
# stackwiz/registry/* and reconciles KB sync + MCPJungle registration.
# See src/stackwiz_hub/ for the reconciler.
#
# Idempotent: re-running just refreshes the env + checks the
# container's healthy. Never deletes — uninstalling the last consumer
# isn't a signal to kill the hub (the operator might be mid-migration).

STACKWIZ_HUB_DIR="/opt/stackwiz/hub"
STACKWIZ_HUB_CONTAINER="stackwiz-hub"
STACKWIZ_HUB_COMPOSE="${STACKWIZ_HUB_DIR}/compose.yml"
STACKWIZ_HUB_ENV="${STACKWIZ_HUB_DIR}/.env"

stackwiz_hub_ensure() {
    # Usage: stackwiz_hub_ensure
    #
    # Reads the following env vars (all optional, sensible defaults):
    #   STACKWIZ_HUB_IMAGE           default ghcr.io/chistokhinsv/stackwiz-hub:latest
    #   STACKWIZ_HUB_CONSUL_ADDR     default http://consul:8500
    #   STACKWIZ_HUB_CONSUL_TOKEN    default empty
    #   STACKWIZ_HUB_VAULT_ADDR      default empty (hub skips vault lookups)
    #   STACKWIZ_HUB_VAULT_TOKEN     default empty
    #   STACKWIZ_HUB_VAULT_MOUNT     default stackwiz
    #   STACKWIZ_HUB_MCPJUNGLE_URL   default http://mcpjungle:8080
    install -d -m 0755 "${STACKWIZ_HUB_DIR}"

    # Stage the compose file from the framework's bundled share/.
    # The engine copies share/ into ${STATE_DIR}/bin on every run.
    local src_compose="${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}/bin/stackwiz-hub-compose.yml"
    if [ -f "${src_compose}" ]; then
        install -m 0644 "${src_compose}" "${STACKWIZ_HUB_COMPOSE}"
    else
        echo "stackwiz-hub: no bundled compose at ${src_compose}" >&2
        return 1
    fi

    # Write env. Operator overrides via explicit vars (engine sets
    # these from discovery + the hub's freshly-minted vault token).
    umask 077
    cat > "${STACKWIZ_HUB_ENV}" <<EOF
STACKWIZ_HUB_IMAGE=${STACKWIZ_HUB_IMAGE:-ghcr.io/chistokhinsv/stackwiz-hub:latest}
STACKWIZ_HUB_CONSUL_ADDR=${STACKWIZ_HUB_CONSUL_ADDR:-http://consul:8500}
STACKWIZ_HUB_CONSUL_TOKEN=${STACKWIZ_HUB_CONSUL_TOKEN:-}
STACKWIZ_HUB_VAULT_ADDR=${STACKWIZ_HUB_VAULT_ADDR:-}
STACKWIZ_HUB_VAULT_TOKEN=${STACKWIZ_HUB_VAULT_TOKEN:-}
STACKWIZ_HUB_VAULT_MOUNT=${STACKWIZ_HUB_VAULT_MOUNT:-stackwiz}
STACKWIZ_HUB_MCPJUNGLE_URL=${STACKWIZ_HUB_MCPJUNGLE_URL:-http://mcpjungle:8080}
EOF
    umask 022

    # stackwiz-shared must exist before compose can attach.
    docker network inspect stackwiz-shared >/dev/null 2>&1 \
        || docker network create stackwiz-shared >/dev/null

    docker compose \
        --file "${STACKWIZ_HUB_COMPOSE}" \
        --env-file "${STACKWIZ_HUB_ENV}" \
        up -d 2>&1 | grep -vE '^\s*$' || true

    echo "stackwiz-hub: ensured container ${STACKWIZ_HUB_CONTAINER} up"
}

stackwiz_hub_status() {
    # Usage: stackwiz_hub_status
    # Prints: running | stopped | absent
    if ! docker inspect "${STACKWIZ_HUB_CONTAINER}" >/dev/null 2>&1; then
        echo "absent"; return 0
    fi
    if docker ps --format '{{.Names}}' | grep -qx "${STACKWIZ_HUB_CONTAINER}"; then
        echo "running"
    else
        echo "stopped"
    fi
}
