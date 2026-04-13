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
from stackwiz.secrets_env import (
    SECRETS_ENV_FILENAME,
    filled_entries,
    load_secrets_env,
    rewrite_after_upload,
    secret_vault_path,
    user_secret_specs,
)
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
        forced_refresh: set[str] | None = None,
    ) -> AsyncIterator[StepEvent]:
        """Run the install plan. Yields StepEvents in real time.

        Backends are built lazily: if Vault/Consul aren't reachable at start
        but are installed as manifest components, the engine re-probes and
        builds clients after each such component completes. Secrets are
        materialized the moment Vault becomes available.

        `forced_refresh` is a set of ids to run with Action.REFRESH regardless
        of the config-hash diff. Used by `wizinstall refresh`.
        """
        self._stage_host_helpers()
        self.state.save_config(config_values)
        actions = self.state.plan_actions(
            self.manifest, selected_ids, config_values, forced_refresh
        )
        prefix = self.manifest.consul.service_prefix
        materialized: dict[str, MaterializedSecret] = {}
        if self.vault is not None:
            self._upload_user_secrets()
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
                    # If consul.sh persisted an HTTP token (because ACLs are
                    # default-deny), read it and pass to the ConsulClient so
                    # the engine can register services for later components.
                    token_file = self.state.state_dir / "consul-http-token"
                    token = token_file.read_text().strip() if token_file.exists() else None
                    self.consul = ConsulClient(probe.address, token=token)
                    log.info("consul adopted after install: %s", probe.address)
                    # Retroactively register services for components that ran
                    # BEFORE consul existed (e.g. 081 installs vault first,
                    # then consul — without this, vault never gets a catalog
                    # entry because the client was None when vault ran).
                    for earlier in self.manifest.topo_order():
                        if earlier.id == component.id:
                            break
                        for svc in earlier.all_consul_services():
                            try:
                                self.consul.register_service(earlier, svc)
                                log.info("%s: retroactively registered in consul as %s",
                                         earlier.id, svc.name)
                            except Exception as exc:  # noqa: BLE001
                                log.warning("%s: retro consul register %s failed: %s",
                                            earlier.id, svc.name, exc)
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
                            self._upload_user_secrets()
                            materialized = materialize_secrets(self.manifest, self.vault)
                            result.secrets = materialized
                            log.info("materialized %d secrets", len(materialized))
                        except Exception as exc:  # noqa: BLE001
                            log.warning("secret materialization failed: %s", exc)

            services = component.all_consul_services()
            if self.consul is not None:
                for svc in services:
                    try:
                        self.consul.register_service(component, svc)
                        log.info("%s: registered in consul as %s",
                                 component.id, svc.name)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("%s: consul register %s failed: %s",
                                    component.id, svc.name, exc)

            if self.consul is not None:
                for key, value in self._kv_payload(config_values, component).items():
                    try:
                        self.consul.kv_put(f"{prefix}/config/{key}", str(value))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("%s: consul KV put %s failed: %s", component.id, key, exc)

            if self.vault is not None and services:
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

        # Write summary.md at end of successful install
        if not result.failed:
            try:
                from stackwiz.info import write_summary_md
                write_summary_md(self.manifest, self.state, self.consul, self.vault)
                log.info("summary written to %s", self.state.host_path("summary.md"))
            except Exception as exc:  # noqa: BLE001
                log.warning("summary.md write failed: %s", exc)

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
        # Dedicated per-component logger so log lines carry the component id
        # without string formatting on every line.
        script_log = logging.getLogger(f"stackwiz.script.{component.id}")
        script_log.info("→ running %s (%s)", script, action.value)

        async def pump() -> None:
            try:
                async for stream, text in self.executor.run(script, env):
                    if stream == "exit":
                        code = int(text)
                        if code == 0:
                            script_log.info("← exit 0 (ok)")
                            await queue.put(
                                StepEvent(
                                    component.id, Status.RUNNING, action,
                                    message="script ok", exit_code=0,
                                )
                            )
                        else:
                            script_log.error("← exit %d (FAILED)", code)
                            await queue.put(
                                StepEvent(
                                    component.id, Status.FAILED, action,
                                    message=f"exit {code}", exit_code=code,
                                )
                            )
                    else:
                        # Forward every stdout/stderr line to install.log at
                        # INFO (stdout) or WARNING (stderr) level. This is the
                        # primary forensic trail — both TUI and headless modes
                        # get it because log_module.configure() is called in
                        # both app.py and headless.py.
                        if stream == "stderr":
                            script_log.warning("%s", text)
                        else:
                            script_log.info("%s", text)
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
            v = str(value).lower() if isinstance(value, bool) else str(value)
            env[f"WIZ_CFG_{key.upper()}"] = v

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
        if action is Action.REFRESH:
            env["WIZ_REFRESH"] = "1"

        if self.consul is not None:
            for discover in component.consul_discover:
                entry = self.consul.discover(discover.service)
                if entry is not None:
                    env[discover.env_var] = f"{entry.address}:{entry.port}"

        env["VAULT_ADDR"] = self.vault.address if self.vault else ""
        if self.consul is not None:
            env["CONSUL_HTTP_ADDR"] = self.consul.address
        env["STACKWIZ_STATE_DIR"] = self.state.host_path()
        # Host-side manifest dir — so nsenter scripts can find repo files
        # (the container's /manifest mount isn't visible from host PID 1).
        host_manifest = os.environ.get("STACKWIZ_HOST_MANIFEST_DIR", "")
        if host_manifest:
            env["WIZ_MANIFEST_DIR"] = host_manifest
        return env

    def _kv_payload(
        self, config_values: dict[str, Any], component: Component
    ) -> dict[str, Any]:
        # v1: mirror all config values into consul KV. Per-component filtering
        # can be added later if the manifest schema grows a `publishes:` field.
        del component
        return dict(config_values)

    def _upload_user_secrets(self) -> None:
        """Push filled `.stackwiz.secrets.env` entries to Vault, strip on success.

        Unknown keys in the file (renamed/deleted manifest secrets) are skipped
        and left in place so the operator can clean them up manually. Empty
        entries stay in the file — `materialize_secrets` will surface them via
        its missing-secret RuntimeError so the operator sees a clear pointer.
        """
        if self.vault is None:
            return
        path = self.executor.manifest_dir / SECRETS_ENV_FILENAME
        values = load_secrets_env(path)
        if not values:
            return
        specs_by_id = {s.id: s for s in user_secret_specs(self.manifest)}
        uploaded: set[str] = set()
        for sid, val in filled_entries(values).items():
            spec = specs_by_id.get(sid)
            if spec is None:
                log.warning(
                    "ignoring unknown key %r in %s (not in manifest.secrets)",
                    sid, SECRETS_ENV_FILENAME,
                )
                continue
            vault_path = secret_vault_path(self.manifest, spec)
            try:
                self.vault.kv_put(vault_path, {"value": val})
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to upload %s to %s: %s", sid, vault_path, exc)
                continue
            uploaded.add(sid)
            log.info("uploaded user-supplied secret %s → %s", sid, vault_path)
        if uploaded:
            rewrite_after_upload(path, self.manifest, uploaded)

    def _stage_host_helpers(self) -> None:
        """Copy bundled `share/` + manifest `templates/` to state_dir for host scripts.

        The state dir is bind-mounted host→container, so anything written here
        is visible to bash scripts running under `nsenter --target 1`. Helpers
        are overwritten on every run so a new image automatically replaces the
        old copies. Three destinations:

          <state_dir>/bin/        — framework share/ (stackwiz-tls.sh etc.)
          <state_dir>/templates/  — consumer repo's templates/ (blueprints, nginx
                                    configs, whatever the consumer ships)
        """
        import shutil
        import stat

        def _copy_dir(src: Path, dst: Path, exec_mode: bool) -> None:
            dst.mkdir(parents=True, exist_ok=True)
            # Copy source files to destination
            src_files: set[str] = set()
            for item in src.rglob("*"):
                rel = item.relative_to(src)
                src_files.add(str(rel))
                target = dst / rel
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                elif item.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(item, target)
                    if exec_mode:
                        try:
                            target.chmod(
                                target.stat().st_mode
                                | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                            )
                        except OSError:
                            pass
            # Remove files in dst that no longer exist in src
            for item in sorted(dst.rglob("*"), reverse=True):
                rel = str(item.relative_to(dst))
                if rel not in src_files:
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir() and not any(item.iterdir()):
                        item.rmdir()

        share_candidates = [
            Path("/usr/local/share/stackwiz"),                 # production
            Path(__file__).parent / "share",                    # dev layout
        ]
        share_src = next((p for p in share_candidates if p.exists()), None)
        if share_src is not None:
            _copy_dir(share_src, self.state.state_dir / "bin", exec_mode=True)
            log.info("staged framework helpers at %s", self.state.host_path("bin"))
        else:
            log.warning("framework share dir not found; helper scripts not staged")

        manifest_templates = self.executor.manifest_dir / "templates"
        if manifest_templates.is_dir():
            _copy_dir(
                manifest_templates,
                self.state.state_dir / "templates",
                exec_mode=False,
            )
            log.info("staged manifest templates at %s", self.state.host_path("templates"))

        # Same for assets/: consumers can ship static files (brand logos,
        # icons, config samples) that install scripts need to reference from
        # the host's mount namespace.
        manifest_assets = self.executor.manifest_dir / "assets"
        if manifest_assets.is_dir():
            _copy_dir(
                manifest_assets,
                self.state.state_dir / "assets",
                exec_mode=False,
            )
            log.info("staged manifest assets at %s", self.state.host_path("assets"))

        # Stage any top-level directories that install scripts need for docker
        # build contexts (e.g. mcp/ for MCP server containers).
        for extra in ("mcp", "remote"):
            extra_dir = self.executor.manifest_dir / extra
            if extra_dir.is_dir():
                _copy_dir(
                    extra_dir,
                    self.state.state_dir / extra,
                    exec_mode=False,
                )
                log.info("staged %s at %s", extra, self.state.host_path(extra))

        # Stage individual top-level files that install scripts reference.
        for fname in ("projects.conf",):
            src = self.executor.manifest_dir / fname
            if src.is_file():
                import shutil
                dst = self.state.state_dir / fname
                shutil.copyfile(src, dst)
                log.info("staged %s at %s", fname, self.state.host_path(fname))
