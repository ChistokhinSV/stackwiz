"""Install/upgrade/uninstall orchestrator.

Consumes a validated Manifest + State + backends and emits StepEvents to a
subscriber (typically the progress screen). The engine is deliberately
TUI-agnostic: screens subscribe via an asyncio.Queue.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stackwiz.pipeline import ComponentStep

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
        self._node_ip = self._resolve_node_ip(config_values)
        actions = self.state.plan_actions(
            self.manifest, selected_ids, config_values, forced_refresh
        )
        # Catchup passes heal stacks originally installed before the engine
        # could resolve a Consul ACL token or apply a Vault policy. Both
        # are idempotent so re-asserting on every run is cheap.
        self._catchup_registrations()
        self._catchup_service_policies()
        # Materialize secrets up front when Vault is already there; the
        # vault-install step (if any) will refresh via _adopt_vault_after_install.
        materialized: dict[str, MaterializedSecret] = {}
        if self.vault is not None:
            # ensure_kv_mount is idempotent — re-asserting on every install
            # costs nothing on a fully-provisioned Vault and fixes the case
            # where an operator brought Vault up manually (or a previous
            # install failed between init and mount creation), leaving it
            # reachable but without the `stackwiz` KV v2 mount.
            try:
                self.vault.ensure_kv_mount()
            except Exception as exc:  # noqa: BLE001
                log.warning("vault kv mount ensure failed: %s", exc)
            # SCR-173: seed the shared kb-sync keypair BEFORE any
            # install script runs so kb-publish satellites never race
            # the central kb-agent that historically owned generation.
            try:
                self.vault.ensure_shared_kb_sync_keypair()
            except Exception as exc:  # noqa: BLE001
                log.warning("kb-sync identity seed failed: %s", exc)
            self._upload_user_secrets()
            materialized = materialize_secrets(self.manifest, self.vault)
        result = EngineResult(secrets=materialized)
        self.last_run = result

        steps = self._build_install_steps(
            actions, config_values, materialized, result,
        )
        from stackwiz.pipeline import run_pipeline
        async for event in run_pipeline(self, steps):
            yield event
            if event.status is Status.FAILED:
                result.failed.append(event.component_id)
                return
            if event.status is Status.DONE:
                result.succeeded.append(event.component_id)
            elif event.status is Status.SKIPPED:
                result.skipped.append(event.component_id)

        log.info(
            "install finished: %d ok, %d failed, %d skipped",
            len(result.succeeded), len(result.failed), len(result.skipped),
        )
        self._write_summary_md_if_ok(result)

    # --- uninstall --------------------------------------------------------------

    async def uninstall(self, selected_ids: set[str]) -> AsyncIterator[StepEvent]:
        actions = self.state.plan_uninstall(self.manifest, selected_ids)
        steps = self._build_uninstall_steps(actions)
        from stackwiz.pipeline import run_pipeline
        async for event in run_pipeline(self, steps):
            yield event
            if event.status is Status.FAILED:
                return

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

    # --- step-list builders -----------------------------------------------------

    def _build_install_steps(
        self,
        actions: dict[str, Action],
        config_values: dict[str, Any],
        materialized: dict[str, MaterializedSecret],
        result: EngineResult,
    ) -> list[ComponentStep]:
        """Compose one ComponentStep per component the plan touches.

        Backend-adoption hooks attach to the specific consul/vault steps as
        ``post_execute`` callbacks — not ``if component.id == ...`` branches
        sprinkled through the install loop.
        """
        from stackwiz.pipeline import ComponentStep

        # Mutable box so _adopt_vault_after_install can swap the materialized
        # dict when Vault first becomes available mid-install.
        mat_box: list[dict[str, MaterializedSecret]] = [materialized]
        steps: list[ComponentStep] = []
        for component in self.manifest.topo_order():
            action = actions[component.id]
            if action is Action.NOOP:
                steps.append(ComponentStep(
                    component=component, action=action,
                    skip=True, skip_message="up to date",
                ))
                continue

            # Prepare closure: mint the install token and build env with the
            # current materialized map (may have been refreshed by the vault
            # step earlier in this run).
            def _prepare(c=component, a=action) -> tuple[dict[str, str], str | None]:
                log.info("%s: %s", c.id, a.value)
                token = self._mint_install_token(c)
                env = self._component_env(
                    c, config_values, mat_box[0], a, vault_token=token,
                )
                return env, token

            # Lazy adoption: only runs for the consul / vault component ids.
            post_execute: Callable[[], Any] | None = None
            if component.id == "consul":
                async def _adopt_consul(c=component) -> None:
                    if self.consul is None:
                        await self._adopt_consul_after_install(c)
                post_execute = _adopt_consul
            elif component.id == "vault":
                async def _adopt_vault() -> None:
                    if self.vault is None:
                        mat_box[0] = await self._adopt_vault_after_install(
                            result, mat_box[0],
                        )
                post_execute = _adopt_vault

            def _persist(c=component) -> None:
                cfg_hash = component_config_hash(c, config_values)
                self.state.mark_installed(c, cfg_hash)

            steps.append(ComponentStep(
                component=component,
                action=action,
                script=self._script_for_action(component, action),
                prepare=_prepare,
                post_execute=post_execute,
                post_publish=lambda c=component: self._post_component_publish(c, config_values),
                persist=_persist,
            ))
        return steps

    def _build_uninstall_steps(
        self,
        actions: dict[str, Action],
    ) -> list[ComponentStep]:
        """Compose one ComponentStep per component in reverse topo order.

        Uninstall has no prepare (no scoped tokens) and no post_execute
        (nothing to adopt); its post_publish does deregister + policy revoke.
        """
        from stackwiz.pipeline import ComponentStep

        steps: list[ComponentStep] = []
        for component in reversed(self.manifest.topo_order()):
            action = actions.get(component.id, Action.NOOP)
            if action is Action.NOOP:
                steps.append(ComponentStep(
                    component=component, action=action,
                    skip=True, skip_message="not installed",
                ))
                continue

            def _cleanup_backends(c=component) -> None:
                log.info("%s: uninstall", c.id)
                if self.consul is not None:
                    try:
                        self.consul.deregister_service(c)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("%s: consul deregister failed: %s", c.id, exc)
                if self.vault is not None:
                    prefix = self.manifest.consul.service_prefix
                    try:
                        self.vault.revoke_service_policy(prefix, c.id)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("%s: vault policy revoke failed: %s", c.id, exc)
                    try:
                        self.vault.revoke_install_policy(prefix, c.id)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("%s: vault install policy revoke failed: %s", c.id, exc)

            steps.append(ComponentStep(
                component=component,
                action=action,
                script=component.uninstall,  # may be None → pipeline skips script phase
                post_publish=_cleanup_backends,
                persist=lambda c=component: self.state.mark_uninstalled(c.id),
                done_message="removed",
            ))
        return steps

    # --- helpers ----------------------------------------------------------------

    async def _run_script(
        self,
        component: Component,
        action: Action,
        script: Path,
        env: dict[str, str],
    ) -> AsyncIterator[StepEvent]:
        # Dedicated per-component logger so log lines carry the component id
        # without string formatting on every line. Forwards to install.log via
        # log_module.configure() (called from both app.py and headless.py).
        script_log = logging.getLogger(f"stackwiz.script.{component.id}")
        script_log.info("-> running %s (%s)", script, action.value)

        try:
            async for stream, text in self.executor.run(script, env):
                if stream == "exit":
                    code = int(text)
                    if code == 0:
                        script_log.info("<- exit 0 (ok)")
                        yield StepEvent(
                            component.id, Status.RUNNING, action,
                            message="script ok", exit_code=0,
                        )
                    else:
                        script_log.error("<- exit %d (FAILED)", code)
                        yield StepEvent(
                            component.id, Status.FAILED, action,
                            message=f"exit {code}", exit_code=code,
                        )
                    return
                if stream == "stderr":
                    script_log.warning("%s", text)
                else:
                    script_log.info("%s", text)
                yield StepEvent(
                    component.id, Status.RUNNING, action,
                    line=text, stream=stream,
                )
        except asyncio.CancelledError:
            # Consumer cancelled (e.g. TUI quit) — let executor.run()'s own
            # cancellation handling terminate the subprocess and re-raise.
            raise
        except Exception as exc:  # noqa: BLE001
            script_log.exception("script aborted: %s", exc)
            yield StepEvent(
                component.id, Status.FAILED, action,
                message=f"script error: {exc}", exit_code=-1,
            )

    def _mint_install_token(self, component: Component) -> str | None:
        """Mint a scoped child token for the component's install script.

        Returns None when the engine should fall back to its own token, which
        happens for:
          * components with no Vault backend yet (bootstrapping Vault itself)
          * the `vault` component — it writes the root policy catalog
          * any error minting (logged at warning level)

        The caller MUST revoke the returned token once the script finishes.
        """
        if self.vault is None or not self.vault.token:
            return None
        if component.id == "vault":
            # Vault install bootstraps everything else; child tokens aren't
            # available before init completes, and the script legitimately
            # needs root.
            return None
        prefix = self.manifest.consul.service_prefix
        try:
            policy = self.vault.create_install_policy(prefix, component.id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "%s: install policy create failed, falling back to parent token: %s",
                component.id, exc,
            )
            return None
        token = self.vault.create_child_token(
            policies=[policy],
            ttl="2h",
            display_name=f"stackwiz-{component.id}",
        )
        if token is None:
            log.warning(
                "%s: child token mint failed, falling back to parent token",
                component.id,
            )
        return token

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
        vault_token: str | None = None,
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
            # Re-probe on demand: a component earlier in this run may have
            # registered the service, so querying the catalog NOW (not at
            # engine startup) is what makes same-run discovery work.
            for discover in component.consul_discover:
                entry = self.consul.discover(discover.service)
                if entry is not None:
                    env[discover.env_var] = f"{entry.address}:{entry.port}"
                elif discover.required:
                    raise RuntimeError(
                        f"component {component.id!r}: consul_discover target "
                        f"{discover.service!r} is not registered in the catalog "
                        f"(needed for env var {discover.env_var}). Register it "
                        f"before this component runs, or mark the dependency "
                        f"with `required: false`."
                    )

        env["VAULT_ADDR"] = self.vault.address if self.vault else ""
        if self.vault is not None:
            token = vault_token or self.vault.token
            if token:
                env["VAULT_TOKEN"] = token
        if self.consul is not None:
            env["CONSUL_HTTP_ADDR"] = self.consul.address
            if self.consul.token:
                env["CONSUL_HTTP_TOKEN"] = self.consul.token
        env["STACKWIZ_STATE_DIR"] = self.state.host_path()
        # Host-side manifest dir — so nsenter scripts can find repo files
        # (the container's /manifest mount isn't visible from host PID 1).
        host_manifest = os.environ.get("STACKWIZ_HOST_MANIFEST_DIR", "")
        if host_manifest:
            env["WIZ_MANIFEST_DIR"] = host_manifest
        return env

    # --- extracted install() helpers -------------------------------------------

    def _resolve_node_ip(self, config_values: dict[str, Any]) -> str:
        """Pick the node address for Consul service registration.

        Checks ``node_ip`` first, then any ``*_internal_ip`` field, falling
        back to ``127.0.0.1``. The first non-empty match wins.
        """
        return str(
            config_values.get("node_ip")
            or next(
                (v for k, v in config_values.items()
                 if k.endswith("_internal_ip") and v),
                "127.0.0.1",
            )
        )

    def _register_component_services(
        self, component: Component, force: bool = False,
    ) -> None:
        """Register all of a component's Consul services.

        When ``force=False`` (default, catchup path): probe via ``discover()``
        first and skip services already in the catalog — avoids re-registering
        every NOOP component on every run.

        When ``force=True`` (post-publish path): always re-register, even when
        the service already exists. Consul's agent register is idempotent BUT
        it is also the only way a check-config change (tls_skip_verify,
        interval, health URL) reaches the agent. Without force, operators
        running ``wizinstall run --force`` to pick up a manifest edit would
        see the edit fall on deaf ears for services already registered.

        All errors are warn-logged, never raised — Consul registration is
        best-effort forensic metadata, not a gate.
        """
        if self.consul is None:
            return
        for svc in component.all_consul_services():
            if not force:
                try:
                    if self.consul.discover(svc) is not None:
                        continue
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "%s: discover %s failed: %s",
                        component.id, svc.name, exc,
                    )
                    continue
            else:
                # Consul's agent keeps the existing check goroutine running
                # across a plain re-register of the same service id — the
                # registration is replaced but the check config isn't. A
                # deregister first kills the old goroutine so the fresh
                # register actually applies any check-config changes
                # (tls_skip_verify, interval, URL). Best-effort; if the
                # service wasn't there we don't care.
                try:
                    self.consul.deregister_service(component, svc)
                except Exception as exc:  # noqa: BLE001
                    log.debug(
                        "%s: pre-register deregister %s: %s",
                        component.id, svc.name, exc,
                    )
            try:
                self.consul.register_service(
                    component, svc, node_address=self._node_ip,
                )
                log.info(
                    "%s: registered in consul as %s", component.id, svc.name,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "%s: consul register %s failed: %s",
                    component.id, svc.name, exc,
                )

    def _catchup_registrations(self) -> None:
        """Re-register installed components whose services are missing."""
        if self.consul is None:
            return
        installed = self.state.installed()
        for component in self.manifest.topo_order():
            if component.id in installed:
                self._register_component_services(component)

    def _catchup_service_policies(self) -> None:
        """Idempotently re-apply per-component runtime policies in Vault.

        Vault's `create_or_update_policy` is idempotent, so re-asserting on
        every run is cheap. Covers the failure mode where the original
        install's `apply_service_policy` warn-logged an error but the
        component was marked installed anyway, and the next run NOOP'd it.
        """
        if self.vault is None:
            return
        prefix = self.manifest.consul.service_prefix
        installed = self.state.installed()
        for component in self.manifest.topo_order():
            if component.id not in installed:
                continue
            if not component.all_consul_services():
                continue
            try:
                self.vault.apply_service_policy(prefix, component.id)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "%s: policy re-apply failed: %s", component.id, exc,
                )

    async def _adopt_consul_after_install(self, current: Component) -> None:
        """Probe for Consul after its own install, adopt the client, and
        retroactively register services for components that ran before it."""
        probe = await probe_consul(self.manifest.domain, self.manifest.consul_host)
        if not (probe.reachable and probe.address):
            return
        token_file = self.state.state_dir / "consul-http-token"
        token = token_file.read_text().strip() if token_file.exists() else None
        self.consul = ConsulClient(probe.address, token=token)
        log.info("consul adopted after install: %s", probe.address)
        # Register services for components that ran BEFORE consul existed
        # (e.g. 081 installs vault first, then consul).
        for earlier in self.manifest.topo_order():
            if earlier.id == current.id:
                break
            self._register_component_services(earlier)

    async def _adopt_vault_after_install(
        self,
        result: EngineResult,
        materialized: dict[str, MaterializedSecret],
    ) -> dict[str, MaterializedSecret]:
        """Probe for Vault after its install, adopt the client, and
        materialize secrets if they weren't already. Returns the possibly
        refreshed ``materialized`` map.

        Two-stage probe:

        1. Honor the operator's TLS-verify policy first (STACKWIZ_VAULT_VERIFY
           / VAULT_CACERT) — picks up an externally-configured Vault cleanly.
        2. On failure, if the install script just persisted ``vault-token``
           to state, retry with ``verify=False``. Rationale: the script we
           just ran generated a self-signed cert and wrote the root token to
           disk; we know this is OUR Vault, trust-on-first-use is correct.

        Loud warn-log on every failure path so the operator can diagnose if
        downstream components later fail due to missing materialized secrets.
        """
        probe = await probe_vault(
            self.manifest.domain, self.manifest.vault_host,
        )
        token_file = self.state.state_dir / "vault-token"
        just_installed = token_file.exists()
        if not (probe.reachable and probe.address) and just_installed:
            log.warning(
                "vault probe failed under current TLS policy (%s); the "
                "install script just persisted vault-token so retrying with "
                "verify=False (trust-on-first-use for freshly-issued "
                "self-signed cert)", probe.detail,
            )
            probe = await probe_vault(
                self.manifest.domain, self.manifest.vault_host,
                verify_override=False,
            )
        if not (probe.reachable and probe.address):
            log.warning(
                "vault adoption: probe failed (%s). Downstream components "
                "needing stackwiz-generated secrets will fail. Check that "
                "the install script brought vault up and the engine can "
                "reach it at vault.%s or 127.0.0.1:8200.",
                probe.detail, self.manifest.domain,
            )
            return materialized
        token = token_file.read_text().strip() if just_installed else None
        # If we retried with verify=False, build the client the same way so
        # subsequent hvac calls don't fail on the same self-signed cert.
        vault_verify: bool | str | None = (
            False if just_installed else None
        )
        self.vault = VaultClient(
            probe.address, token=token, verify=vault_verify,
        )
        log.info("vault adopted after install: %s", probe.address)
        try:
            self.vault.ensure_kv_mount()
        except Exception as exc:  # noqa: BLE001
            log.warning("vault kv mount failed: %s", exc)
        # SCR-173: same shared-identity seed as the upfront path — on a
        # fresh-Vault install this is the FIRST chance to populate it,
        # and any component that triggers kb-publish later in this run
        # must find it in place.
        try:
            self.vault.ensure_shared_kb_sync_keypair()
        except Exception as exc:  # noqa: BLE001
            log.warning("kb-sync identity seed failed: %s", exc)
        if materialized:
            return materialized
        try:
            self._upload_user_secrets()
            fresh = materialize_secrets(self.manifest, self.vault)
            result.secrets = fresh
            log.info("materialized %d secrets", len(fresh))
            return fresh
        except Exception as exc:  # noqa: BLE001
            log.warning("secret materialization failed: %s", exc)
            return materialized

    def _post_component_publish(
        self, component: Component, config_values: dict[str, Any],
    ) -> None:
        """After a component's install script succeeds, publish its Consul
        services, KV config, and apply the runtime read-only Vault policy."""
        prefix = self.manifest.consul.service_prefix
        if self.consul is not None:
            # force=True so a manifest check-config edit (e.g.
            # tls_skip_verify) lands on the next install, not the next
            # fresh-deploy.
            self._register_component_services(component, force=True)
            for key, value in self._kv_payload(config_values, component).items():
                try:
                    self.consul.kv_put(f"{prefix}/config/{key}", str(value))
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "%s: consul KV put %s failed: %s",
                        component.id, key, exc,
                    )
        if self.vault is not None and component.all_consul_services():
            try:
                self.vault.apply_service_policy(prefix, component.id)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: vault policy failed: %s", component.id, exc)

    def _write_summary_md_if_ok(self, result: EngineResult) -> None:
        """Write state_dir/summary.md when every selected component succeeded."""
        if result.failed:
            return
        try:
            from stackwiz.info import write_summary_md
            write_summary_md(
                self.manifest, self.state, self.consul, self.vault,
                manifest_dir=self.executor.manifest_dir,
            )
            log.info("summary written to %s", self.state.host_path("summary.md"))
        except Exception as exc:  # noqa: BLE001
            log.warning("summary.md write failed: %s", exc)

    def _kv_payload(
        self, config_values: dict[str, Any], component: Component
    ) -> dict[str, Any]:
        """Config subset that THIS component publishes to Consul KV.

        A component's ``publishes:`` list enumerates which keys other
        components / external consumers need to discover at runtime. Empty
        (the default) means the component publishes nothing — state.yaml
        remains the single source of truth for local config, and Consul KV
        only carries cross-component contract data.
        """
        if not component.publishes:
            return {}
        return {k: config_values[k] for k in component.publishes if k in config_values}

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
            log.info("uploaded user-supplied secret %s -> %s", sid, vault_path)
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
