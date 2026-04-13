"""`wizinstall info` — summarize installed components, URLs, secrets.

Also used by the engine to write `<state_dir>/summary.md` at the end of every
successful install run so operators always have a "where is everything" file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from stackwiz.consul_client import ConsulClient
from stackwiz.discovery import probe_consul, probe_vault
from stackwiz.manifest import Manifest
from stackwiz.state import State
from stackwiz.vault_client import VaultClient


@dataclass
class ServiceInfo:
    name: str
    address: str
    port: int
    tags: list[str]


@dataclass
class SecretInfo:
    id: str
    vault_path: str
    masked: str          # always safe to print
    value: str | None    # None unless show_secrets=True
    immutable: bool


@dataclass
class ComponentInfo:
    id: str
    name: str
    version: str
    installed_at: str
    services: list[ServiceInfo]


@dataclass
class ReportData:
    manifest_name: str
    manifest_version: str
    domain: str
    components: list[ComponentInfo]
    secrets: list[SecretInfo]
    config: dict[str, Any]
    state_dir: Path        # actual Path where the file was written (container-side)
    host_state_dir: str    # host-facing display string (where the operator looks)


def collect(
    manifest: Manifest,
    state: State,
    consul: ConsulClient | None,
    vault: VaultClient | None,
    show_secrets: bool,
) -> ReportData:
    """Gather everything into a ReportData (pure, easy to unit-test)."""
    by_id = {c.id: c for c in manifest.components}
    components: list[ComponentInfo] = []
    for cid, entry in sorted(state.installed().items()):
        component = by_id.get(cid)
        if component is None:
            continue
        services: list[ServiceInfo] = []
        if consul is not None:
            for svc in component.all_consul_services():
                discovered = consul.discover(svc)
                if discovered is None:
                    services.append(ServiceInfo(
                        name=svc.name, address="(not registered)",
                        port=svc.port, tags=list(svc.tags),
                    ))
                else:
                    services.append(ServiceInfo(
                        name=discovered.name,
                        address=discovered.address or "127.0.0.1",
                        port=discovered.port or svc.port,
                        tags=discovered.tags or list(svc.tags),
                    ))
        else:
            for svc in component.all_consul_services():
                services.append(ServiceInfo(
                    name=svc.name, address="(consul unreachable)",
                    port=svc.port, tags=list(svc.tags),
                ))
        components.append(ComponentInfo(
            id=cid,
            name=component.name,
            version=entry.version,
            installed_at=entry.installed_at,
            services=services,
        ))

    secrets: list[SecretInfo] = []
    for spec in manifest.secrets:
        path = spec.vault_path or f"{manifest.consul.service_prefix}/{spec.id}"
        raw: str | None = None
        if vault is not None:
            data = vault.kv_get(path)
            if data and "value" in data:
                raw = data["value"]
        masked = _mask(raw) if raw else "(not in vault)"
        secrets.append(SecretInfo(
            id=spec.id,
            vault_path=path,
            masked=masked,
            value=raw if show_secrets else None,
            immutable=spec.immutable,
        ))

    effective_domain = state.config().get("domain", manifest.domain) or manifest.domain

    return ReportData(
        manifest_name=manifest.display_name,
        manifest_version=manifest.version,
        domain=effective_domain,
        components=components,
        secrets=secrets,
        config=state.config(),
        state_dir=state.state_dir,
        host_state_dir=state.host_path(),
    )


def _mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 6:
        return "***"
    return value[:4] + "…" + value[-2:]


# --- rendering --------------------------------------------------------------


def render_markdown(report: ReportData, *, show_secrets: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"# {report.manifest_name} — v{report.manifest_version}")
    lines.append("")
    lines.append(f"- **Domain**: `{report.domain}`")
    lines.append(f"- **State directory**: `{report.host_state_dir}`")
    lines.append(f"- **Install log**: `{report.host_state_dir}/install.log`")
    lines.append(f"- **Summary**: `{report.host_state_dir}/summary.md`")
    lines.append("")

    lines.append("## Components")
    lines.append("")
    if not report.components:
        lines.append("_Nothing installed yet._")
    else:
        lines.append("| Component | Version | Installed at | Services |")
        lines.append("|---|---|---|---|")
        for c in report.components:
            svcs = "<br/>".join(
                f"`{s.name}` → `{s.address}:{s.port}`"
                for s in c.services
            ) or "—"
            lines.append(
                f"| **{c.name}** (`{c.id}`) | `{c.version}` | {c.installed_at} | {svcs} |"
            )
    lines.append("")

    lines.append("## Config values")
    lines.append("")
    if not report.config:
        lines.append("_None_")
    else:
        lines.append("| Key | Value |")
        lines.append("|---|---|")
        for k, v in sorted(report.config.items()):
            lines.append(f"| `{k}` | `{v}` |")
    lines.append("")

    lines.append("## Secrets (Vault KV)")
    lines.append("")
    if not report.secrets:
        lines.append("_None_")
    else:
        header = "| ID | Vault path | Value |" if show_secrets else "| ID | Vault path | Masked |"
        lines.append(header)
        lines.append("|---|---|---|")
        for s in report.secrets:
            flag = " 🔒" if s.immutable else ""
            shown = s.value if (show_secrets and s.value) else s.masked
            lines.append(f"| `{s.id}`{flag} | `{s.vault_path}` | `{shown}` |")
    lines.append("")

    return "\n".join(lines) + "\n"


def render_text(report: ReportData, *, show_secrets: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"{report.manifest_name}  v{report.manifest_version}")
    lines.append(f"domain: {report.domain}")
    lines.append(f"state:  {report.host_state_dir}")
    lines.append(f"log:    {report.host_state_dir}/install.log")
    lines.append("")

    lines.append("Components")
    lines.append("----------")
    if not report.components:
        lines.append("  (none)")
    else:
        for c in report.components:
            lines.append(f"  {c.id}  v{c.version}  ({c.installed_at})")
            for s in c.services:
                lines.append(f"    {s.name:20s}  {s.address}:{s.port}")
    lines.append("")

    if report.config:
        lines.append("Config")
        lines.append("------")
        for k, v in sorted(report.config.items()):
            lines.append(f"  {k:20s} = {v}")
        lines.append("")

    lines.append("Secrets")
    lines.append("-------")
    if not report.secrets:
        lines.append("  (none)")
    else:
        for s in report.secrets:
            flag = " [immutable]" if s.immutable else ""
            shown = s.value if (show_secrets and s.value) else s.masked
            lines.append(f"  {s.id:30s} vault:{s.vault_path}{flag}")
            lines.append(f"    {shown}")
    lines.append("")
    return "\n".join(lines)


def render_json(report: ReportData, *, show_secrets: bool = False) -> str:
    def _svc(s: ServiceInfo) -> dict:
        return {"name": s.name, "address": s.address, "port": s.port, "tags": s.tags}

    def _component(c: ComponentInfo) -> dict:
        return {
            "id": c.id,
            "name": c.name,
            "version": c.version,
            "installed_at": c.installed_at,
            "services": [_svc(s) for s in c.services],
        }

    def _secret(s: SecretInfo) -> dict:
        return {
            "id": s.id,
            "vault_path": s.vault_path,
            "value": s.value if show_secrets else None,
            "masked": s.masked,
            "immutable": s.immutable,
        }

    payload = {
        "manifest": {
            "name": report.manifest_name,
            "version": report.manifest_version,
            "domain": report.domain,
        },
        "state_dir": report.host_state_dir,
        "components": [_component(c) for c in report.components],
        "config": report.config,
        "secrets": [_secret(s) for s in report.secrets],
    }
    return json.dumps(payload, indent=2)


# --- summary.md side-effect -------------------------------------------------


def write_summary_md(
    manifest: Manifest,
    state: State,
    consul: ConsulClient | None,
    vault: VaultClient | None,
) -> Path:
    """Atomic write of `<state_dir>/summary.md` for engine post-install."""
    report = collect(manifest, state, consul, vault, show_secrets=False)
    body = render_markdown(report, show_secrets=False)
    target = state.state_dir / "summary.md"
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, target)
    return target


# --- CLI entry --------------------------------------------------------------


def render_info(
    manifest: Manifest,
    state_dir: Path,
    show_secrets: bool,
    output_format: str,
) -> int:
    """Called by `wizinstall info`. Builds clients from discovery probes."""
    import asyncio

    state = State(state_dir)
    effective_domain = state.config().get("domain", manifest.domain) or manifest.domain
    consul_client: ConsulClient | None = None
    vault_client: VaultClient | None = None

    consul_probe = asyncio.run(probe_consul(effective_domain, manifest.consul_host))
    if consul_probe.reachable and consul_probe.address:
        token_file = state_dir / "consul-http-token"
        token = token_file.read_text().strip() if token_file.exists() else None
        consul_client = ConsulClient(consul_probe.address, token=token)

    vault_probe = asyncio.run(probe_vault(effective_domain, manifest.vault_host))
    if vault_probe.reachable and vault_probe.address:
        token_file = state_dir / "vault-token"
        token = token_file.read_text().strip() if token_file.exists() else None
        if not token:
            token = os.environ.get("VAULT_TOKEN", "").strip() or None
        vault_client = VaultClient(vault_probe.address, token=token)

    report = collect(manifest, state, consul_client, vault_client, show_secrets)

    if output_format == "markdown":
        click.echo(render_markdown(report, show_secrets=show_secrets), nl=False)
    elif output_format == "json":
        click.echo(render_json(report, show_secrets=show_secrets))
    else:
        click.echo(render_text(report, show_secrets=show_secrets), nl=False)

    # Refresh summary.md whenever `info` is run (keeps it current on re-runs).
    write_summary_md(manifest, state, consul_client, vault_client)
    return 0
