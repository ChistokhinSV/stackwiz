"""Secret generation + Vault KV v2 persistence, honoring `immutable:`.

Supported generator types (selected via manifest `type:` on each secret):
  password — random alnum string of length `length`
  hex      — `secrets.token_hex(length)` (length = bytes, output = 2×length)
  base64   — base64 of `length` random bytes (Consul gossip key = length 32)
  uuid     — `uuid.uuid4()`
  cmd      — run `command:` via subprocess, stdout stripped

`cmd` runs inside the stackwiz container. If the consumer needs host-side
execution, they spell out `nsenter --target 1 --all -- <cmd>` themselves.
"""
from __future__ import annotations

import base64
import logging
import secrets
import string
import subprocess
import uuid
from dataclasses import dataclass

from stackwiz.manifest import Manifest, Secret
from stackwiz.secrets_env import SECRETS_ENV_FILENAME
from stackwiz.vault_client import VaultClient

log = logging.getLogger(__name__)

PASSWORD_ALPHABET = string.ascii_letters + string.digits
CMD_TIMEOUT_SECONDS = 30


def generate_password(length: int) -> str:
    if length <= 0:
        raise ValueError("password length must be > 0")
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def generate_hex(length: int) -> str:
    if length <= 0:
        raise ValueError("hex length (bytes) must be > 0")
    return secrets.token_hex(length)


def generate_base64(length: int) -> str:
    if length <= 0:
        raise ValueError("base64 length (bytes) must be > 0")
    return base64.b64encode(secrets.token_bytes(length)).decode("ascii")


def generate_uuid() -> str:
    return str(uuid.uuid4())


def run_cmd_generator(command: str) -> str:
    """Execute `command` via shell and return its stdout, trailing WS stripped.

    Non-zero exit is a hard error — a misconfigured generator should abort
    the install, not silently produce an empty secret.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT_SECONDS,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"secret cmd generator timed out after {CMD_TIMEOUT_SECONDS}s: "
            f"{command!r}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            f"secret cmd generator failed (exit {exc.returncode}): {command!r}"
            + (f" — {stderr}" if stderr else "")
        ) from exc
    value = (result.stdout or "").strip()
    if not value:
        raise RuntimeError(
            f"secret cmd generator produced empty output: {command!r}"
        )
    return value


def generate_value(spec: Secret) -> str:
    """Dispatch to the right generator based on `spec.type`."""
    if spec.type == "password":
        return generate_password(spec.length)
    if spec.type == "hex":
        return generate_hex(spec.length)
    if spec.type == "base64":
        return generate_base64(spec.length)
    if spec.type == "uuid":
        return generate_uuid()
    if spec.type == "cmd":
        assert spec.command is not None  # guaranteed by Secret._type_contract
        return run_cmd_generator(spec.command)
    raise ValueError(f"unknown secret type: {spec.type!r}")


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
        if existing and existing.get("value"):
            results[spec.id] = MaterializedSecret(
                id=spec.id,
                vault_path=vault_path,
                value=existing["value"],
                regenerated=False,
            )
            continue
        if not spec.generate:
            if spec.optional:
                log.info(
                    "optional secret %r not set — using empty value "
                    "(fill %s or Vault path %s to enable)",
                    spec.id, SECRETS_ENV_FILENAME, vault_path,
                )
                results[spec.id] = MaterializedSecret(
                    id=spec.id,
                    vault_path=vault_path,
                    value="",
                    regenerated=False,
                )
                continue
            raise RuntimeError(
                f"secret {spec.id!r} is missing at Vault path {vault_path!r} "
                f"and generate=false. Fill `{SECRETS_ENV_FILENAME}` next to "
                f"the manifest (key `{spec.id}`) and re-run, or put it "
                f"directly in Vault with "
                f"`vault kv put stackwiz/{vault_path} value=<…>`."
            )
        value = generate_value(spec)
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
