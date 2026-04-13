# stackwiz-tls.sh — TLS certificate helper for any stackwiz consumer.
#
# Source this from a consumer install script:
#     . "${STACKWIZ_STATE_DIR}/bin/stackwiz-tls.sh"
#     stackwiz_tls_ensure "auth.example.internal"
#     echo "cert is $CERT_PATH, key is $KEY_PATH"
#
# Ladder (each step is tried, first success wins):
#   1. existing cert still valid >30 days (idempotent re-run)
#   2. Let's Encrypt via Cloudflare DNS-01  (needs CF_DNS_API_TOKEN)
#   3. Let's Encrypt via Route53 DNS-01     (needs AWS_DNS_ACCESS_KEY_ID + AWS_DNS_SECRET_ACCESS_KEY)
#   4. Let's Encrypt via HTTP-01 standalone (needs port 80 reachable from the internet)
#   5. self-signed
#
# Opt-in flags (env):
#   STACKWIZ_TLS_MODE   auto | self-signed | letsencrypt    (default: auto)
#   STACKWIZ_TLS_FORCE  1 to bypass the 30-day idempotency check
#   CERTBOT_EMAIL       email for Let's Encrypt registration (default: admin@<hostname>)
#
# Adapted from C:\HOME\1.SCRIPTS\061.awx_installation\remote\nginx\generate-cert.sh.

stackwiz_tls_self_signed_dir() { echo "/etc/stackwiz/tls"; }

stackwiz_tls_paths() {
    local host="$1"
    local dir; dir="$(stackwiz_tls_self_signed_dir)"
    echo "${dir}/${host}.crt" "${dir}/${host}.key"
}

stackwiz_tls_le_paths() {
    local host="$1"
    echo "/etc/letsencrypt/live/${host}/fullchain.pem" \
         "/etc/letsencrypt/live/${host}/privkey.pem"
}

stackwiz_tls_cert_fresh() {
    # returns 0 if cert file exists and valid for >30 days
    local cert="$1"
    [ -f "$cert" ] || return 1
    openssl x509 -checkend 2592000 -noout -in "$cert" >/dev/null 2>&1
}

# Idempotency check — emits CERT_PATH/KEY_PATH in caller's env and returns 0
# if an existing cert is still fresh.
stackwiz_tls_reuse_existing() {
    local host="$1"
    local ss_cert ss_key
    read -r ss_cert ss_key < <(stackwiz_tls_paths "$host")
    local le_cert le_key
    read -r le_cert le_key < <(stackwiz_tls_le_paths "$host")

    if [ "${STACKWIZ_TLS_FORCE:-0}" = "1" ]; then
        return 1
    fi

    if stackwiz_tls_cert_fresh "$le_cert"; then
        CERT_PATH="$le_cert"; KEY_PATH="$le_key"
        echo "stackwiz-tls: reusing Let's Encrypt cert for ${host} (>30 days remaining)"
        return 0
    fi
    if stackwiz_tls_cert_fresh "$ss_cert"; then
        CERT_PATH="$ss_cert"; KEY_PATH="$ss_key"
        echo "stackwiz-tls: reusing self-signed cert for ${host} (>30 days remaining)"
        return 0
    fi
    return 1
}

# Try to install certbot if not already present.
stackwiz_tls_ensure_certbot() {
    if command -v certbot >/dev/null 2>&1; then return 0; fi
    echo "stackwiz-tls: installing certbot..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update >/dev/null
    apt-get install -y --no-install-recommends certbot python3-certbot-dns-cloudflare \
        python3-certbot-dns-route53 >/dev/null
}

# --- Let's Encrypt paths ---

stackwiz_tls_try_cloudflare() {
    local host="$1" email="$2"
    [ -n "${CF_DNS_API_TOKEN:-}" ] || return 1
    stackwiz_tls_ensure_certbot || return 1
    local le_cert le_key creds
    read -r le_cert le_key < <(stackwiz_tls_le_paths "$host")
    # Persist credentials so certbot's auto-renewal timer can use them.
    # certbot writes the credential path into /etc/letsencrypt/renewal/*.conf
    # and needs it on every renewal, not just the initial issuance.
    creds="/etc/letsencrypt/.stackwiz-cloudflare.ini"
    install -d -m 0700 /etc/letsencrypt
    printf 'dns_cloudflare_api_token = %s\n' "${CF_DNS_API_TOKEN}" > "$creds"
    chmod 600 "$creds"
    echo "stackwiz-tls: requesting cert for ${host} via Cloudflare DNS-01..."
    if certbot certonly --dns-cloudflare --dns-cloudflare-credentials "$creds" \
        --dns-cloudflare-propagation-seconds 30 \
        -d "$host" --non-interactive --agree-tos --email "$email" >/dev/null 2>&1 \
        && [ -f "$le_cert" ] && [ -f "$le_key" ]; then
        CERT_PATH="$le_cert"; KEY_PATH="$le_key"
        echo "stackwiz-tls: obtained via Cloudflare DNS-01"
        return 0
    fi
    return 1
}

stackwiz_tls_try_route53() {
    local host="$1" email="$2"
    [ -n "${AWS_DNS_ACCESS_KEY_ID:-}" ] || return 1
    [ -n "${AWS_DNS_SECRET_ACCESS_KEY:-}" ] || return 1
    stackwiz_tls_ensure_certbot || return 1
    local le_cert le_key
    read -r le_cert le_key < <(stackwiz_tls_le_paths "$host")
    # Persist AWS credentials so certbot's auto-renewal timer can use them.
    # The Route53 plugin reads from env vars, but `certbot renew` runs from
    # systemd with a clean env. Write an env file that the renewal hook sources.
    local aws_env="/etc/letsencrypt/.stackwiz-route53.env"
    install -d -m 0700 /etc/letsencrypt
    printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\n' \
        "${AWS_DNS_ACCESS_KEY_ID}" "${AWS_DNS_SECRET_ACCESS_KEY}" > "${aws_env}"
    chmod 600 "${aws_env}"
    echo "stackwiz-tls: requesting cert for ${host} via Route53 DNS-01..."
    if env AWS_ACCESS_KEY_ID="$AWS_DNS_ACCESS_KEY_ID" \
           AWS_SECRET_ACCESS_KEY="$AWS_DNS_SECRET_ACCESS_KEY" \
           certbot certonly --dns-route53 -d "$host" \
           --non-interactive --agree-tos --email "$email" >/dev/null 2>&1 \
       && [ -f "$le_cert" ] && [ -f "$le_key" ]; then
        # Patch the renewal config to source the env file before renewing.
        local renewal_conf="/etc/letsencrypt/renewal/${host}.conf"
        if [ -f "${renewal_conf}" ] && ! grep -q 'pre_hook' "${renewal_conf}"; then
            printf '\n[renewalparams]\npre_hook = . %s\n' "${aws_env}" >> "${renewal_conf}"
        fi
        CERT_PATH="$le_cert"; KEY_PATH="$le_key"
        echo "stackwiz-tls: obtained via Route53 DNS-01"
        return 0
    fi
    return 1
}

stackwiz_tls_try_standalone() {
    local host="$1" email="$2"
    [ "${STACKWIZ_TLS_ALLOW_HTTP01:-0}" = "1" ] || return 1
    stackwiz_tls_ensure_certbot || return 1
    local le_cert le_key
    read -r le_cert le_key < <(stackwiz_tls_le_paths "$host")
    echo "stackwiz-tls: requesting cert for ${host} via HTTP-01 standalone..."
    systemctl stop nginx 2>/dev/null || true
    local ok=0
    if certbot certonly --standalone -d "$host" \
        --non-interactive --agree-tos --email "$email" >/dev/null 2>&1 \
        && [ -f "$le_cert" ] && [ -f "$le_key" ]; then
        ok=1
    fi
    systemctl start nginx 2>/dev/null || true
    if [ "$ok" = "1" ]; then
        CERT_PATH="$le_cert"; KEY_PATH="$le_key"
        echo "stackwiz-tls: obtained via HTTP-01 standalone"
        return 0
    fi
    return 1
}

# --- Self-signed fallback ---

stackwiz_tls_self_sign() {
    local host="$1"
    local ss_cert ss_key ss_dir
    read -r ss_cert ss_key < <(stackwiz_tls_paths "$host")
    ss_dir="$(stackwiz_tls_self_signed_dir)"
    mkdir -p "$ss_dir"
    chmod 755 "$ss_dir"

    local san_parts="DNS:${host}"
    if [ -n "${STACKWIZ_TLS_EXTRA_IP:-}" ]; then
        san_parts="${san_parts},IP:${STACKWIZ_TLS_EXTRA_IP}"
    fi

    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout "$ss_key" \
        -out "$ss_cert" \
        -subj "/CN=${host}" \
        -addext "subjectAltName=${san_parts}" >/dev/null 2>&1
    chmod 600 "$ss_key"
    chmod 644 "$ss_cert"
    CERT_PATH="$ss_cert"; KEY_PATH="$ss_key"
    echo "stackwiz-tls: generated self-signed cert for ${host}"
}

# Install a certbot deploy hook that copies renewed certs into the shared
# nginx tls dir and reloads the container. Runs after every successful
# renewal — certbot calls it with $RENEWED_LINEAGE set to the cert dir.
_stackwiz_tls_install_deploy_hook() {
    local hook_dir="/etc/letsencrypt/renewal-hooks/deploy"
    mkdir -p "$hook_dir"
    cat > "$hook_dir/stackwiz-nginx-reload.sh" <<'HOOK'
#!/bin/bash
# Auto-generated by stackwiz-tls.sh — copies renewed cert into the shared
# nginx tls dir and reloads the container.
NGINX_TLS="/opt/stackwiz/nginx/tls"
[ -d "$NGINX_TLS" ] || exit 0
HOST=$(basename "$RENEWED_LINEAGE")
cp "$RENEWED_LINEAGE/fullchain.pem" "$NGINX_TLS/${HOST}.crt" 2>/dev/null || true
cp "$RENEWED_LINEAGE/privkey.pem"   "$NGINX_TLS/${HOST}.key" 2>/dev/null || true
docker exec stackwiz-nginx nginx -s reload 2>/dev/null || true
HOOK
    chmod 755 "$hook_dir/stackwiz-nginx-reload.sh"
}

# --- Public API ---

# stackwiz_tls_ensure <hostname>
#   After successful return, $CERT_PATH and $KEY_PATH are set to paths on disk
#   readable by root.
stackwiz_tls_ensure() {
    local host="${1:?hostname required}"
    local mode="${STACKWIZ_TLS_MODE:-auto}"
    local email="${CERTBOT_EMAIL:-admin@${host}}"
    CERT_PATH=""; KEY_PATH=""

    # 1. Reuse existing fresh cert.
    if stackwiz_tls_reuse_existing "$host"; then return 0; fi

    # 2. Let's Encrypt ladder (auto mode only).
    if [ "$mode" = "auto" ] || [ "$mode" = "letsencrypt" ]; then
        if stackwiz_tls_try_cloudflare "$host" "$email"; then _stackwiz_tls_install_deploy_hook; return 0; fi
        if stackwiz_tls_try_route53    "$host" "$email"; then _stackwiz_tls_install_deploy_hook; return 0; fi
        if stackwiz_tls_try_standalone "$host" "$email"; then _stackwiz_tls_install_deploy_hook; return 0; fi
        if [ "$mode" = "letsencrypt" ]; then
            echo "stackwiz-tls: all Let's Encrypt methods failed" >&2
            return 1
        fi
    fi

    # 3. Self-signed.
    stackwiz_tls_self_sign "$host"
}

# stackwiz_nginx_render <template_path> <hostname> <cert> <key> <upstream>
#   Substitutes __SERVER_NAME__, __SSL_CERT__, __SSL_KEY__, __UPSTREAM__ in the
#   template and echoes the result to stdout.
stackwiz_nginx_render() {
    local template="$1" host="$2" cert="$3" key="$4" upstream="$5"
    sed -e "s|__SERVER_NAME__|${host}|g" \
        -e "s|__SSL_CERT__|${cert}|g" \
        -e "s|__SSL_KEY__|${key}|g" \
        -e "s|__UPSTREAM__|${upstream}|g" \
        "$template"
}
