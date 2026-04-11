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

import yaml

from stackwiz import log as log_module
from stackwiz.consul_client import ConsulClient
from stackwiz.discovery import probe_consul, probe_vault
from stackwiz.engine import Engine, Status, StepEvent
from stackwiz.executor import Executor
from stackwiz.manifest import Manifest
from stackwiz.state import State
from stackwiz.vault_client import VaultClient

log = logging.getLogger("stackwiz.headless")


def _load_config_values(manifest: Manifest, state: State, env_file: Path | None) -> dict[str, Any]:
    """Resolve config values in order of priority: env file → state → manifest defaults."""
    values: dict[str, Any] = {}
    for field in manifest.config:
        values[field.id] = field.default
    values.update(state.config())
    if env_file is not None and env_file.exists():
        try:
            overrides = yaml.safe_load(env_file.read_text(encoding="utf-8")) or {}
            if isinstance(overrides, dict):
                values.update(overrides)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s: %s", env_file, exc)
    missing = [
        f.id for f in manifest.config if f.required and (values.get(f.id) in (None, ""))
    ]
    if missing:
        raise RuntimeError(
            f"headless mode requires values for required config: {', '.join(missing)}. "
            f"Provide them in /manifest/.stackwiz.env (YAML)."
        )
    return values


async def _run(
    manifest: Manifest,
    state_dir: Path,
    manifest_dir: Path,
    mode: str,
    config_env_file: Path | None,
) -> int:
    state = State(state_dir)
    executor = Executor(manifest_dir=manifest_dir)

    consul_probe = await probe_consul(manifest.domain, manifest.consul_host)
    vault_probe = await probe_vault(manifest.domain, manifest.vault_host)

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
        needed = {"consul", "vault"}
        missing_backends = [name for name in needed if name not in component_ids]
        if (not consul_probe.reachable or not vault_probe.reachable) and missing_backends:
            print(
                "[auto] ERROR: backends not reachable and not in manifest: "
                f"{', '.join(missing_backends)}",
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
        selected = {
            c.id for c in manifest.components if c.required or c.default
        }
        config_values = _load_config_values(manifest, state, config_env_file)
        print(f"[auto] selected: {', '.join(sorted(selected))}")
        print(f"[auto] config: {config_values}")
        print("[auto] starting install")
        failed = await _drive(engine.install(selected, config_values))
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
) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    log_module.configure(state_dir)
    return asyncio.run(_run(manifest, state_dir, manifest_dir, mode, config_env_file))
