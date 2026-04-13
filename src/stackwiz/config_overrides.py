"""Compute effective config values with ${var} substitution.

Priority, highest wins:
    1. <manifest_dir>/.stackwiz.env (operator pre-seed — git-tracked intent)
    2. <state_dir>/config.yaml (cache of the last successful run's values)
    3. Manifest `default:` fields

`.stackwiz.env` beats state because it's the explicit operator override for a
specific deployment. Without this ordering, a prior install's cached
`state/config.yaml` would clobber the `${domain}` cascade whenever the
operator edits the env file post-install.

After merging, every string value is `${id}`-substituted against the merged
map. That lets consumer manifests declare

    domain: "example.com"
    config:
      - id: authentik_hostname
        default: "auth.${domain}"
      - id: ldap_base_dn
        default: "${domain_dn}"       # synthetic: dc=stackwiz,dc=lab

and have everything downstream update from a single `domain:` override in
`.stackwiz.env`.
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
    """`example.com` → `dc=example,dc=com`. Empty string → empty."""
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
    template_fields: set[str] = set()
    for field in manifest.config:
        if field.default is not None:
            merged[field.id] = field.default
            if isinstance(field.default, str) and "${" in field.default:
                template_fields.add(field.id)

    # Apply state cache, but SKIP any field whose manifest default is a
    # template (contains `${...}`). State stores fully-resolved values from
    # prior runs, so a cached `authentik_hostname: auth.example.com` would
    # otherwise clobber the `auth.${domain}` template and break the cascade
    # when the operator changes `domain` in `.stackwiz.env`.
    for key, val in state_config.items():
        if key == "domain":
            merged[key] = val
        elif key in {f.id for f in manifest.config} and key not in template_fields:
            merged[key] = val

    # `.stackwiz.env` overrides everything — this is explicit operator intent
    # and wins over both state cache and manifest defaults. Templated fields
    # can still be overridden here (uncommented line in the env file).
    env_overrides = _load_env_file(env_file)
    for key, val in env_overrides.items():
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
