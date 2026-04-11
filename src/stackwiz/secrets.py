"""Password generation + Vault KV v2 persistence, honoring `immutable:`."""
from __future__ import annotations

import secrets
import string
from dataclasses import dataclass

from stackwiz.manifest import Manifest, Secret
from stackwiz.vault_client import VaultClient

PASSWORD_ALPHABET = string.ascii_letters + string.digits


def generate_password(length: int) -> str:
    if length <= 0:
        raise ValueError("password length must be > 0")
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


@dataclass
class MaterializedSecret:
    id: str
    vault_path: str
    value: str
    regenerated: bool


def materialize_secrets(
    manifest: Manifest,
    vault: VaultClient,
) -> dict[str, MaterializedSecret]:
    """Ensure every manifest secret exists in Vault; generate if missing.

    Immutable secrets are never regenerated. Returns a mapping `secret_id -> info`.
    """
    results: dict[str, MaterializedSecret] = {}
    for spec in manifest.secrets:
        vault_path = _secret_path(manifest, spec)
        existing = vault.kv_get(vault_path)
        if existing and "value" in existing:
            results[spec.id] = MaterializedSecret(
                id=spec.id,
                vault_path=vault_path,
                value=existing["value"],
                regenerated=False,
            )
            continue
        if not spec.generate:
            raise RuntimeError(
                f"secret {spec.id!r} is missing at {vault_path!r} and generate=false"
            )
        value = generate_password(spec.length)
        vault.kv_put(vault_path, {"value": value})
        results[spec.id] = MaterializedSecret(
            id=spec.id,
            vault_path=vault_path,
            value=value,
            regenerated=True,
        )
    return results


def delete_secrets(manifest: Manifest, vault: VaultClient) -> list[str]:
    """Remove all non-immutable secrets; return the paths that were deleted."""
    deleted: list[str] = []
    for spec in manifest.secrets:
        if spec.immutable:
            continue
        path = _secret_path(manifest, spec)
        vault.kv_delete_metadata(path)
        deleted.append(path)
    return deleted


def _secret_path(manifest: Manifest, spec: Secret) -> str:
    return spec.vault_path or f"{manifest.consul.service_prefix}/{spec.id}"
