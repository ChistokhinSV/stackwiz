"""Non-interactive (headless) install mode.

Runs the same engine as the TUI but prints progress to stdout. Suitable for
CI provisioning, Vagrant smoke tests, and anywhere you can't drive a terminal.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from stackwiz import log as log_module
from stackwiz.config_overrides import effective_config
from stackwiz.consul_client import ConsulClient
from stackwiz.discovery import probe_consul, probe_vault
from stackwiz.engine import Engine, Status, StepEvent
from stackwiz.executor import Executor
from stackwiz.manifest import Manifest
from stackwiz.state import State
from stackwiz.vault_client import VaultClient

log = logging.getLogger("stackwiz.headless")


def _load_config_values(
    manifest: Manifest, state: State, env_file: Path | None
) -> tuple[dict[str, Any], str]:
    """Resolve config values using the shared override+substitution helper."""
    values, domain = effective_config(manifest, state.config(), env_file)
    missing = [
        f.id for f in manifest.config if f.required and (values.get(f.id) in (None, ""))
    ]
    if missing:
        raise RuntimeError(
            f"headless mode requires values for required config: {', '.join(missing)}. "
            f"Provide them in /manifest/.stackwiz.env (YAML)."
        )
    return values, domain


async def _run(
    manifest: Manifest,
    state_dir: Path,
    manifest_dir: Path,
    mode: str,
    config_env_file: Path | None,
    selected_override: set[str] | None = None,
    forced_refresh: set[str] | None = None,
) -> int:
    state = State(state_dir)
    executor = Executor(manifest_dir=manifest_dir)

    # Compute effective config + domain (state > .stackwiz.env > defaults, with
    # ${var} substitution) so probes use the overridden domain.
    config_values, effective_domain = _load_config_values(manifest, state, config_env_file)
    print(f"[auto] effective domain: {effective_domain}")

    consul_probe = await probe_consul(effective_domain, manifest.consul_host)
    vault_probe = await probe_vault(effective_domain, manifest.vault_host)

    consul_client: ConsulClient | None = None
    vault_client: VaultClient | None = None
    if consul_probe.reachable and consul_probe.address:
        consul_client = ConsulClient(consul_probe.address)
        print(f"[auto] consul: {consul_probe.address} ({consul_probe.source.value})")
    else:
        print(f"[auto] consul: not reachable ({consul_probe.detail})")
    if vault_probe.reachable and vault_probe.address:
        token_file = state_dir / "vault-token"
        token = token_file.read_text().strip() if token_file.exists() else None
        vault_client = VaultClient(vault_probe.address, token=token)
        print(f"[auto] vault: {vault_probe.address} ({vault_probe.source.value})")
    else:
        print(f"[auto] vault: not reachable ({vault_probe.detail})")

    component_ids = {c.id for c in manifest.components}
    if mode == "install":
        # Only block if a REQUIRED backend is missing and not installable
        required_missing = []
        if not consul_probe.reachable and manifest.consul.required and "consul" not in component_ids:
            required_missing.append("consul")
        if not vault_probe.reachable and manifest.secrets and "vault" not in component_ids:
            required_missing.append("vault")
        if required_missing:
            print(
                "[auto] ERROR: required backends not reachable and not in manifest: "
                f"{', '.join(required_missing)}",
                file=sys.stderr,
            )
            return 2

    engine = Engine(
        manifest=manifest,
        state=state,
        executor=executor,
        consul=consul_client,
        vault=vault_client,
    )

    if mode == "install":
        if selected_override is not None:
            selected = set(selected_override)
        else:
            selected = {
                c.id for c in manifest.components if c.required or c.default
            }
        print(f"[auto] selected: {', '.join(sorted(selected))}")
        if forced_refresh:
            print(f"[auto] forced refresh: {', '.join(sorted(forced_refresh))}")
        print(f"[auto] config: {config_values}")
        print("[auto] starting install")
        try:
            failed = await _drive(engine.install(selected, config_values, forced_refresh))
        except RuntimeError as exc:
            # Typically raised by materialize_secrets when a user-supplied
            # secret is missing from both Vault and `.stackwiz.secrets.env`.
            print(f"[auto] ERROR: {exc}", file=sys.stderr)
            return 2
    else:
        if selected_override is not None:
            selected = set(selected_override)
            # Drop anything the user asked for that was never installed.
            installed_ids = set(state.installed().keys())
            orphans = selected - installed_ids
            if orphans:
                print(f"[auto] note: not installed: {', '.join(sorted(orphans))}")
            selected = selected & installed_ids
        else:
            selected = set(state.installed().keys())
        print(f"[auto] uninstalling: {', '.join(sorted(selected))}")
        failed = await _drive(engine.uninstall(selected))

    if failed:
        print("[auto] FAILED", file=sys.stderr)
        return 1
    print("[auto] OK")
    if engine.last_run is not None and engine.last_run.secrets:
        print("[auto] secrets:")
        for sid, info in engine.last_run.secrets.items():
            marker = "new" if info.regenerated else "existing"
            print(f"  {sid}: {info.vault_path} ({marker})")
    return 0


async def _drive(iterator) -> bool:
    """Consume an engine async iterator, print events, return True on failure."""
    failed = False
    current: str | None = None
    async for event in iterator:
        assert isinstance(event, StepEvent)
        if event.line is not None:
            prefix = "ERR " if event.stream == "stderr" else "    "
            print(f"{prefix}[{event.component_id}] {event.line}")
            continue
        if event.component_id != current:
            current = event.component_id
            print(f"[{event.component_id}] {event.status.value}: {event.action.value}"
                  + (f" — {event.message}" if event.message else ""))
        elif event.status is Status.DONE:
            print(f"[{event.component_id}] done")
        elif event.status is Status.FAILED:
            failed = True
            print(f"[{event.component_id}] FAILED (exit {event.exit_code})",
                  file=sys.stderr)
        elif event.status is Status.SKIPPED:
            print(f"[{event.component_id}] skipped: {event.message}")
    return failed


def run_headless(
    manifest: Manifest,
    state_dir: Path,
    manifest_dir: Path,
    mode: str = "install",
    config_env_file: Path | None = None,
    selected_override: set[str] | None = None,
    forced_refresh: set[str] | None = None,
) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    log_module.configure(state_dir, mode=f"headless:{mode}")
    return asyncio.run(
        _run(
            manifest, state_dir, manifest_dir, mode, config_env_file,
            selected_override, forced_refresh,
        )
    )
