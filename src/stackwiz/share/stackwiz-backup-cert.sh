#!/usr/bin/env bash
# stackwiz-backup-cert — save / restore TLS certificate material so a VM
# reinstall doesn't trigger a full cert re-issue cycle.
#
# Scope is intentionally narrow: ONLY cryptographic material that would
# otherwise need to be regenerated from scratch (self-signed CA root +
# leaf certs, Let's Encrypt state). This is NOT a full disaster-recovery
# backup — Vault data, Consul KV, Authentik DB, docker named volumes are
# out of scope and covered by a separate tool (TBD).
#
# What survives a round-trip:
#   * stackwiz self-signed CA root          (/etc/stackwiz/tls/stackwiz-ca.{crt,key})
#   * per-host leaf certs + fullchains      (/etc/stackwiz/tls/<host>.{crt,key,fullchain.crt})
#   * operator "bring your own" certs       (/etc/stackwiz/tls/custom/<host>/)
#   * Let's Encrypt full state dir          (/etc/letsencrypt/)
#
# What is NOT captured (regenerable from the above on next install):
#   * /opt/stackwiz/{vault,nginx}/tls/*     — copied from /etc/stackwiz/tls by install scripts
#   * /var/lib/stackwiz/shared/vault-ca.crt — published by 081's vault.sh
#
# Usage:
#   sudo stackwiz-backup-cert backup [DIR]          # writes DIR/stackwiz-certs-<host>-<utc>.tar.gz
#   sudo stackwiz-backup-cert list TARBALL          # show contents
#   sudo stackwiz-backup-cert restore TARBALL       # refuses to overwrite existing files
#   sudo stackwiz-backup-cert restore --force TBL   # overwrite + re-assert ownership
#
# Post-restore the operator re-runs ./bootstrap.sh. stackwiz-tls.sh's
# >30-day freshness check treats the restored cert as current and skips
# re-issue; install scripts that copy /etc/stackwiz/tls/<host>.crt into
# /opt/stackwiz/<service>/tls/ refresh derived copies automatically.
set -euo pipefail

_die() { echo "stackwiz-backup-cert: $*" >&2; exit 1; }

_need_root() {
    [ "$(id -u)" -eq 0 ] || _die "must run as root (cert material is mode 0600/0700)"
}

# Paths that hold primary cert material. Order matters for restore —
# /etc/letsencrypt/live symlinks resolve against /etc/letsencrypt/archive so
# both must land together (tar preserves symlinks).
CERT_PATHS=(
    /etc/stackwiz/tls
    /etc/letsencrypt
)

_host_tag() {
    hostname -s 2>/dev/null | tr -c '[:alnum:]-' '_' | tr -s '_' || echo host
}

_cmd_backup() {
    _need_root
    local out_dir="${1:-$(pwd)}"
    [ -d "$out_dir" ] || _die "output dir ${out_dir} does not exist"

    local stamp host out_tar work manifest
    stamp="$(date -u +%Y%m%d-%H%M%S)"
    host="$(_host_tag)"
    out_tar="${out_dir%/}/stackwiz-certs-${host}-${stamp}.tar.gz"
    work="$(mktemp -d)"
    trap "rm -rf '${work}'" RETURN

    mkdir -p "${work}/files"
    manifest="${work}/manifest.txt"
    {
        echo "# stackwiz certificate backup"
        echo "source_host: $(hostname -f 2>/dev/null || hostname)"
        echo "created_at_utc: ${stamp}"
        echo "tool: stackwiz-backup-cert"
        echo ""
        echo "# included paths"
    } > "${manifest}"

    local any=0
    local p
    for p in "${CERT_PATHS[@]}"; do
        if [ -e "$p" ]; then
            mkdir -p "${work}/files$(dirname "$p")"
            cp -a "$p" "${work}/files${p}"
            echo "  + ${p}"
            echo "  included: ${p}" >> "${manifest}"
            any=1
        else
            echo "  - ${p} (absent, skipping)"
            echo "  skipped: ${p} (not present)" >> "${manifest}"
        fi
    done
    [ "${any}" -eq 1 ] || _die "no cert paths found — nothing to back up"

    # cp -a inside mktemp can end up owned by root; keep as-is, tar preserves.
    tar -C "${work}" -czf "${out_tar}" manifest.txt files
    chmod 600 "${out_tar}"

    echo ""
    echo "stackwiz-backup-cert: wrote ${out_tar} ($(du -h "${out_tar}" | cut -f1))"
    echo ""
    echo "The tarball contains the CA PRIVATE KEY and Let's Encrypt"
    echo "private keys. Encrypt before moving off-host:"
    echo "  gpg --symmetric --cipher-algo AES256 '${out_tar}'"
}

_cmd_list() {
    local tarball="${1:-}"
    [ -f "$tarball" ] || _die "usage: list <tarball>"
    local work; work="$(mktemp -d)"
    trap "rm -rf '${work}'" RETURN
    tar -C "${work}" -xzf "${tarball}" manifest.txt
    cat "${work}/manifest.txt"
    echo ""
    echo "# archive tree"
    tar -tzf "${tarball}" | grep -v '^files/$' | sed 's|^files/|  |'
}

_cmd_restore() {
    _need_root
    local force=0
    if [ "${1:-}" = "--force" ]; then force=1; shift; fi
    local tarball="${1:-}"
    [ -f "$tarball" ] || _die "usage: restore [--force] <tarball>"

    local work; work="$(mktemp -d)"
    trap "rm -rf '${work}'" RETURN
    tar -C "${work}" -xzf "${tarball}"

    echo "# source:"
    sed -n '1,4p' "${work}/manifest.txt"
    echo ""

    local src dst
    for src in "${work}/files/etc/stackwiz/tls" "${work}/files/etc/letsencrypt"; do
        [ -e "$src" ] || continue
        # Re-derive host path by stripping "${work}/files" prefix.
        dst="${src#${work}/files}"
        if [ -e "${dst}" ] && [ "${force}" -eq 0 ]; then
            echo "  ! ${dst} already exists (use --force to overwrite)"
            continue
        fi
        if [ -e "${dst}" ] && [ "${force}" -eq 1 ]; then
            local bak; bak="${dst}.before-restore-$(date -u +%Y%m%d-%H%M%S)"
            mv "${dst}" "${bak}"
            echo "  ~ moved existing ${dst} -> ${bak}"
        fi
        mkdir -p "$(dirname "${dst}")"
        cp -a "${src}" "${dst}"
        echo "  + restored ${dst}"
    done

    # Ownership re-assertion: /etc/stackwiz/tls should be root-owned but
    # private keys need 0600. cp -a preserves mode; only touch if restore
    # happened.
    if [ -d /etc/stackwiz/tls ]; then
        find /etc/stackwiz/tls -name '*.key' -type f -exec chmod 600 {} \; 2>/dev/null || true
        find /etc/stackwiz/tls -name '*.crt' -type f -exec chmod 644 {} \; 2>/dev/null || true
        [ -f /etc/stackwiz/tls/stackwiz-ca.key ] && chmod 600 /etc/stackwiz/tls/stackwiz-ca.key
    fi

    echo ""
    echo "stackwiz-backup-cert: restore done. Next steps:"
    echo "  1) Re-run ./bootstrap.sh on each stack (079, 081, 077, 061, 082)."
    echo "     stackwiz-tls.sh's 30-day freshness check will reuse the"
    echo "     restored certs and install scripts will re-populate derived"
    echo "     copies (/opt/stackwiz/vault/tls, /opt/stackwiz/nginx/tls,"
    echo "     /var/lib/stackwiz/shared/vault-ca.crt)."
    echo "  2) Clients that already trust the stackwiz CA keep working."
    echo "     New clients: serve /etc/stackwiz/tls/stackwiz-ca.crt as"
    echo "     before (no change — same CA == same trust chain)."
}

_cmd_help() {
    sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-help}" in
    backup)  shift; _cmd_backup "$@" ;;
    list)    shift; _cmd_list "$@" ;;
    restore) shift; _cmd_restore "$@" ;;
    -h|--help|help) _cmd_help ;;
    *)       _die "unknown subcommand '$1'. Try: $0 help" ;;
esac
