"""File scaffolding for consumer projects.

Business logic behind the ``wizinstall init-env`` and ``wizinstall
extract-bootstrap`` subcommands. Separated from ``cli.py`` so the logic can
be unit-tested without Click and reused by other entry points (e.g. a future
VS Code integration).

The functions here never write to stdout. The CLI layer is responsible for
user-facing output; these return structured results.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from stackwiz.manifest import Manifest
from stackwiz.secrets_env import (
    SECRETS_ENV_FILENAME,
    load_secrets_env,
    user_secret_specs,
    write_secrets_env_scaffold,
)

log = logging.getLogger("stackwiz.scaffold")

# Conservative domain charset: dotted labels of alphanum + hyphen. Same shape
# we use for hostnames elsewhere; rejects spaces, slashes, quoting tricks.
_DOMAIN_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
_DOMAIN_RE = re.compile(rf"^{_DOMAIN_LABEL}(\.{_DOMAIN_LABEL})*$")


@dataclass
class EnvScaffoldResult:
    """Return value of ``scaffold_env_files``.

    Each path is non-None when the corresponding file was written; callers
    use this to emit user-friendly log lines without duplicating the logic.
    """

    env_file: Path
    secrets_file: Path | None = None
    dot_env_file: Path | None = None
    gitignore_added: list[str] = field(default_factory=list)


@dataclass
class BootstrapWriteResult:
    lib_path: Path
    launcher_path: Path
    config_path: Path
    config_created: bool  # False if pre-existing config was preserved


BOOTSTRAP_CONFIG_TEMPLATE = """#!/usr/bin/env bash
# stackwiz consumer bootstrap config — the ONE editable piece.
#
# EDIT THIS FILE to customize the bootstrap for your project. Override only
# what you need; every SW_* variable has a sensible default in the library.
#
# The paired bootstrap.sh launcher and stackwiz-bootstrap.sh library are
# both framework-managed (do not edit). Refresh them together via:
#     docker run --rm -v "$PWD:/out" ghcr.io/chistokhinsv/stackwiz:latest \\
#         extract-bootstrap --output /out --force
# which preserves this file.

# Host packages to ensure before running. Drop jq/openssl/envsubst if unused.
# SW_REQUIRED_PKGS=(curl ca-certificates jq openssl gettext-base python3)

# Extra env vars to pass through to the installer container (beyond the
# canonical set: CONSUL_HTTP_ADDR, VAULT_ADDR/TOKEN, CF/AWS DNS, CERTBOT_EMAIL,
# STACKWIZ_TLS_FORCE, STACKWIZ_HOST_STATE_DIR, STACKWIZ_HOST_MANIFEST_DIR).
# SW_EXTRA_ENV=(CONSUL_HTTP_TOKEN)

# Files the installer may write as root; reclaimed after each writable run.
# SW_CHOWN_FILES=(.stackwiz.env .stackwiz.secrets.env .env)

# Default manifest mount mode (0 = ro, 1 = rw). Write-commands below flip it
# regardless. Set to 1 only if install-time secret redaction rewrites files.
# SW_WRITABLE_DEFAULT=0

# Args that force manifest RW / RO mount respectively.
# SW_WRITE_CMDS=(init-env)
# SW_READONLY_CMDS=(validate list info)

# Args that imply headless run (no -it).
# SW_HEADLESS_ARGS=(--auto --validate validate list info init-env)
"""


# --- env file scaffolding ---------------------------------------------------


def scaffold_env_files(
    manifest: Manifest,
    manifest_dir: Path,
    target: Path,
    force: bool,
    domain: str | None = None,
) -> EnvScaffoldResult:
    """Write ``.stackwiz.env`` + optional companion files.

    Creates:
      * ``target`` (typically ``<manifest_dir>/.stackwiz.env``) — always
      * ``<manifest_dir>/.stackwiz.secrets.env`` — when the manifest declares
        user-provided secrets (``secrets[*].source: user``)
      * ``<manifest_dir>/.env`` — when the manifest has TLS-related config
        fields or when ``<manifest_dir>/.env.template`` exists
      * appends each of the above to ``<manifest_dir>/.gitignore`` if missing

    When ``domain`` is provided, the generated file's top-level ``domain:``
    is set to that value (instead of the manifest default) and every
    ``${domain}``-derived field in the comments renders against it — so the
    operator can immediately run ``wizinstall run`` without hand-editing.

    Raises ``FileExistsError`` if ``target`` exists and ``force`` is False.
    Raises ``ValueError`` if ``domain`` is syntactically invalid.
    """
    target = target.resolve()
    if target.exists() and not force:
        raise FileExistsError(target)

    from stackwiz.config_overrides import effective_config

    existing = _try_read_existing_yaml(target)
    if domain is not None:
        domain = domain.strip()
        if not domain or not _DOMAIN_RE.match(domain):
            raise ValueError(
                f"invalid domain '{domain}': expected dotted alphanumeric "
                "labels (e.g. 'example.com', 'lab.mycompany.internal')"
            )
        # Inject the override before `effective_config` so derived fields
        # (auth.${domain}, admin@${domain}) resolve against the new domain.
        existing = dict(existing)
        existing["domain"] = domain
    resolved, domain = effective_config(manifest, existing, None)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _render_env_text(manifest, resolved, domain, existing),
        encoding="utf-8",
    )
    _chmod_600(target)

    result = EnvScaffoldResult(env_file=target)

    user_specs = user_secret_specs(manifest)
    if user_specs:
        secrets_target = (manifest_dir / SECRETS_ENV_FILENAME).resolve()
        existing_secrets = load_secrets_env(secrets_target)
        write_secrets_env_scaffold(
            secrets_target, manifest, existing_secrets, user_specs,
        )
        result.secrets_file = secrets_target

    dot_env = manifest_dir / ".env"
    env_template = manifest_dir / ".env.template"
    tls_fields = {f.id for f in manifest.config} & {"tls_mode", "certbot_email"}
    if env_template.exists() or tls_fields:
        if not dot_env.exists() or force:
            _write_dot_env(dot_env, env_template if env_template.exists() else None)
            result.dot_env_file = dot_env.resolve()

    gitignore_entries = [".stackwiz.env", SECRETS_ENV_FILENAME, ".env"]
    result.gitignore_added = _ensure_gitignore_entries(
        manifest_dir / ".gitignore", gitignore_entries,
    )
    return result


def _render_env_text(
    manifest: Manifest,
    resolved: dict,
    domain: str,
    existing: dict,
) -> str:
    lines: list[str] = [
        f"# stackwiz consumer config overrides for "
        f"{manifest.display_name} v{manifest.version}",
        "#",
        "# EDIT ONLY WHAT YOU NEED TO OVERRIDE.",
        "# Fields below are commented out — uncomment to override the manifest default.",
        "# Change `domain:` and every ${domain}-derived field auto-updates at run time.",
        "# Precedence (highest wins):",
        "#   <state_dir>/config.yaml > this file > manifest `default:` fields",
        "",
        "# ---- Deployment domain ----",
        "# Drives Consul/Vault service discovery (consul.<domain> / vault.<domain>)",
        "# and is referenced via ${domain} by other fields in the manifest.",
        _yaml_line("domain", domain),
        "",
        "# ---- Component configuration ----",
        "# Values shown in comments are what the current `domain:` resolves to —",
        "# they're HINTS, not defaults baked into this file. Uncomment a line to",
        "# force that specific value regardless of the cascade.",
        "",
    ]
    for f in manifest.config:
        if f.help:
            lines.append(f"# {f.label}: {f.help}")
        else:
            lines.append(f"# {f.label}")
        if f.type == "select" and f.choices:
            lines.append(f"#   choices: {', '.join(f.choices)}")
        if f.required:
            lines.append("#   required")
        val = resolved.get(f.id, f.default)
        yaml_line = _yaml_line(f.id, val)
        # Preserve user's explicit override as uncommented; else emit hint.
        if f.id in existing and f.id != "domain":
            lines.append(yaml_line)
        else:
            lines.append(f"# {yaml_line}")
        lines.append("")
    return "\n".join(lines)


def _try_read_existing_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("could not parse %s as YAML, ignoring: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write_dot_env(path: Path, template: Path | None) -> None:
    lines = [
        "# stackwiz bootstrap credentials",
        "# Fill the credentials you need; bootstrap.sh sources this file automatically.",
        "# This file is gitignored -- it contains secrets.",
        "",
        "# ---- TLS / Let's Encrypt DNS challenge credentials ----",
        "# Uncomment ONE provider block. If none are set and tls_mode is \"auto\",",
        "# the TLS helper falls back to self-signed certificates.",
        "",
        "# Cloudflare DNS-01 (fastest, recommended):",
        "#CF_DNS_API_TOKEN=",
        "",
        "# AWS Route53 DNS-01:",
        "#AWS_DNS_ACCESS_KEY_ID=",
        "#AWS_DNS_SECRET_ACCESS_KEY=",
    ]
    if template is not None and template.exists():
        lines.append("")
        lines.append(f"# ---- From {template.name} ----")
        for raw in template.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                lines.append(raw)
            else:
                # Comment out KEY=VALUE so operator opts in explicitly.
                lines.append(f"#{raw}")
        if lines and lines[-1] != "":
            lines.append("")
    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")
    _chmod_600(path)


def _ensure_gitignore_entries(path: Path, entries: list[str]) -> list[str]:
    """Idempotently append ``entries`` to ``path``. Returns what was added."""
    existing_lines: list[str] = []
    existing_set: set[str] = set()
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()
        for line in existing_lines:
            stripped = line.strip()
            if stripped.startswith("./"):
                stripped = stripped[2:]
            elif stripped.startswith("/"):
                stripped = stripped[1:]
            if stripped:
                existing_set.add(stripped)
    missing = [e for e in entries if e not in existing_set]
    if not missing:
        return []
    out = list(existing_lines)
    if out and out[-1] != "":
        out.append("")
    out.extend(missing)
    out.append("")
    path.write_text("\n".join(out), encoding="utf-8")
    return missing


def _yaml_line(key: str, value: object) -> str:
    """Emit ``key: value`` with appropriate YAML quoting."""
    if value is None:
        return f"{key}: null"
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}"
    if isinstance(value, (int, float)):
        return f"{key}: {value}"
    s = str(value)
    # Always quote strings; avoids YAML parsing surprises for numeric-looking
    # values like IP addresses.
    escaped = s.replace('"', '\\"')
    return f'{key}: "{escaped}"'


def _chmod_600(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


# --- bootstrap extraction ---------------------------------------------------


def read_bootstrap_library_text() -> str:
    """Return the packaged ``stackwiz-bootstrap.sh`` library as text."""
    return (
        files("stackwiz")
        .joinpath("share/bootstrap/stackwiz-bootstrap.sh")
        .read_text(encoding="utf-8")
    )


def read_bootstrap_launcher_text() -> str:
    """Return the packaged ``bootstrap.sh`` launcher as text."""
    return (
        files("stackwiz")
        .joinpath("share/bootstrap/bootstrap.sh")
        .read_text(encoding="utf-8")
    )


def write_bootstrap(
    output_dir: Path,
    launcher_name: str = "bootstrap.sh",
    config_name: str = "bootstrap.conf.sh",
    force: bool = False,
) -> BootstrapWriteResult:
    """Write the bootstrap triplet into ``output_dir``.

    Three files are involved, with different overwrite semantics:
      * ``stackwiz-bootstrap.sh`` — framework-managed library. Always
        written; overwritten only with ``force=True``.
      * ``<launcher_name>`` — framework-managed launcher. Same semantics.
      * ``<config_name>`` — consumer-owned config template. Written only
        if the file doesn't already exist. NEVER force-overwritten:
        ``force=True`` only refreshes the framework-managed pair.

    Raises ``FileExistsError`` for the library or launcher when they
    already exist and ``force`` is False.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    lib_path = output_dir / "stackwiz-bootstrap.sh"
    launcher_path = output_dir / launcher_name
    config_path = output_dir / config_name

    if not force:
        for p in (lib_path, launcher_path):
            if p.exists():
                raise FileExistsError(p)

    lib_path.write_text(read_bootstrap_library_text(), encoding="utf-8")
    launcher_path.write_text(read_bootstrap_launcher_text(), encoding="utf-8")

    config_created = False
    if not config_path.exists():
        config_path.write_text(BOOTSTRAP_CONFIG_TEMPLATE, encoding="utf-8")
        config_created = True

    try:
        lib_path.chmod(0o755)
        launcher_path.chmod(0o755)
        # Config is sourced, not executed — 0644 is fine and avoids
        # signaling it's directly runnable.
        if config_created:
            config_path.chmod(0o644)
    except OSError:
        pass
    return BootstrapWriteResult(
        lib_path=lib_path,
        launcher_path=launcher_path,
        config_path=config_path,
        config_created=config_created,
    )
