"""Compute effective config values with ${var} substitution.

Priority, highest wins:
    1. <state_dir>/config.yaml (saved from the last successful run)
    2. <manifest_dir>/.stackwiz.env (operator pre-seed)
    3. Manifest `default:` fields

After merging, every string value (both the manifest's top-level `domain:`
and each `config:` field's effective value) is `${id}`-substituted against
the merged map. That lets consumer manifests declare

    domain: "stackwiz.lab"
    config:
      - id: authentik_hostname
        default: "auth.${domain}"
      - id: ldap_base_dn
        default: "dc=${domain_slug},dc=local"

and have everything downstream update from a single `domain:` override in
`.stackwiz.env` or the TUI config screen.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from stackwiz.manifest import Manifest

_PLACEHOLDER = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _load_env_file(env_file: Path | None) -> dict[str, Any]:
    if env_file is None or not env_file.exists():
        return {}
    try:
        data = yaml.safe_load(env_file.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _interpolate(template: str, values: dict[str, Any]) -> str:
    """Replace ${key} in `template` with `values[key]`. Unknown keys stay literal."""
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return str(values[key])
        return match.group(0)
    return _PLACEHOLDER.sub(repl, template)


def _recursive_interpolate(values: dict[str, Any], max_depth: int = 4) -> dict[str, Any]:
    """Repeatedly substitute until no `${…}` placeholders remain or max_depth hit.

    Supports one-hop chains like `auth.${domain}` where `domain: my.lab`.
    """
    out = dict(values)
    for _ in range(max_depth):
        changed = False
        for key, val in list(out.items()):
            if isinstance(val, str) and "${" in val:
                new_val = _interpolate(val, out)
                if new_val != val:
                    out[key] = new_val
                    changed = True
        if not changed:
            break
    return out


def _domain_to_dn(domain: str) -> str:
    """`example.lab` → `dc=example,dc=lab`. Empty string → empty."""
    parts = [p for p in domain.split(".") if p]
    return ",".join(f"dc={p}" for p in parts)


def effective_config(
    manifest: Manifest,
    state_config: dict[str, Any],
    env_file: Path | None,
) -> tuple[dict[str, Any], str]:
    """Return `(config_values, effective_domain)` after merge + substitution.

    `state_config` is whatever `State.config()` returned — passed as an arg so
    tests don't need a real state dir.

    Synthetic substitution keys (injected automatically, not manifest fields):
      - `${domain}`      — effective deployment domain
      - `${domain_dn}`   — domain rendered as LDAP base DN (`dc=a,dc=b`)
    """
    merged: dict[str, Any] = {"domain": manifest.domain}
    for field in manifest.config:
        if field.default is not None:
            merged[field.id] = field.default

    env_overrides = _load_env_file(env_file)
    for key, val in env_overrides.items():
        if key == "domain" or key in {f.id for f in manifest.config}:
            merged[key] = val

    for key, val in state_config.items():
        if key == "domain" or key in {f.id for f in manifest.config}:
            merged[key] = val

    # Inject derived values BEFORE interpolation so fields like
    # `default: "dc=${domain_dn}"` render correctly.
    effective_domain_raw = str(merged.get("domain", manifest.domain))
    merged.setdefault("domain_dn", _domain_to_dn(effective_domain_raw))

    resolved = _recursive_interpolate(merged)

    # Domain may have been ${var}-substituted itself; recompute domain_dn from
    # the final value so overrides propagate correctly.
    domain = str(resolved.get("domain", manifest.domain))
    resolved["domain_dn"] = _domain_to_dn(domain)
    resolved = _recursive_interpolate(resolved)

    config_values: dict[str, Any] = {
        f.id: resolved.get(f.id, f.default) for f in manifest.config
    }
    return config_values, domain
