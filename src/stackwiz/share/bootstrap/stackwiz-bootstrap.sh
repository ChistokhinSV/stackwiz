#!/usr/bin/env bash
# stackwiz-bootstrap.sh — shared bootstrap library for stackwiz consumers.
#
# Consumer stub contract:
#
#   #!/usr/bin/env bash
#   set -euo pipefail
#   SW_REQUIRED_PKGS=(curl ca-certificates jq openssl gettext-base python3)
#   SW_EXTRA_ENV=(CONSUL_HTTP_TOKEN STACKWIZ_HOST_MANIFEST_DIR)
#   SW_CHOWN_FILES=(.stackwiz.env .stackwiz.secrets.env .env)
#   SW_WRITABLE_DEFAULT=0                       # 1 = manifest mount RW by default
#   SW_WRITE_CMDS=(init-env)                    # args that force manifest RW
#   SW_READONLY_CMDS=(validate list info)       # args that force manifest RO
#   SW_HEADLESS_ARGS=(--auto --validate validate list info init-env)
#   . "$(dirname "$0")/stackwiz-bootstrap.sh"
#   sw_bootstrap_main "$@"
#
# All SW_* vars are optional and have sensible defaults (see below).

# ---------- defaults ----------
: "${STACKWIZ_IMAGE:=ghcr.io/chistokhinsv/stackwiz:latest}"
: "${STACKWIZ_STATE_DIR:=/var/lib/stackwiz}"
: "${SW_WRITABLE_DEFAULT:=0}"

if ! declare -p SW_REQUIRED_PKGS >/dev/null 2>&1; then
  SW_REQUIRED_PKGS=(curl ca-certificates jq openssl gettext-base python3)
fi
if ! declare -p SW_EXTRA_ENV >/dev/null 2>&1; then
  SW_EXTRA_ENV=()
fi
if ! declare -p SW_CHOWN_FILES >/dev/null 2>&1; then
  SW_CHOWN_FILES=(.stackwiz.env .stackwiz.secrets.env .env)
fi
if ! declare -p SW_WRITE_CMDS >/dev/null 2>&1; then
  SW_WRITE_CMDS=(init-env)
fi
if ! declare -p SW_READONLY_CMDS >/dev/null 2>&1; then
  SW_READONLY_CMDS=(validate list info)
fi
if ! declare -p SW_HEADLESS_ARGS >/dev/null 2>&1; then
  SW_HEADLESS_ARGS=(--auto --validate validate list info init-env)
fi

# Canonical env vars always passed through to the installer container.
# Consumer-specific extras go in SW_EXTRA_ENV.
_SW_BASE_ENV=(
  CONSUL_HTTP_ADDR
  VAULT_ADDR
  VAULT_TOKEN
  CF_DNS_API_TOKEN
  AWS_DNS_ACCESS_KEY_ID
  AWS_DNS_SECRET_ACCESS_KEY
  CERTBOT_EMAIL
  STACKWIZ_TLS_FORCE
)

sw_log() { echo "[bootstrap] $*"; }
sw_warn() { echo "warn: $*" >&2; }
sw_err() { echo "error: $*" >&2; }

# Map required package binary name -> apt package name for edge cases.
_sw_pkg_for() {
  case "$1" in
    envsubst) echo "gettext-base" ;;
    *) echo "$1" ;;
  esac
}

sw_bootstrap_source_env() {
  local f="${1:-$PWD/.env}"
  if [ -f "$f" ]; then
    if [ -r "$f" ]; then
      set -a; . "$f"; set +a
    else
      sw_warn "$f exists but is not readable (run: sudo chown \$(id -u):\$(id -g) $f)"
    fi
  fi
}

sw_bootstrap_require_sudo() {
  if ! command -v sudo >/dev/null; then
    sw_err "sudo is required but not installed"
    echo "       run as root: apt-get install -y sudo" >&2
    exit 1
  fi
}

# Install declared prerequisites. Uses SW_REQUIRED_PKGS unless args given.
sw_bootstrap_ensure_pkgs() {
  local pkgs=("$@")
  [ "${#pkgs[@]}" -eq 0 ] && pkgs=("${SW_REQUIRED_PKGS[@]}")
  local needed=()
  local p bin apt_name
  for p in "${pkgs[@]}"; do
    # Treat multi-token entries (e.g. "curl ca-certificates") as apt-name-only
    # when the first token isn't on PATH. Probe the first token.
    bin="${p%% *}"
    # Virtual names that aren't invoked as commands (like ca-certificates).
    case "$bin" in
      ca-certificates|gettext-base)
        dpkg -s "$bin" >/dev/null 2>&1 || needed+=("$bin")
        continue
        ;;
    esac
    if ! command -v "$bin" >/dev/null; then
      apt_name="$(_sw_pkg_for "$bin")"
      needed+=("$apt_name")
      # Always pair curl with ca-certificates for TLS.
      [ "$bin" = "curl" ] && needed+=("ca-certificates")
    fi
  done
  if [ "${#needed[@]}" -gt 0 ]; then
    sw_log "installing host dependencies: ${needed[*]}"
    sudo apt-get update -qq && sudo apt-get install -y -qq "${needed[@]}"
  fi
}

sw_bootstrap_discover_consul() {
  [ -n "${CONSUL_HTTP_ADDR:-}" ] && return 0
  local addr
  for addr in "http://127.0.0.1:8500" "http://consul.$(hostname -d 2>/dev/null || echo local):8500"; do
    if curl -sf "${addr}/v1/status/leader" >/dev/null 2>&1; then
      CONSUL_HTTP_ADDR="$addr"
      export CONSUL_HTTP_ADDR
      sw_log "discovered Consul at ${CONSUL_HTTP_ADDR}"
      return 0
    fi
  done
}

sw_bootstrap_discover_vault() {
  [ -n "${VAULT_ADDR:-}" ] && return 0
  [ -z "${CONSUL_HTTP_ADDR:-}" ] && return 0
  local svc
  svc=$(curl -sf "${CONSUL_HTTP_ADDR}/v1/catalog/service/vault" 2>/dev/null \
    | python3 -c "
import sys,json
r=json.load(sys.stdin)
if r:
    s=r[0]; addr=s.get('ServiceAddress') or s['Address']; port=s['ServicePort']
    print(f'https://{addr}:{port}')
" 2>/dev/null || true)
  if [ -n "$svc" ]; then
    VAULT_ADDR="$svc"
    export VAULT_ADDR
    sw_log "discovered Vault at ${VAULT_ADDR}"
  fi
}

sw_bootstrap_ensure_docker() {
  if ! command -v docker >/dev/null; then
    sw_log "installing Docker..."
    curl -fsSL https://get.docker.com | sudo sh
  fi
  sudo mkdir -p "${STACKWIZ_STATE_DIR}"
}

# Pull image; fall back to cached copy if the registry is unreachable.
# Surfaces whether the pull actually downloaded a newer image or the local
# copy was already current, using docker's own Status: line. On failure,
# surface the underlying docker error (auth / DNS / rate-limit / not-found)
# instead of a generic "registry unreachable".
sw_bootstrap_pull_image() {
  local image="${1:-$STACKWIZ_IMAGE}"
  local pull_out
  if pull_out=$(sudo docker pull "$image" 2>&1); then
    local status
    status=$(echo "$pull_out" | grep -E '^Status:' | tail -n1)
    local digest created
    digest=$(sudo docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "$image" 2>/dev/null || true)
    created=$(sudo docker image inspect --format '{{.Created}}' "$image" 2>/dev/null || true)
    if echo "$status" | grep -q 'Downloaded newer image'; then
      sw_log "image refreshed (${digest:-$image}, built ${created})"
    else
      sw_log "image up to date (${digest:-$image}, built ${created})"
    fi
    return 0
  fi
  if sudo docker image inspect "$image" >/dev/null 2>&1; then
    local digest created
    digest=$(sudo docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "$image" 2>/dev/null || true)
    created=$(sudo docker image inspect --format '{{.Created}}' "$image" 2>/dev/null || true)
    sw_warn "docker pull failed: ${pull_out}"
    if [ -n "$digest" ]; then
      sw_warn "using cached image: ${digest} (built ${created})"
    else
      sw_warn "using cached image: ${image} (built ${created}; no RepoDigests — likely a local build)"
    fi
  else
    sw_err "docker pull failed: ${pull_out}"
    sw_err "image $image not found locally either"
    exit 1
  fi
}

# Parse positional args to set SW_HEADLESS and SW_WRITABLE_MANIFEST.
# Writable resolution:
#   - start from SW_WRITABLE_DEFAULT
#   - SW_WRITE_CMDS match -> force 1
#   - SW_READONLY_CMDS match -> force 0 (takes precedence over write match)
sw_bootstrap_parse_args() {
  SW_HEADLESS=0
  SW_WRITABLE_MANIFEST="${SW_WRITABLE_DEFAULT}"
  local arg cmd
  for arg in "$@"; do
    for cmd in "${SW_HEADLESS_ARGS[@]}"; do
      [ "$arg" = "$cmd" ] && SW_HEADLESS=1
    done
    for cmd in "${SW_WRITE_CMDS[@]}"; do
      [ "$arg" = "$cmd" ] && SW_WRITABLE_MANIFEST=1
    done
    for cmd in "${SW_READONLY_CMDS[@]}"; do
      [ "$arg" = "$cmd" ] && SW_WRITABLE_MANIFEST=0
    done
  done
  export SW_HEADLESS SW_WRITABLE_MANIFEST
}

# Run the installer container. Any args after the function name are passed
# through to wizinstall as the entrypoint command line.
sw_bootstrap_run() {
  local docker_flags=(--rm)
  [ "${SW_HEADLESS}" -eq 0 ] && docker_flags+=(-it)

  local manifest_mount
  if [ "${SW_WRITABLE_MANIFEST}" -eq 1 ]; then
    manifest_mount="$PWD:/manifest"
  else
    manifest_mount="$PWD:/manifest:ro"
  fi

  local env_args=()
  local v
  for v in "${_SW_BASE_ENV[@]}" "${SW_EXTRA_ENV[@]}"; do
    env_args+=(-e "${v}=${!v:-}")
  done
  # Always propagate host state/manifest paths so install scripts can reason
  # about the real filesystem locations.
  env_args+=(-e "STACKWIZ_HOST_STATE_DIR=${STACKWIZ_STATE_DIR}")
  env_args+=(-e "STACKWIZ_HOST_MANIFEST_DIR=$PWD")

  sudo docker run "${docker_flags[@]}" \
    --privileged --pid=host --network=host \
    -v "${manifest_mount}" \
    -v "${STACKWIZ_STATE_DIR}:/state" \
    "${env_args[@]}" \
    "${STACKWIZ_IMAGE}" "$@"
}

# Reclaim ownership of any files the container wrote as root.
sw_bootstrap_chown_outputs() {
  [ "${SW_WRITABLE_MANIFEST}" -eq 1 ] || return 0
  local f
  for f in "${SW_CHOWN_FILES[@]}"; do
    if [ -f "$PWD/$f" ]; then
      sudo chown "$(id -u):$(id -g)" "$PWD/$f" 2>/dev/null || true
    fi
  done
}

# Full bootstrap lifecycle. Consumer stubs call `sw_bootstrap_main "$@"`.
sw_bootstrap_main() {
  sw_bootstrap_source_env
  sw_bootstrap_require_sudo
  sw_bootstrap_ensure_pkgs
  sw_bootstrap_discover_consul
  sw_bootstrap_discover_vault
  sw_bootstrap_ensure_docker
  sw_bootstrap_pull_image
  sw_bootstrap_parse_args "$@"

  local rc=0
  sw_bootstrap_run "$@" || rc=$?
  sw_bootstrap_chown_outputs
  exit "$rc"
}
