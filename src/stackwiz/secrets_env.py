"""Read/write `.stackwiz.secrets.env` — user-supplied values for `generate: false`.

The file holds values the operator must provide before install (e.g. SMTP
passwords, API keys for third-party services). `wizinstall init-env`
scaffolds it with one empty line per `generate: false` secret, alongside
`.stackwiz.env`. On `wizinstall run`, filled entries are pushed to Vault
and removed from the file — the on-disk copy converges on "empty" after a
successful run, so sensitive values live only in Vault.

Entries left empty at run time cause `materialize_secrets` to raise with a
pointer back to this file and the target Vault path.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from stackwiz.manifest import Manifest, Secret

SECRETS_ENV_FILENAME = ".stackwiz.secrets.env"


def user_secret_specs(manifest: Manifest) -> list[Secret]:
    """Every manifest secret whose value the operator must supply."""
    return [s for s in manifest.secrets if not s.generate]


def secret_vault_path(manifest: Manifest, spec: Secret) -> str:
    return spec.vault_path or f"{manifest.consul.service_prefix}/{spec.id}"


def load_secrets_env(path: Path) -> dict[str, str]:
    """Load a `secret_id -> value` mapping. Missing/bad file → empty dict."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, str] = {}
    for k, v in data.items():
        result[str(k)] = "" if v is None else str(v)
    return result


def filled_entries(values: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in values.items() if v != ""}


def write_secrets_env_scaffold(
    path: Path,
    manifest: Manifest,
    existing: dict[str, str] | None = None,
    specs: list[Secret] | None = None,
) -> None:
    """Write a commented YAML skeleton with one line per user-supplied secret.

    Existing values are preserved. Keys not in `specs` are dropped entirely.
    When `specs` is empty the file is deleted (if present) — a clean-state
    signal that every user-supplied secret is now in Vault.
    """
    specs = specs if specs is not None else user_secret_specs(manifest)
    existing = existing or {}

    if not specs:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        return

    lines: list[str] = []
    lines.append(
        f"# stackwiz user-supplied secrets for "
        f"{manifest.display_name} v{manifest.version}"
    )
    lines.append("#")
    lines.append("# Fill in values below, then run `wizinstall run`.")
    lines.append("# Filled entries are uploaded to Vault and REMOVED from this file,")
    lines.append("# so secrets do not linger on disk. Empty entries cause the run to")
    lines.append("# hard-error with the target Vault path.")
    lines.append("")

    for spec in specs:
        vault_path = secret_vault_path(manifest, spec)
        lines.append(f"# Vault path: {vault_path}")
        value = existing.get(spec.id, "")
        lines.append(_yaml_line(spec.id, value))
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def rewrite_after_upload(
    path: Path,
    manifest: Manifest,
    uploaded_ids: set[str],
) -> None:
    """Regenerate the file with uploaded keys removed.

    If nothing user-supplied remains in the manifest after removing uploaded
    ids, the file is deleted entirely.
    """
    if not path.exists():
        return
    existing = load_secrets_env(path)
    remaining_existing = {k: v for k, v in existing.items() if k not in uploaded_ids}
    remaining_specs = [
        s for s in user_secret_specs(manifest) if s.id not in uploaded_ids
    ]
    write_secrets_env_scaffold(path, manifest, remaining_existing, remaining_specs)


def _yaml_line(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}: "{escaped}"'
