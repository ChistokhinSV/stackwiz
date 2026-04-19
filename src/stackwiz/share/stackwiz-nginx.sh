# stackwiz-nginx.sh — Shared nginx reverse proxy for multi-stack VMs.
#
# Source this from a consumer install script:
#     . "${STACKWIZ_STATE_DIR}/bin/stackwiz-nginx.sh"
#
# Public API:
#   stackwiz_nginx_init                          — ensure container + dirs exist
#   stackwiz_nginx_add_conf  NS PRI NAME < file  — drop a namespaced vhost config
#   stackwiz_nginx_add_cert  HOST CERT KEY        — copy cert+key for a hostname
#   stackwiz_nginx_add_forwardauth_proxy         — TLS vhost gated by Authentik outpost
#   stackwiz_nginx_add_basicauth_proxy           — TLS vhost gated by htpasswd
#   stackwiz_nginx_write_htpasswd HOST USER PASS — write bcrypt htpasswd file
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

# ---- Input validation ------------------------------------------------------
# Caller-supplied labels (namespace, component name, hostname) flow into
# filesystem paths under ${STACKWIZ_NGINX_DIR}. Reject anything outside a
# conservative charset so a compromised/typo'd manifest can't write outside
# the intended dirs (../../etc/passwd.conf, absolute paths, newlines).

_stackwiz_nginx_check_label() {
    # Usage: _stackwiz_nginx_check_label <kind> <value>
    local kind="$1" value="$2"
    case "$value" in
        *[!A-Za-z0-9._-]*|""|"."|"..")
            echo "stackwiz-nginx: invalid ${kind} '${value}' — expected [A-Za-z0-9._-]+" >&2
            return 1
            ;;
    esac
}

_stackwiz_nginx_check_hostname() {
    # Hostnames: dotted labels, each [A-Za-z0-9-]+. No leading dot, no ../.
    local host="$1"
    case "$host" in
        *[!A-Za-z0-9.-]*|""|.*|*..*)
            echo "stackwiz-nginx: invalid hostname '${host}'" >&2
            return 1
            ;;
    esac
}

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

_stackwiz_nginx_discover_gelf() {
    # Try to discover Graylog GELF endpoint from Consul for centralized logging.
    # Falls back to 127.0.0.1 (works if Graylog is on the same host; silently
    # drops UDP if nothing listens).
    local consul_addr="${CONSUL_HTTP_ADDR:-http://127.0.0.1:8500}"
    local consul_token=""
    local state_dir="${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}"
    # Read consul token from any consumer's state dir.
    for d in "${state_dir}"/*/consul-http-token "${state_dir}/consul-http-token"; do
        if [ -f "$d" ]; then consul_token="$(cat "$d")"; break; fi
    done
    local addr=""
    if [ -n "${consul_token}" ]; then
        addr="$(curl -sf -H "X-Consul-Token: ${consul_token}" \
            "${consul_addr}/v1/catalog/service/graylog" 2>/dev/null \
            | python3 -c 'import sys,json
d=json.load(sys.stdin)
if d: print(d[0].get("ServiceAddress") or d[0].get("Address",""))' 2>/dev/null || true)"
    fi
    echo "${addr:-127.0.0.1}"
}

_stackwiz_nginx_write_compose() {
    # The compose file lives alongside the consumer dir at a well-known path.
    # It is framework-owned — consumers MUST NOT edit it.
    local bin_dir="${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}/bin"
    local gelf_host
    gelf_host="$(_stackwiz_nginx_discover_gelf)"
    if [ -f "${bin_dir}/stackwiz-nginx-compose.yml" ]; then
        sed "s|udp://127.0.0.1:12201|udp://${gelf_host}:12201|g" \
            "${bin_dir}/stackwiz-nginx-compose.yml" > "${STACKWIZ_NGINX_COMPOSE}"
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
    # Already running — nothing to do.
    if docker ps --format '{{.Names}}' | grep -qxF "${STACKWIZ_NGINX_CONTAINER}"; then
        return 0
    fi
    # Container EXISTS but is stopped (e.g. another stack's install
    # called `docker stop stackwiz-nginx` for cert provisioning and
    # never restarted). `docker start` is cheaper than compose
    # recreate and preserves the existing container's configuration.
    if docker ps -a --format '{{.Names}}' | grep -qxF "${STACKWIZ_NGINX_CONTAINER}"; then
        if docker start "${STACKWIZ_NGINX_CONTAINER}" >/dev/null 2>&1; then
            return 0
        fi
        # Start failed — container is in a bad state. Fall through to
        # force-recreate via compose below.
        docker rm -f "${STACKWIZ_NGINX_CONTAINER}" 2>/dev/null || true
    fi
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

_stackwiz_nginx_preempt_host_nginx() {
    # Debian's certbot apt package (and some base images) pull in the
    # `nginx` system package, which systemd auto-enables + auto-starts.
    # That host nginx grabs port 80/443 — exactly what the stackwiz-nginx
    # container wants. Symptom: `docker compose up stackwiz-nginx` fails
    # with "address already in use". Stop + disable + mask so it stays
    # off through reboots. Mask also neutralises recommends-pulled
    # reinstalls triggered by later apt upgrades.
    if [ -f /lib/systemd/system/nginx.service ] \
        || [ -f /etc/systemd/system/nginx.service ]; then
        if systemctl is-active nginx >/dev/null 2>&1 \
            || systemctl is-enabled nginx >/dev/null 2>&1; then
            systemctl stop nginx 2>/dev/null || true
            systemctl disable nginx 2>/dev/null || true
            systemctl mask nginx 2>/dev/null || true
            echo "stackwiz-nginx: disabled + masked host nginx service " \
                 "(port 80/443 reserved for stackwiz-nginx container)"
        fi
    fi
}

stackwiz_nginx_init() {
    # Idempotent: create dirs, default conf, compose file, start container.
    install -d -m 0755 "${STACKWIZ_NGINX_DIR}/conf.d" "${STACKWIZ_NGINX_DIR}/tls" "${STACKWIZ_NGINX_DIR}/auth"

    if [ ! -f "${STACKWIZ_NGINX_DIR}/conf.d/00-stackwiz-default.conf" ]; then
        _stackwiz_nginx_write_default_conf
    fi

    # Preempt any systemd-managed host nginx — must happen BEFORE
    # _stackwiz_nginx_ensure_container tries to bind port 80/443.
    _stackwiz_nginx_preempt_host_nginx

    _stackwiz_nginx_lock
    _stackwiz_nginx_ensure_container
    _stackwiz_nginx_unlock

    # Make configs and tls dir traversable by nginx-unprivileged (uid 101).
    # Certs are world-readable; private keys stay 0600 owned by uid 101 (see
    # stackwiz_nginx_add_cert). Recursive go+rX on tls/ is intentionally avoided
    # to prevent re-exposing keys.
    chmod -R go+rX "${STACKWIZ_NGINX_DIR}/conf.d" 2>/dev/null || true
    chmod go+rX "${STACKWIZ_NGINX_DIR}/tls" 2>/dev/null || true
    find "${STACKWIZ_NGINX_DIR}/tls" -maxdepth 1 -type f -name '*.crt' \
        -exec chmod 0644 {} + 2>/dev/null || true
}

stackwiz_nginx_add_conf() {
    # Usage: stackwiz_nginx_add_conf <namespace> <priority> <name> < rendered.conf
    #    OR: stackwiz_nginx_add_conf <namespace> <priority> <name> /path/to/file
    local ns="${1:?namespace required}"
    local pri="${2:?priority required}"
    local name="${3:?name required}"
    local file="${4:-}"
    _stackwiz_nginx_check_label namespace "$ns" || return 1
    _stackwiz_nginx_check_label priority  "$pri" || return 1
    _stackwiz_nginx_check_label name      "$name" || return 1
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
    _stackwiz_nginx_check_hostname "$host" || return 1
    install -m 0644 "${cert}" "${STACKWIZ_NGINX_DIR}/tls/${host}.crt"
    install -m 0600 "${key}"  "${STACKWIZ_NGINX_DIR}/tls/${host}.key"
    # nginx-unprivileged (image) runs as uid 101; the read-only bind mount
    # preserves host ownership, so the key must be owned by 101 for nginx to
    # read it. chown is best-effort: if running in a rootless context it may
    # fail, in which case the operator must adjust manually.
    chown 101:101 "${STACKWIZ_NGINX_DIR}/tls/${host}.key" 2>/dev/null || true
}

stackwiz_nginx_add_proxy() {
    # High-level: generate a standard TLS reverse proxy vhost.
    # Usage: stackwiz_nginx_add_proxy <ns> <pri> <name> <hostname> <upstream>
    # Example: stackwiz_nginx_add_proxy "082" "00" "backup" "backup.example.com" "http://oxidized:8888"
    local ns="${1:?}" pri="${2:?}" name="${3:?}" hostname="${4:?}" upstream="${5:?}"
    local max_body="${6:-50M}"
    cat <<EOF | stackwiz_nginx_add_conf "${ns}" "${pri}" "${name}"
server {
    listen 8443 ssl;
    http2 on;
    server_name ${hostname};

    ssl_certificate     /etc/nginx/tls/${hostname}.crt;
    ssl_certificate_key /etc/nginx/tls/${hostname}.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    client_max_body_size ${max_body};
    resolver 127.0.0.11 valid=10s ipv6=off;
    set \$backend ${upstream};

    location / {
        proxy_pass              \$backend;
        proxy_set_header        Host \$host;
        proxy_set_header        X-Real-IP \$remote_addr;
        proxy_set_header        X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header        X-Forwarded-Proto \$scheme;
        proxy_http_version      1.1;
        proxy_set_header        Upgrade \$http_upgrade;
        proxy_set_header        Connection "upgrade";
    }
}
EOF
}

stackwiz_nginx_add_forwardauth_proxy() {
    # High-level: generate a TLS vhost gated by Authentik ForwardAuth.
    # Usage: stackwiz_nginx_add_forwardauth_proxy <ns> <pri> <name> <hostname> <upstream> [authentik_proxy_url]
    local ns="${1:?}" pri="${2:?}" name="${3:?}" hostname="${4:?}" upstream="${5:?}"
    local auth_proxy="${6:-http://authentik-proxy:9000}"
    cat <<EOF | stackwiz_nginx_add_conf "${ns}" "${pri}" "${name}"
server {
    listen 8443 ssl;
    http2 on;
    server_name ${hostname};

    ssl_certificate     /etc/nginx/tls/${hostname}.crt;
    ssl_certificate_key /etc/nginx/tls/${hostname}.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    resolver 127.0.0.11 valid=10s ipv6=off;
    set \$backend_proxy ${auth_proxy};
    set \$backend_upstream ${upstream};

    location /outpost.goauthentik.io {
        proxy_pass              \$backend_proxy/outpost.goauthentik.io;
        proxy_set_header        Host \$host;
        proxy_set_header        X-Original-URL \$scheme://\$http_host\$request_uri;
        add_header              Set-Cookie \$auth_cookie;
        auth_request_set        \$auth_cookie \$upstream_http_set_cookie;
        proxy_pass_request_body off;
        proxy_set_header        Content-Length "";
        # Allow self-signed outpost URLs (e.g. cross-host:
        # https://auth.other-domain/outpost.goauthentik.io/...). Without
        # proxy_ssl_verify off nginx rejects the stackwiz-CA cert and
        # auth_request returns 502 — every login loops through 401.
        proxy_ssl_verify        off;
        proxy_ssl_server_name   on;
    }

    location / {
        auth_request            /outpost.goauthentik.io/auth/nginx;
        error_page              401 = @goauthentik_proxy_signin;
        auth_request_set        \$auth_cookie \$upstream_http_set_cookie;
        add_header              Set-Cookie \$auth_cookie;
        auth_request_set        \$authentik_username \$upstream_http_x_authentik_username;
        auth_request_set        \$authentik_groups   \$upstream_http_x_authentik_groups;
        auth_request_set        \$authentik_email    \$upstream_http_x_authentik_email;
        proxy_set_header        X-authentik-username \$authentik_username;
        proxy_set_header        X-authentik-groups   \$authentik_groups;
        proxy_set_header        X-authentik-email    \$authentik_email;

        proxy_pass              \$backend_upstream;
        proxy_set_header        Host \$host;
        proxy_set_header        X-Real-IP \$remote_addr;
        proxy_set_header        X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header        X-Forwarded-Proto \$scheme;
        proxy_ssl_verify        off;
    }

    location @goauthentik_proxy_signin {
        internal;
        add_header              Set-Cookie \$auth_cookie;
        return                  302 /outpost.goauthentik.io/start?rd=\$request_uri;
    }
}
EOF
}

stackwiz_nginx_write_htpasswd() {
    # Usage: stackwiz_nginx_write_htpasswd <hostname> <user> <password>
    # Writes ${STACKWIZ_NGINX_DIR}/auth/<hostname>.htpasswd with a single
    # APR1-hashed entry. Idempotent — overwrites the file each call.
    # Uses openssl (alpine-compatible) instead of the httpd-tools htpasswd
    # binary because the nginx container doesn't ship one.
    local host="${1:?host required}" user="${2:?user required}" pass="${3:?password required}"
    _stackwiz_nginx_check_hostname "$host" || return 1
    _stackwiz_nginx_check_label user "$user" || return 1
    install -d -m 0755 "${STACKWIZ_NGINX_DIR}/auth"
    local target="${STACKWIZ_NGINX_DIR}/auth/${host}.htpasswd"
    local hash
    hash="$(openssl passwd -apr1 "$pass")" || return 1
    printf '%s:%s\n' "$user" "$hash" > "${target}.tmp"
    chmod 0644 "${target}.tmp"
    mv -f "${target}.tmp" "${target}"
}

stackwiz_nginx_add_basicauth_proxy() {
    # High-level: generate a TLS vhost gated by HTTP basic auth.
    # Usage: stackwiz_nginx_add_basicauth_proxy <ns> <pri> <name> <hostname> <upstream> <htpasswd_container_path>
    #
    # <htpasswd_container_path> is the path INSIDE the nginx container —
    # typically /etc/nginx/auth/<hostname>.htpasswd (the auth dir is bind-
    # mounted from ${STACKWIZ_NGINX_DIR}/auth). Pair with
    # stackwiz_nginx_write_htpasswd to create the file on the host side.
    local ns="${1:?}" pri="${2:?}" name="${3:?}" hostname="${4:?}" upstream="${5:?}" htpasswd="${6:?}"
    cat <<EOF | stackwiz_nginx_add_conf "${ns}" "${pri}" "${name}"
server {
    listen 8443 ssl;
    http2 on;
    server_name ${hostname};

    ssl_certificate     /etc/nginx/tls/${hostname}.crt;
    ssl_certificate_key /etc/nginx/tls/${hostname}.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    resolver 127.0.0.11 valid=10s ipv6=off;
    set \$backend_upstream ${upstream};

    location / {
        auth_basic              "Restricted";
        auth_basic_user_file    ${htpasswd};

        proxy_pass              \$backend_upstream;
        proxy_set_header        Host \$host;
        proxy_set_header        X-Real-IP \$remote_addr;
        proxy_set_header        X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header        X-Forwarded-Proto \$scheme;
        proxy_set_header        Upgrade \$http_upgrade;
        proxy_set_header        Connection "upgrade";
        proxy_http_version      1.1;
        proxy_ssl_verify        off;
        proxy_read_timeout      300s;
        proxy_send_timeout      300s;
        client_max_body_size    50M;
    }
}
EOF
}

stackwiz_nginx_reload() {
    # If the container died (e.g. stale config on startup), restart it.
    if ! docker ps --format '{{.Names}}' | grep -qxF "${STACKWIZ_NGINX_CONTAINER}"; then
        echo "stackwiz-nginx: container not running — restarting"
        if [ -f "${STACKWIZ_NGINX_COMPOSE}" ]; then
            docker compose -f "${STACKWIZ_NGINX_COMPOSE}" up -d --force-recreate
        fi
        sleep 1
    fi
    # Validate config first — fail loudly so callers' set -e catches it.
    if ! docker exec "${STACKWIZ_NGINX_CONTAINER}" nginx -t 2>&1; then
        echo "stackwiz-nginx: config test FAILED — reload skipped" >&2
        docker logs --tail 20 "${STACKWIZ_NGINX_CONTAINER}" >&2 || true
        return 1
    fi
    docker exec "${STACKWIZ_NGINX_CONTAINER}" nginx -s reload
}

stackwiz_nginx_remove_consumer() {
    # Remove all configs for a namespace. If last consumer, tear down container.
    local ns="${1:?namespace required}"
    _stackwiz_nginx_check_label namespace "$ns" || return 1

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
