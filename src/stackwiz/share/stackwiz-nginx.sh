# stackwiz-nginx.sh — Shared nginx reverse proxy for multi-stack VMs.
#
# Source this from a consumer install script:
#     . "${STACKWIZ_STATE_DIR}/bin/stackwiz-nginx.sh"
#
# Public API:
#   stackwiz_nginx_init                          — ensure container + dirs exist
#   stackwiz_nginx_add_conf  NS PRI NAME < file  — drop a namespaced vhost config
#   stackwiz_nginx_add_cert  HOST CERT KEY        — copy cert+key for a hostname
#   stackwiz_nginx_reload                         — nginx -t + nginx -s reload
#   stackwiz_nginx_remove_consumer NS             — remove all configs for NS
#   stackwiz_nginx_ensure_network  NET            — connect container to a docker net
#
# Multiple stackwiz consumers share a single nginx container (stackwiz-nginx)
# on ports 80+443. Each consumer namespaces its config files with a short prefix
# (e.g. "081", "077") so install/uninstall never touches another consumer's
# vhosts. A .consumers registry tracks who is active; the container is torn
# down only when the last consumer deregisters.

STACKWIZ_NGINX_DIR="/opt/stackwiz/nginx"
STACKWIZ_NGINX_CONTAINER="stackwiz-nginx"
STACKWIZ_NGINX_COMPOSE="${STACKWIZ_NGINX_DIR}/compose.yml"
STACKWIZ_NGINX_CONSUMERS="${STACKWIZ_NGINX_DIR}/.consumers"
STACKWIZ_NGINX_LOCK="${STACKWIZ_NGINX_DIR}/.lock"

# ---- Locking (flock) -------------------------------------------------------

_stackwiz_nginx_lock() {
    install -d -m 0755 "${STACKWIZ_NGINX_DIR}"
    exec 9>"${STACKWIZ_NGINX_LOCK}"
    flock -w 30 9 || { echo "stackwiz-nginx: failed to acquire lock" >&2; return 1; }
}

_stackwiz_nginx_unlock() {
    flock -u 9 2>/dev/null || true
}

# ---- Consumer registry ------------------------------------------------------

_stackwiz_nginx_register() {
    local ns="$1"
    touch "${STACKWIZ_NGINX_CONSUMERS}"
    if ! grep -qxF "${ns}" "${STACKWIZ_NGINX_CONSUMERS}" 2>/dev/null; then
        echo "${ns}" >> "${STACKWIZ_NGINX_CONSUMERS}"
    fi
}

_stackwiz_nginx_deregister() {
    local ns="$1"
    if [ -f "${STACKWIZ_NGINX_CONSUMERS}" ]; then
        local tmp="${STACKWIZ_NGINX_CONSUMERS}.tmp"
        grep -vxF "${ns}" "${STACKWIZ_NGINX_CONSUMERS}" > "${tmp}" 2>/dev/null || true
        mv "${tmp}" "${STACKWIZ_NGINX_CONSUMERS}"
    fi
}

_stackwiz_nginx_consumer_count() {
    if [ -f "${STACKWIZ_NGINX_CONSUMERS}" ]; then
        grep -c . "${STACKWIZ_NGINX_CONSUMERS}" 2>/dev/null || echo 0
    else
        echo 0
    fi
}

# ---- Container lifecycle ----------------------------------------------------

_stackwiz_nginx_write_compose() {
    # The compose file lives alongside the consumer dir at a well-known path.
    # It is framework-owned — consumers MUST NOT edit it.
    local bin_dir="${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}/bin"
    if [ -f "${bin_dir}/stackwiz-nginx-compose.yml" ]; then
        cp "${bin_dir}/stackwiz-nginx-compose.yml" "${STACKWIZ_NGINX_COMPOSE}"
    else
        # Inline fallback if the staged file is missing (shouldn't happen).
        cat > "${STACKWIZ_NGINX_COMPOSE}" <<'YAML'
services:
  nginx:
    image: nginxinc/nginx-unprivileged:alpine
    container_name: stackwiz-nginx
    restart: unless-stopped
    volumes:
      - /opt/stackwiz/nginx/conf.d:/etc/nginx/conf.d:ro
      - /opt/stackwiz/nginx/tls:/etc/nginx/tls:ro
    ports:
      - "80:8080"
      - "443:8443"
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://127.0.0.1:8080/healthz"]
      interval: 15s
      timeout: 3s
      retries: 5
      start_period: 5s
networks:
  default:
    name: stackwiz-shared
YAML
    fi
}

_stackwiz_nginx_write_default_conf() {
    local bin_dir="${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}/bin"
    if [ -f "${bin_dir}/stackwiz-nginx-default.conf" ]; then
        cp "${bin_dir}/stackwiz-nginx-default.conf" \
           "${STACKWIZ_NGINX_DIR}/conf.d/00-stackwiz-default.conf"
    else
        cat > "${STACKWIZ_NGINX_DIR}/conf.d/00-stackwiz-default.conf" <<'CONF'
server {
    listen 8080 default_server;
    server_name _;
    location = /healthz { access_log off; return 200 "ok\n"; }
    location / { return 301 https://$host$request_uri; }
}
CONF
    fi
}

_stackwiz_nginx_ensure_container() {
    if docker ps --format '{{.Names}}' | grep -qxF "${STACKWIZ_NGINX_CONTAINER}"; then
        return 0
    fi
    # Remove stale container (e.g. referencing a deleted network) so
    # compose recreates it cleanly.
    docker rm -f "${STACKWIZ_NGINX_CONTAINER}" 2>/dev/null || true
    if [ ! -f "${STACKWIZ_NGINX_COMPOSE}" ]; then
        _stackwiz_nginx_write_compose
    fi
    docker compose -f "${STACKWIZ_NGINX_COMPOSE}" up -d
}

_stackwiz_nginx_teardown() {
    if [ -f "${STACKWIZ_NGINX_COMPOSE}" ]; then
        docker compose -f "${STACKWIZ_NGINX_COMPOSE}" down 2>/dev/null || true
    fi
    docker rm -f "${STACKWIZ_NGINX_CONTAINER}" 2>/dev/null || true
    rm -f "${STACKWIZ_NGINX_COMPOSE}"
}

# ---- Public API -------------------------------------------------------------

stackwiz_nginx_init() {
    # Idempotent: create dirs, default conf, compose file, start container.
    install -d -m 0755 "${STACKWIZ_NGINX_DIR}/conf.d" "${STACKWIZ_NGINX_DIR}/tls"

    if [ ! -f "${STACKWIZ_NGINX_DIR}/conf.d/00-stackwiz-default.conf" ]; then
        _stackwiz_nginx_write_default_conf
    fi

    _stackwiz_nginx_lock
    _stackwiz_nginx_ensure_container
    _stackwiz_nginx_unlock

    # Make tls dir readable by nginx-unprivileged (uid 101).
    chmod -R go+rX "${STACKWIZ_NGINX_DIR}/conf.d" "${STACKWIZ_NGINX_DIR}/tls" 2>/dev/null || true
}

stackwiz_nginx_add_conf() {
    # Usage: stackwiz_nginx_add_conf <namespace> <priority> <name> < rendered.conf
    #    OR: stackwiz_nginx_add_conf <namespace> <priority> <name> /path/to/file
    local ns="${1:?namespace required}"
    local pri="${2:?priority required}"
    local name="${3:?name required}"
    local file="${4:-}"
    local target="${STACKWIZ_NGINX_DIR}/conf.d/${ns}--${pri}-${name}.conf"

    if [ -n "${file}" ] && [ -f "${file}" ]; then
        cp "${file}" "${target}"
    else
        cat > "${target}"
    fi
    chmod 644 "${target}"

    _stackwiz_nginx_lock
    _stackwiz_nginx_register "${ns}"
    _stackwiz_nginx_unlock
}

stackwiz_nginx_add_cert() {
    # Usage: stackwiz_nginx_add_cert <hostname> <cert_path> <key_path>
    local host="${1:?hostname required}"
    local cert="${2:?cert_path required}"
    local key="${3:?key_path required}"
    install -m 0644 "${cert}" "${STACKWIZ_NGINX_DIR}/tls/${host}.crt"
    install -m 0644 "${key}"  "${STACKWIZ_NGINX_DIR}/tls/${host}.key"
}

stackwiz_nginx_reload() {
    # Validate config first — fail loudly so callers' set -e catches it.
    if ! docker exec "${STACKWIZ_NGINX_CONTAINER}" nginx -t 2>&1; then
        echo "stackwiz-nginx: config test FAILED — reload skipped" >&2
        return 1
    fi
    docker exec "${STACKWIZ_NGINX_CONTAINER}" nginx -s reload
}

stackwiz_nginx_remove_consumer() {
    # Remove all configs for a namespace. If last consumer, tear down container.
    local ns="${1:?namespace required}"

    # Remove this consumer's config files.
    rm -f "${STACKWIZ_NGINX_DIR}/conf.d/${ns}--"*.conf

    _stackwiz_nginx_lock
    _stackwiz_nginx_deregister "${ns}"

    local count
    count="$(_stackwiz_nginx_consumer_count)"
    if [ "${count}" -eq 0 ]; then
        echo "stackwiz-nginx: last consumer removed — tearing down container"
        _stackwiz_nginx_teardown
        # Clean up the directory skeleton but keep tls/ (certs may be shared).
        rm -rf "${STACKWIZ_NGINX_DIR}/conf.d"
        rm -f "${STACKWIZ_NGINX_CONSUMERS}" "${STACKWIZ_NGINX_LOCK}"
    else
        _stackwiz_nginx_unlock
        # Reload so nginx drops the removed vhosts.
        if docker ps --format '{{.Names}}' | grep -qxF "${STACKWIZ_NGINX_CONTAINER}"; then
            stackwiz_nginx_reload || true
        fi
        return 0
    fi
    _stackwiz_nginx_unlock
}

stackwiz_nginx_ensure_network() {
    # Connect the shared nginx container to an additional docker network
    # so it can reach upstream containers on that network.
    local network="${1:?network name required}"
    docker network create "${network}" >/dev/null 2>&1 || true
    if ! docker network inspect "${network}" --format '{{range .Containers}}{{.Name}} {{end}}' \
         | grep -qw "${STACKWIZ_NGINX_CONTAINER}"; then
        docker network connect "${network}" "${STACKWIZ_NGINX_CONTAINER}" 2>/dev/null || true
    fi
}
