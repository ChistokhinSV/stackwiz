"""Install/upgrade/uninstall orchestrator.

Consumes a validated Manifest + State + backends and emits StepEvents to a
subscriber (typically the progress screen). The engine is deliberately
TUI-agnostic: screens subscribe via an asyncio.Queue.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from stackwiz.consul_client import ConsulClient
from stackwiz.discovery import probe_consul, probe_vault
from stackwiz.executor import Executor
from stackwiz.manifest import Component, Manifest
from stackwiz.secrets import MaterializedSecret, delete_secrets, materialize_secrets
from stackwiz.state import Action, State, component_config_hash
from stackwiz.vault_client import VaultClient

log = logging.getLogger("stackwiz.engine")


class Status(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StepEvent:
    component_id: str
    status: Status
    action: Action
    line: str | None = None         # stream text (stdout/stderr)
    stream: str | None = None       # "stdout" | "stderr" | None
    exit_code: int | None = None
    message: str | None = None      # status message for the UI


@dataclass
class EngineResult:
    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    secrets: dict[str, MaterializedSecret] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


class Engine:
    def __init__(
        self,
        manifest: Manifest,
        state: State,
        executor: Executor,
        consul: ConsulClient | None = None,
        vault: VaultClient | None = None,
    ) -> None:
        self.manifest = manifest
        self.state = state
        self.executor = executor
        self.consul = consul
        self.vault = vault
        self.last_run: EngineResult | None = None

    # --- install / upgrade / reconfigure ----------------------------------------

    async def install(
        self,
        selected_ids: set[str],
        config_values: dict[str, Any],
    ) -> AsyncIterator[StepEvent]:
        """Run the install plan. Yields StepEvents in real time.

        Backends are built lazily: if Vault/Consul aren't reachable at start
        but are installed as manifest components, the engine re-probes and
        builds clients after each such component completes. Secrets are
        materialized the moment Vault becomes available.
        """
        self.state.save_config(config_values)
        actions = self.state.plan_actions(self.manifest, selected_ids, config_values)
        prefix = self.manifest.consul.service_prefix
        materialized: dict[str, MaterializedSecret] = {}
        if self.vault is not None:
            materialized = materialize_secrets(self.manifest, self.vault)
        result = EngineResult(secrets=materialized)
        self.last_run = result

        for component in self.manifest.topo_order():
            action = actions[component.id]
            if action is Action.NOOP:
                yield StepEvent(component.id, Status.SKIPPED, action, message="up to date")
                result.skipped.append(component.id)
                continue

            yield StepEvent(component.id, Status.RUNNING, action, message=action.value)
            log.info("%s: %s", component.id, action.value)

            env = self._component_env(component, config_values, materialized, action)
            script = self._script_for_action(component, action)

            async for event in self._run_script(component, action, script, env):
                yield event
                if event.status is Status.FAILED:
                    result.failed.append(component.id)
                    return

            # Lazy backend bootstrap: after consul/vault installs, probe + adopt.
            if component.id == "consul" and self.consul is None:
                probe = await probe_consul(self.manifest.domain, self.manifest.consul_host)
                if probe.reachable and probe.address:
                    self.consul = ConsulClient(probe.address)
                    log.info("consul adopted after install: %s", probe.address)
            if component.id == "vault" and self.vault is None:
                probe = await probe_vault(self.manifest.domain, self.manifest.vault_host)
                if probe.reachable and probe.address:
                    token_file = self.state.state_dir / "vault-token"
                    token = token_file.read_text().strip() if token_file.exists() else None
                    self.vault = VaultClient(probe.address, token=token)
                    log.info("vault adopted after install: %s", probe.address)
                    try:
                        self.vault.ensure_kv_mount()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("vault kv mount failed: %s", exc)
                    if not materialized:
                        try:
                            materialized = materialize_secrets(self.manifest, self.vault)
                            result.secrets = materialized
                            log.info("materialized %d secrets", len(materialized))
                        except Exception as exc:  # noqa: BLE001
                            log.warning("secret materialization failed: %s", exc)

            if self.consul is not None and component.consul_service is not None:
                try:
                    self.consul.register_service(component)
                    log.info("%s: registered in consul as %s",
                             component.id, component.consul_service.name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s: consul register failed: %s", component.id, exc)

            if self.consul is not None:
                for key, value in self._kv_payload(config_values, component).items():
                    try:
                        self.consul.kv_put(f"{prefix}/config/{key}", str(value))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("%s: consul KV put %s failed: %s", component.id, key, exc)

            if self.vault is not None and component.consul_service is not None:
                try:
                    self.vault.apply_service_policy(prefix, component.id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s: vault policy failed: %s", component.id, exc)

            cfg_hash = component_config_hash(component, config_values)
            self.state.mark_installed(component, cfg_hash)
            yield StepEvent(component.id, Status.DONE, action, message="done")
            result.succeeded.append(component.id)

        log.info(
            "install finished: %d ok, %d failed, %d skipped",
            len(result.succeeded), len(result.failed), len(result.skipped),
        )

    # --- uninstall --------------------------------------------------------------

    async def uninstall(self, selected_ids: set[str]) -> AsyncIterator[StepEvent]:
        actions = self.state.plan_uninstall(self.manifest, selected_ids)
        order = list(reversed(self.manifest.topo_order()))

        for component in order:
            action = actions.get(component.id, Action.NOOP)
            if action is Action.NOOP:
                yield StepEvent(component.id, Status.SKIPPED, action, message="not installed")
                continue

            yield StepEvent(component.id, Status.RUNNING, action, message="uninstall")
            log.info("%s: uninstall", component.id)

            if component.uninstall is not None:
                async for event in self._run_script(
                    component, action, component.uninstall, {}
                ):
                    yield event
                    if event.status is Status.FAILED:
                        return

            if self.consul is not None:
                try:
                    self.consul.deregister_service(component)
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s: consul deregister failed: %s", component.id, exc)

            if self.vault is not None:
                try:
                    self.vault.revoke_service_policy(
                        self.manifest.consul.service_prefix, component.id
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s: vault policy revoke failed: %s", component.id, exc)

            self.state.mark_uninstalled(component.id)
            yield StepEvent(component.id, Status.DONE, action, message="removed")

        # Best-effort cleanup of leftover data. When consul/vault are themselves
        # being uninstalled they're already gone by this point — their own
        # storage teardown handled the data — so reaching the backends is
        # expected to fail.
        full_sweep = all(
            actions.get(c.id) == Action.UNINSTALL for c in self.manifest.components
        )
        if full_sweep and self.vault is not None:
            try:
                delete_secrets(self.manifest, self.vault)
            except Exception as exc:  # noqa: BLE001
                log.info("vault cleanup skipped (backend already down): %s", exc)
        if full_sweep and self.consul is not None:
            try:
                self.consul.kv_delete_tree(f"{self.manifest.consul.service_prefix}/config/")
            except Exception as exc:  # noqa: BLE001
                log.info("consul KV cleanup skipped (backend already down): %s", exc)

    # --- helpers ----------------------------------------------------------------

    async def _run_script(
        self,
        component: Component,
        action: Action,
        script: Path,
        env: dict[str, str],
    ) -> AsyncIterator[StepEvent]:
        queue: asyncio.Queue[StepEvent] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for stream, text in self.executor.run(script, env):
                    if stream == "exit":
                        code = int(text)
                        if code == 0:
                            await queue.put(
                                StepEvent(
                                    component.id, Status.RUNNING, action,
                                    message="script ok", exit_code=0,
                                )
                            )
                        else:
                            await queue.put(
                                StepEvent(
                                    component.id, Status.FAILED, action,
                                    message=f"exit {code}", exit_code=code,
                                )
                            )
                    else:
                        await queue.put(
                            StepEvent(
                                component.id, Status.RUNNING, action,
                                line=text, stream=stream,
                            )
                        )
            finally:
                await queue.put(StepEvent(component.id, Status.RUNNING, action, message="__end__"))

        task = asyncio.create_task(pump())
        try:
            while True:
                event = await queue.get()
                if event.message == "__end__":
                    return
                yield event
                if event.status is Status.FAILED:
                    return
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    def _script_for_action(self, component: Component, action: Action) -> Path:
        if action is Action.UPGRADE and component.upgrade is not None:
            return component.upgrade
        if action is Action.UNINSTALL and component.uninstall is not None:
            return component.uninstall
        return component.install

    def _component_env(
        self,
        component: Component,
        config_values: dict[str, Any],
        materialized: dict[str, MaterializedSecret],
        action: Action,
    ) -> dict[str, str]:
        env: dict[str, str] = {}
        env.update(component.env)

        for key, value in config_values.items():
            env[f"WIZ_CFG_{key.upper()}"] = str(value)

        for secret_id, info in materialized.items():
            env[f"WIZ_SECRET_{secret_id.upper()}"] = info.value
            env[f"WIZ_SECRET_{secret_id.upper()}_PATH"] = info.vault_path

        env["WIZ_COMPONENT_ID"] = component.id
        env["WIZ_COMPONENT_VERSION"] = component.version
        env["WIZ_ACTION"] = action.value
        if action is Action.UPGRADE:
            env["WIZ_UPGRADE"] = "1"
            installed = self.state.get(component.id)
            if installed is not None:
                env["WIZ_OLD_VERSION"] = installed.version
        if action is Action.RECONFIGURE:
            env["WIZ_RECONFIGURE"] = "1"

        if self.consul is not None:
            for discover in component.consul_discover:
                entry = self.consul.discover(discover.service)
                if entry is not None:
                    env[discover.env_var] = f"{entry.address}:{entry.port}"

        env["VAULT_ADDR"] = self.vault.address if self.vault else ""
        if self.consul is not None:
            env["CONSUL_HTTP_ADDR"] = self.consul.address
        host_state = os.environ.get("STACKWIZ_HOST_STATE_DIR")
        env["STACKWIZ_STATE_DIR"] = host_state or str(self.state.state_dir)
        return env

    def _kv_payload(
        self, config_values: dict[str, Any], component: Component
    ) -> dict[str, Any]:
        # v1: mirror all config values into consul KV. Per-component filtering
        # can be added later if the manifest schema grows a `publishes:` field.
        del component
        return dict(config_values)
