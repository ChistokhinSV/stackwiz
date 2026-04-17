"""Token resolution and backend-client construction.

Consolidates the logic that previously lived in app.py, headless.py, and
info.py — all three built Consul + Vault clients from discovery probes
using the same fallback chain.

Call order matters: Vault is built first because its KV can hold the
Consul ACL token (at ``shared/consul_bootstrap_token``).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from stackwiz.consul_client import ConsulClient
from stackwiz.discovery import ProbeResult
from stackwiz.vault_client import VaultClient

log = logging.getLogger("stackwiz.tokens")


def read_sibling_state_token(state_dir: Path, filename: str) -> str | None:
    """Search sibling consumer state dirs for ``filename``.

    Namespaced state means each consumer owns its own dir
    (``/var/lib/stackwiz/awx-platform/``, ``.../consul-vault-authentik-docker/``
    etc.). Cross-consumer secrets (a consul ACL token written by 081, needed
    by 061) live in one of those siblings. Falling back to a sibling is safe
    — all namespaces on the same host share the same Vault/Consul backend.
    Cross-host installs must provide the token via env or Vault shared path.
    """
    base = state_dir.parent
    if not base.exists():
        return None
    for sibling in sorted(base.iterdir()):
        if sibling == state_dir or not sibling.is_dir():
            continue
        candidate = sibling / filename
        if candidate.exists():
            try:
                value = candidate.read_text().strip()
                if value:
                    return value
            except OSError:
                continue
    return None


def resolve_vault_token(state_dir: Path) -> str | None:
    """Token resolution order: own state > VAULT_TOKEN env > sibling state."""
    own = state_dir / "vault-token"
    if own.exists():
        value = own.read_text().strip()
        if value:
            return value
    env_value = os.environ.get("VAULT_TOKEN", "").strip()
    if env_value:
        return env_value
    sibling = read_sibling_state_token(state_dir, "vault-token")
    return sibling or None


def resolve_consul_token(
    state_dir: Path, vault: VaultClient | None
) -> str | None:
    """Token resolution order: own state > CONSUL_HTTP_TOKEN env > sibling
    state dirs > Vault ``shared/consul_bootstrap_token`` > anonymous.

    On a successful non-trivial resolution (env / sibling / Vault), the token
    is cached at ``<state_dir>/consul-http-token`` so subsequent runs skip the
    fallback chain. This also makes cross-consumer deploys work even after the
    operator removes the env var or 081 is uninstalled.
    """
    own = state_dir / "consul-http-token"
    if own.exists():
        value = own.read_text().strip()
        if value:
            return value

    def _cache(token: str) -> str:
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            own.write_text(token, encoding="utf-8")
            try:
                own.chmod(0o600)
            except OSError:
                pass
        except OSError:
            pass
        return token

    env_value = os.environ.get("CONSUL_HTTP_TOKEN", "").strip()
    if env_value:
        return _cache(env_value)
    sibling = read_sibling_state_token(state_dir, "consul-http-token")
    if sibling:
        return _cache(sibling)

    candidates: list[VaultClient] = []
    if vault is not None and vault.token:
        candidates.append(vault)
    env_vault_token = os.environ.get("VAULT_TOKEN", "").strip()
    if env_vault_token:
        vault_addr = (
            os.environ.get("VAULT_ADDR", "").strip()
            or (vault.address if vault is not None else "")
        )
        if vault_addr:
            try:
                candidates.append(VaultClient(vault_addr, token=env_vault_token))
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "fallback Vault client for consul-token lookup failed: %s", exc,
                )
    for vc in candidates:
        try:
            data = vc.kv_get("shared/consul_bootstrap_token")
            if data and data.get("value"):
                return _cache(str(data["value"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("consul-token via Vault KV failed: %s", exc)
            continue
    return None


def build_backends(
    state_dir: Path,
    consul_probe: ProbeResult,
    vault_probe: ProbeResult,
    ensure_kv_mount: bool = False,
) -> tuple[ConsulClient | None, VaultClient | None]:
    """Build Consul + Vault clients from discovery probes.

    Vault is built first so it can act as a fallback source for the Consul
    ACL token. Returns (None, None) for any backend whose probe was not
    reachable.

    When ``ensure_kv_mount`` is True, the KV mount is re-asserted on Vault —
    needed by the TUI on re-runs where the lazy-enable path has already
    finished on an earlier run. Errors are logged at WARNING and non-fatal.
    """
    vault_client: VaultClient | None = None
    if vault_probe.reachable and vault_probe.address:
        vault_client = VaultClient(
            vault_probe.address,
            token=resolve_vault_token(state_dir),
        )
        if ensure_kv_mount:
            try:
                vault_client.ensure_kv_mount()
            except Exception as exc:  # noqa: BLE001
                log.warning("vault kv mount ensure failed: %s", exc)

    consul_client: ConsulClient | None = None
    if consul_probe.reachable and consul_probe.address:
        consul_client = ConsulClient(
            consul_probe.address,
            token=resolve_consul_token(state_dir, vault_client),
        )
    return consul_client, vault_client
