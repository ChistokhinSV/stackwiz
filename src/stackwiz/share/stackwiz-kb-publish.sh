# stackwiz-kb-publish.sh — publish a local KB directory as a bare git
# repo accessible via SSH for bidirectional sync with the central KB.
#
# Source from consumer install scripts:
#     . "${STACKWIZ_STATE_DIR}/bin/stackwiz-kb-publish.sh"
#     stackwiz_kb_publish /opt/awx-mcp/kb awx-kb
#
# Creates:
#   /opt/stackwiz/kb-publish/<name>.git  — bare repo (push target)
#   kb-sync system user with SSH key from Vault (authorized for git)
#
# The central kb-source-sync sidecar clones from this bare repo and
# pushes back agent-authored changes. Consul metadata (kb_git_url)
# tells the syncer where to find it.

STATE_DIR="${STACKWIZ_STATE_DIR:-/var/lib/stackwiz}"

# TLS options for curl against Vault. See stackwiz.vault_client.resolve_verify
# for precedence: VAULT_CACERT wins; STACKWIZ_VAULT_VERIFY=false opts out.
_kb_publish_curl_tls() {
    if [ -n "${VAULT_CACERT:-}" ]; then
        printf -- '--cacert %s' "${VAULT_CACERT}"
    elif [ "${STACKWIZ_VAULT_VERIFY:-true}" = "false" ] \
      || [ "${STACKWIZ_VAULT_VERIFY:-true}" = "0" ] \
      || [ "${STACKWIZ_VAULT_VERIFY:-true}" = "no" ]; then
        printf -- '-k'
    fi
}

_kb_publish_vault_token() {
    if [ -n "${VAULT_TOKEN:-}" ]; then echo "$VAULT_TOKEN"; return 0; fi
    local state="${STATE_DIR}"
    for f in "${state}/vault-token" "${state}"/*/vault-token; do
        if [ -f "$f" ]; then cat "$f"; return 0; fi
    done
    echo ""
}

stackwiz_kb_publish() {
    local kb_dir="$1" repo_name="$2"
    # repo_name flows into a filesystem path; restrict to a safe filename
    # charset to stop a malicious / typo'd manifest from writing outside
    # /opt/stackwiz/kb-publish/.
    case "$repo_name" in
        *[!A-Za-z0-9._-]*|""|"."|"..")
            echo "stackwiz-kb-publish: invalid repo_name '$repo_name' — expected [A-Za-z0-9._-]+" >&2
            return 1
            ;;
    esac
    local bare="/opt/stackwiz/kb-publish/${repo_name}.git"

    if [ ! -d "$kb_dir" ]; then
        echo "stackwiz-kb-publish: KB dir not found: $kb_dir" >&2
        return 1
    fi

    # Mark dirs safe BEFORE any git operation (ownership may differ
    # between the stackwiz container uid and the host file owner).
    git config --global --add safe.directory "$kb_dir" 2>/dev/null || true
    git config --global --add safe.directory "$bare" 2>/dev/null || true

    # --- Init bare repo ---
    if [ ! -d "$bare/objects" ]; then
        echo "stackwiz-kb-publish: initializing bare repo at $bare"
        install -d -m 0755 /opt/stackwiz/kb-publish
        git init --bare -q -b main "$bare"
    fi
    # World-readable so the kb-sync user can push/pull.
    chmod -R a+rwX "$bare"

    # --- Init working tree if needed ---
    cd "$kb_dir"
    if [ ! -d .git ]; then
        git init -q -b main .
    fi
    git config user.name "kb-bot"
    git config user.email "kb-bot@local"

    # Set or update the publish remote.
    if git remote | grep -qx publish; then
        git remote set-url publish "$bare"
    else
        git remote add publish "$bare"
    fi

    # Stage, commit, push. Idempotent — no-op if nothing changed.
    git add -A
    if ! git diff --staged --quiet 2>/dev/null; then
        git commit -q -m "kb: publish from $(hostname -s)"
    fi
    git push -q publish main --force 2>/dev/null || true
    echo "stackwiz-kb-publish: published $kb_dir -> $bare"

    # --- Authorize central's SSH key ---
    stackwiz_kb_authorize_sync_key
}

stackwiz_kb_authorize_sync_key() {
    local token
    token="$(_kb_publish_vault_token)"

    if [ -z "${VAULT_ADDR:-}" ] || [ -z "$token" ]; then
        echo "stackwiz-kb-publish: no Vault access — skipping SSH key authorization" >&2
        return 0
    fi

    # Read the central sync public key from Vault shared path.
    local pubkey
    # shellcheck disable=SC2046  # intentional word-split of TLS opts
    pubkey=$(curl -sf $(_kb_publish_curl_tls) -H "X-Vault-Token: ${token}" \
        "${VAULT_ADDR}/v1/stackwiz/data/shared/kb_sync_ssh_pubkey" 2>/dev/null \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["data"]["value"])' 2>/dev/null || true)

    if [ -z "$pubkey" ]; then
        echo "stackwiz-kb-publish: no SSH pubkey in Vault (shared/kb_sync_ssh_pubkey) — sync will need manual key setup" >&2
        return 0
    fi

    # Create kb-sync user if it doesn't exist.
    if ! id kb-sync >/dev/null 2>&1; then
        useradd -r -m -s /usr/bin/git-shell kb-sync 2>/dev/null || \
        adduser -D -s /usr/bin/git-shell kb-sync 2>/dev/null || true
        echo "stackwiz-kb-publish: created kb-sync user (git-shell)"
    fi

    # Authorize the public key.
    local ssh_dir="/home/kb-sync/.ssh"
    install -d -m 0700 -o kb-sync "$ssh_dir"
    local auth_file="$ssh_dir/authorized_keys"
    if ! grep -qF "$pubkey" "$auth_file" 2>/dev/null; then
        echo "$pubkey" >> "$auth_file"
        chown kb-sync "$auth_file"
        chmod 600 "$auth_file"
        echo "stackwiz-kb-publish: authorized central sync key for kb-sync user"
    fi

    # Ensure kb-sync can write to the bare repos.
    chown -R kb-sync /opt/stackwiz/kb-publish/ 2>/dev/null || true
}

stackwiz_kb_unpublish() {
    local repo_name="$1"
    local bare="/opt/stackwiz/kb-publish/${repo_name}.git"
    rm -rf "$bare"
    echo "stackwiz-kb-publish: removed $bare"
}
