"""Non-interactive (headless) install mode.

Runs the same engine as the TUI but routes progress through the stackwiz
logger so install.log captures every line. A dedicated stdout/stderr
handler is attached to the ``stackwiz.headless`` logger so operators also
see status + per-step events on their terminal.

Suitable for CI provisioning, Vagrant smoke tests, and anywhere you can't
drive a terminal.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from stackwiz import log as log_module
from stackwiz.config_overrides import effective_config
from stackwiz.discovery import probe_consul, probe_vault
from stackwiz.engine import Engine, Status, StepEvent
from stackwiz.executor import Executor
from stackwiz.manifest import Manifest
from stackwiz.state import State
from stackwiz.tokens import build_backends

log = logging.getLogger("stackwiz.headless")


class _HeadlessStreamHandler(logging.Handler):
    """Mirror every headless log record to stdout (or stderr for ERROR+)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001
            return
        stream = sys.stderr if record.levelno >= logging.ERROR else sys.stdout
        try:
            stream.write(msg + "\n")
            stream.flush()
        except Exception:  # noqa: BLE001
            pass


def _attach_stream_handler() -> _HeadlessStreamHandler:
    """Attach once-per-process; idempotent on re-invocation."""
    headless_logger = logging.getLogger("stackwiz.headless")
    for h in headless_logger.handlers:
        if isinstance(h, _HeadlessStreamHandler):
            return h
    handler = _HeadlessStreamHandler()
    handler.setFormatter(logging.Formatter("[auto] %(message)s"))
    headless_logger.addHandler(handler)
    # Prevent the stream message from propagating twice through the file
    # handler attached to `stackwiz` root — the file handler also picks up
    # records that propagate up, so it already gets a copy. propagate=True
    # keeps install.log forensics; the stream handler we just attached is
    # the extra terminal-output path.
    return handler


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

    config_values, effective_domain = _load_config_values(manifest, state, config_env_file)
    log.info("effective domain: %s", effective_domain)

    consul_probe = await probe_consul(effective_domain, manifest.consul_host)
    vault_probe = await probe_vault(effective_domain, manifest.vault_host)

    consul_client, vault_client = build_backends(
        state_dir, consul_probe, vault_probe,
        service_prefix=manifest.consul.service_prefix,
    )
    if vault_client is not None:
        log.info("vault: %s (%s)", vault_probe.address, vault_probe.source.value)
    else:
        log.info("vault: not reachable (%s)", vault_probe.detail)
    if consul_client is not None:
        tok = " (ACL token)" if consul_client.token else " (no ACL token)"
        log.info(
            "consul: %s (%s)%s",
            consul_probe.address, consul_probe.source.value, tok,
        )
    else:
        log.info("consul: not reachable (%s)", consul_probe.detail)

    component_ids = {c.id for c in manifest.components}
    if mode == "install":
        required_missing = []
        consul_needed = manifest.consul.required and "consul" not in component_ids
        if not consul_probe.reachable and consul_needed:
            required_missing.append("consul")
        if not vault_probe.reachable and manifest.secrets and "vault" not in component_ids:
            required_missing.append("vault")
        if required_missing:
            log.error(
                "required backends not reachable and not in manifest: %s",
                ", ".join(required_missing),
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
        log.info("selected: %s", ", ".join(sorted(selected)))
        if forced_refresh:
            log.info("forced refresh: %s", ", ".join(sorted(forced_refresh)))
        log.info("config: %s", config_values)
        log.info("starting install")
        try:
            failed = await _drive(engine.install(selected, config_values, forced_refresh))
        except RuntimeError as exc:
            # Typically raised by materialize_secrets when a user-supplied
            # secret is missing from both Vault and `.stackwiz.secrets.env`.
            log.error("%s", exc)
            return 2
    else:
        if selected_override is not None:
            selected = set(selected_override)
            installed_ids = set(state.installed().keys())
            orphans = selected - installed_ids
            if orphans:
                log.info("note: not installed: %s", ", ".join(sorted(orphans)))
            selected = selected & installed_ids
        else:
            selected = set(state.installed().keys())
        log.info("uninstalling: %s", ", ".join(sorted(selected)))
        failed = await _drive(engine.uninstall(selected))

    if failed:
        log.error("FAILED")
        return 1
    log.info("OK")
    if engine.last_run is not None and engine.last_run.secrets:
        log.info("secrets:")
        for sid, secret_info in engine.last_run.secrets.items():
            marker = "new" if secret_info.regenerated else "existing"
            log.info("  %s: %s (%s)", sid, secret_info.vault_path, marker)
    return 0


async def _drive(iterator) -> bool:
    """Consume an engine async iterator, log events, return True on failure."""
    failed = False
    current: str | None = None
    async for event in iterator:
        assert isinstance(event, StepEvent)
        if event.line is not None:
            prefix = "ERR " if event.stream == "stderr" else "    "
            log.info("%s[%s] %s", prefix, event.component_id, event.line)
            continue
        if event.component_id != current:
            current = event.component_id
            suffix = f" -- {event.message}" if event.message else ""
            log.info(
                "[%s] %s: %s%s",
                event.component_id, event.status.value, event.action.value, suffix,
            )
        elif event.status is Status.DONE:
            log.info("[%s] done", event.component_id)
        elif event.status is Status.FAILED:
            failed = True
            log.error(
                "[%s] FAILED (exit %s)",
                event.component_id, event.exit_code,
            )
        elif event.status is Status.SKIPPED:
            log.info("[%s] skipped: %s", event.component_id, event.message)
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
    _attach_stream_handler()
    return asyncio.run(
        _run(
            manifest, state_dir, manifest_dir, mode, config_env_file,
            selected_override, forced_refresh,
        )
    )
