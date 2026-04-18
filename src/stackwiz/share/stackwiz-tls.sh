# stackwiz-tls.sh — TLS certificate helper for any stackwiz consumer.
#
# Source this from a consumer install script:
#     . "${STACKWIZ_STATE_DIR}/bin/stackwiz-tls.sh"
#     stackwiz_tls_ensure "auth.example.internal"
#     echo "cert is $CERT_PATH, key is $KEY_PATH"
#
# Ladder (each step is tried, first success wins):
#   1. operator-provided "bring-your-own" cert at /etc/stackwiz/tls/custom/<host>/
#   2. existing cert still valid >30 days (idempotent re-run)
#   3. Let's Encrypt via Cloudflare DNS-01  (needs CF_DNS_API_TOKEN)
#   4. Let's Encrypt via Route53 DNS-01     (needs AWS_DNS_ACCESS_KEY_ID + AWS_DNS_SECRET_ACCESS_KEY)
#   5. Let's Encrypt via HTTP-01 standalone (needs port 80 reachable from the internet)
#   6. self-signed (CA + leaf)
#
# Opt-in flags (env):
#   STACKWIZ_TLS_MODE   auto | self-signed | letsencrypt    (default: auto)
#   STACKWIZ_TLS_FORCE  1 to bypass the 30-day idempotency check
#   CERTBOT_EMAIL       email for Let's Encrypt registration (default: admin@<hostname>)
#
# Bring-your-own-certificate (BYOC):
#   Drop files at /etc/stackwiz/tls/custom/<hostname>/ to override every
#   other source for that hostname:
#     cert.pem     leaf (required)
#     key.pem      private key (required, chmod 0600)
#     ca.pem       CA chain clients should trust (optional)
#                  — if absent, clients fall back to system trust
#                  — if present, vault.sh etc. publish it to
#                    /var/lib/stackwiz/shared/vault-ca.crt so sibling
#                    stacks automatically trust the chain
#   Replacing a self-signed/LE cert later: drop new files into the same
#   path and run `./bootstrap.sh refresh <component>` — the custom cert
#   check runs BEFORE reuse, so the old cached cert is bypassed.
#
# Adapted from C:\HOME\1.SCRIPTS\061.awx_installation\remote\nginx\generate-cert.sh.

stackwiz_tls_self_signed_dir() { echo "/etc/stackwiz/tls"; }
stackwiz_tls_custom_dir()      { echo "/etc/stackwiz/tls/custom"; }

stackwiz_tls_paths() {
    local host="$1"
    local dir; dir="$(stackwiz_tls_self_signed_dir)"
    echo "${dir}/${host}.crt" "${dir}/${host}.key"
}

stackwiz_tls_le_paths() {
    # Certbot stores wildcard certs under the base domain, not "*.domain".
    local host="${1#\*.}"
    echo "/etc/letsencrypt/live/${host}/fullchain.pem" \
         "/etc/letsencrypt/live/${host}/privkey.pem"
}

stackwiz_tls_cert_fresh() {
    # returns 0 if cert file exists and valid for at least $2 seconds
    # (default 30 days). Single source of truth for cert-validity checks —
    # the engine + other helpers shell out to this rather than reimplementing
    # `openssl x509 -checkend`.
    local cert="$1"
    local window="${2:-2592000}"
    [ -f "$cert" ] || return 1
    openssl x509 -checkend "$window" -noout -in "$cert" >/dev/null 2>&1
}

# Operator-provided "bring-your-own" cert. Checked BEFORE the reuse cache
# and the LE ladder — when the operator drops files at the custom path,
# their intent overrides every other TLS source for that hostname.
#
# Expected files (all PEM):
#   /etc/stackwiz/tls/custom/<host>/cert.pem   leaf cert (required)
#   /etc/stackwiz/tls/custom/<host>/key.pem    private key (required)
#   /etc/stackwiz/tls/custom/<host>/ca.pem     CA chain (optional — when
#                                              present, treated as CA_PATH
#                                              and published to sibling
#                                              stacks via the usual channel)
#
# Returns 0 with CERT_PATH/KEY_PATH/CA_PATH set on success, 1 otherwise.
# Refuses an expired cert even if the files are present — better to fall
# through than to serve expired TLS.
stackwiz_tls_try_custom() {
    local host="$1"
    local dir cert key ca
    dir="$(stackwiz_tls_custom_dir)/${host}"
    cert="${dir}/cert.pem"
    key="${dir}/key.pem"
    ca="${dir}/ca.pem"
    [ -f "$cert" ] && [ -f "$key" ] || return 1
    # Any remaining validity is fine — the operator owns the lifecycle
    # and may be replacing the cert right now. Only reject clearly-
    # expired certs (checkend window = 0 means "expired at this instant").
    if ! stackwiz_tls_cert_fresh "$cert" 0; then
        echo "stackwiz-tls: custom cert ${cert} is EXPIRED — falling through" >&2
        return 1
    fi
    CERT_PATH="$cert"
    KEY_PATH="$key"
    CA_PATH=""
    [ -f "$ca" ] && CA_PATH="$ca"
    local ca_note="no ca.pem; clients use system trust"
    [ -n "$CA_PATH" ] && ca_note="ca.pem published as trust anchor"
    echo "stackwiz-tls: using BYOC cert for ${host} from ${dir} (${ca_note})"
    return 0
}

# Returns 0 if any Let's Encrypt path is actually viable right now.
# Used to decide whether to reuse a self-signed cert: if LE is newly
# reachable (credentials just arrived), reusing a stale self-signed would
# lock the host into self-signed forever despite DNS-01 being available.
stackwiz_tls_le_available() {
    [ "${STACKWIZ_TLS_MODE:-auto}" = "self-signed" ] && return 1
    [ -n "${CF_DNS_API_TOKEN:-}" ] && return 0
    [ -n "${AWS_DNS_ACCESS_KEY_ID:-}" ] && [ -n "${AWS_DNS_SECRET_ACCESS_KEY:-}" ] && return 0
    [ "${STACKWIZ_TLS_ALLOW_HTTP01:-0}" = "1" ] && return 0
    return 1
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
        # LE certs chain to a public root that every client trusts via
        # system trust store — no CA needs to be published for sibling
        # consumers.
        CA_PATH=""
        echo "stackwiz-tls: reusing Let's Encrypt cert for ${host} (>30 days remaining)"
        return 0
    fi

    # Only reuse a self-signed cert if LE is not reachable right now. If CF
    # / Route53 creds have arrived since the self-signed cert was generated,
    # fall through so the LE ladder runs and upgrades the host.
    if stackwiz_tls_cert_fresh "$ss_cert" && stackwiz_tls_le_available; then
        echo "stackwiz-tls: self-signed cert exists for ${host} but LE credentials are available; trying LE upgrade"
        return 1
    fi
    if stackwiz_tls_cert_fresh "$ss_cert"; then
        local ss_dir fullchain
        ss_dir="$(stackwiz_tls_self_signed_dir)"
        fullchain="${ss_dir}/${host}.fullchain.crt"
        # Prefer fullchain (server cert + stackwiz CA) if it exists —
        # servers should serve that so strict TLS clients can build the
        # chain. Legacy self-signed installs (pre-CA refactor) only have
        # the lone cert; fall back to serving that.
        if [ -f "$fullchain" ]; then
            CERT_PATH="$fullchain"
        else
            CERT_PATH="$ss_cert"
        fi
        KEY_PATH="$ss_key"
        CA_PATH="${ss_dir}/stackwiz-ca.crt"
        [ -f "$CA_PATH" ] || CA_PATH=""
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
    if [ -z "${CF_DNS_API_TOKEN:-}" ]; then
        echo "stackwiz-tls: skipping Cloudflare DNS-01 (CF_DNS_API_TOKEN not set)"
        return 1
    fi
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
        CERT_PATH="$le_cert"; KEY_PATH="$le_key"; CA_PATH=""
        echo "stackwiz-tls: obtained via Cloudflare DNS-01"
        return 0
    fi
    return 1
}

stackwiz_tls_try_route53() {
    local host="$1" email="$2"
    if [ -z "${AWS_DNS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_DNS_SECRET_ACCESS_KEY:-}" ]; then
        echo "stackwiz-tls: skipping Route53 DNS-01 (AWS_DNS_ACCESS_KEY_ID / AWS_DNS_SECRET_ACCESS_KEY not set)"
        return 1
    fi
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
        CERT_PATH="$le_cert"; KEY_PATH="$le_key"; CA_PATH=""
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
        CERT_PATH="$le_cert"; KEY_PATH="$le_key"; CA_PATH=""
        echo "stackwiz-tls: obtained via HTTP-01 standalone"
        return 0
    fi
    return 1
}

# --- Self-signed fallback ---

stackwiz_tls_self_sign() {
    # Generate a proper PKI instead of a lone self-signed server cert:
    #   1. Persistent CA under /etc/stackwiz/tls/stackwiz-ca.{crt,key}
    #      (CA:TRUE, keyCertSign). Created once, reused across every
    #      stackwiz self-signed cert on this host.
    #   2. Server cert for $host signed BY the CA (CA:FALSE,
    #      extendedKeyUsage=serverAuth, SAN=DNS:$host[,IP:...]).
    #   3. Fullchain = server cert + CA concatenated, so TLS servers
    #      (Vault, nginx) serving fullchain let clients build the chain
    #      even when they only trust the CA.
    #
    # Why: a lone self-signed cert used as both server and trust anchor
    # is rejected by OpenSSL 3.x / Python's ssl module with error 18
    # "self-signed certificate at depth 0" even when passed as verify=PATH.
    # Splitting into CA + leaf cert fixes that without disabling
    # verification. The CA is what sibling consumers on this host trust
    # (published via vault.sh to /var/lib/stackwiz/shared/vault-ca.crt).
    local host="$1"
    local ss_cert ss_key ss_dir ca_cert ca_key fullchain
    read -r ss_cert ss_key < <(stackwiz_tls_paths "$host")
    ss_dir="$(stackwiz_tls_self_signed_dir)"
    mkdir -p "$ss_dir"
    chmod 755 "$ss_dir"

    ca_cert="${ss_dir}/stackwiz-ca.crt"
    ca_key="${ss_dir}/stackwiz-ca.key"
    fullchain="${ss_dir}/${host}.fullchain.crt"

    if [ ! -f "$ca_cert" ] || [ ! -f "$ca_key" ]; then
        openssl req -x509 -nodes -days 3650 -newkey rsa:4096 \
            -keyout "$ca_key" -out "$ca_cert" \
            -subj "/CN=stackwiz self-signed CA" \
            -addext "basicConstraints=critical,CA:TRUE" \
            -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1
        chmod 600 "$ca_key"
        chmod 644 "$ca_cert"
    fi

    # SANs: hostname + loopback + optional extra IP. 127.0.0.1 is included
    # by default because install scripts commonly address backends via
    # loopback (e.g. the engine adopts Vault at https://127.0.0.1:8200
    # when DNS for the configured hostname isn't resolvable from the
    # installer container), and curl --cacert still enforces hostname
    # verification — a cert with SAN=DNS:vault.sopslab.in alone rejects
    # a connection to https://127.0.0.1/... even though the chain is
    # trusted. Adding the loopback SAN fixes every such intra-host
    # verify failure without disabling TLS checks.
    local san_parts="DNS:${host},IP:127.0.0.1"
    if [ -n "${STACKWIZ_TLS_EXTRA_IP:-}" ] \
        && [ "${STACKWIZ_TLS_EXTRA_IP}" != "127.0.0.1" ]; then
        san_parts="${san_parts},IP:${STACKWIZ_TLS_EXTRA_IP}"
    fi

    local csr="${ss_dir}/${host}.csr"
    local ext; ext="$(mktemp)"
    cat > "$ext" <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=${san_parts}
EXT
    openssl req -new -nodes -newkey rsa:2048 \
        -keyout "$ss_key" -out "$csr" \
        -subj "/CN=${host}" >/dev/null 2>&1
    openssl x509 -req -in "$csr" -CA "$ca_cert" -CAkey "$ca_key" \
        -CAcreateserial -days 365 -out "$ss_cert" \
        -extfile "$ext" >/dev/null 2>&1
    rm -f "$csr" "$ext"
    cat "$ss_cert" "$ca_cert" > "$fullchain"

    chmod 600 "$ss_key"
    chmod 644 "$ss_cert" "$fullchain"
    CERT_PATH="$fullchain"; KEY_PATH="$ss_key"
    # Expose the CA separately so callers can publish it for sibling
    # consumers that need to build trust (verify=PATH against this file).
    CA_PATH="$ca_cert"
    echo "stackwiz-tls: generated self-signed cert for ${host} (signed by stackwiz CA)"
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
install -m 0644 "$RENEWED_LINEAGE/fullchain.pem" "$NGINX_TLS/${HOST}.crt" 2>/dev/null || true
install -m 0600 "$RENEWED_LINEAGE/privkey.pem"   "$NGINX_TLS/${HOST}.key" 2>/dev/null || true
chown 101:101 "$NGINX_TLS/${HOST}.key" 2>/dev/null || true
docker exec stackwiz-nginx nginx -s reload 2>/dev/null || true
HOOK
    chmod 755 "$hook_dir/stackwiz-nginx-reload.sh"
}

# --- Public API ---

# stackwiz_tls_ensure <hostname>
#   After successful return:
#     $CERT_PATH   full certificate (fullchain when self-signed,
#                  LE's fullchain.pem otherwise) — serve this from TLS
#     $KEY_PATH    private key — serve this from TLS
#     $CA_PATH     path to a CA cert clients can pass as verify=PATH to
#                  trust $CERT_PATH. Empty when LE was used (clients
#                  fall back to system trust store). Non-empty when
#                  self-signed — callers should publish it so sibling
#                  consumers on the same host trust this cert.
stackwiz_tls_ensure() {
    local host="${1:?hostname required}"
    local mode="${STACKWIZ_TLS_MODE:-auto}"
    local email="${CERTBOT_EMAIL:-admin@${host}}"
    CERT_PATH=""; KEY_PATH=""; CA_PATH=""

    # 0. Operator-provided custom cert wins over every other path.
    #    Checked FIRST so dropping a new cert and running `refresh` takes
    #    effect immediately, without the reuse cache shadowing it.
    if stackwiz_tls_try_custom "$host"; then return 0; fi

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

# Subcommand dispatcher: `stackwiz-tls.sh check <cert-path> [window-seconds]`
# exits 0 if the cert is fresh (default: 30 days). Intended for the engine
# to shell out to instead of reimplementing the same openssl invocation in
# Python — keeps the validity-window policy in exactly one place.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    case "${1:-}" in
        check)
            stackwiz_tls_cert_fresh "${2:-}" "${3:-2592000}"
            exit $?
            ;;
        "")
            echo "usage: stackwiz-tls.sh check <cert> [window-seconds]" >&2
            exit 2
            ;;
        *)
            echo "stackwiz-tls.sh: unknown subcommand '$1'" >&2
            exit 2
            ;;
    esac
fi
