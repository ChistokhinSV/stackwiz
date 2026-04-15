"""Textual App shell and screen routing."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from textual.app import App

from stackwiz import log as log_module
from stackwiz.config_overrides import effective_config
from stackwiz.consul_client import ConsulClient
from stackwiz.discovery import ProbeResult
from stackwiz.manifest import Manifest
from stackwiz.state import State
from stackwiz.vault_client import VaultClient

Mode = Literal["install", "uninstall"]


def _read_sibling_state_token(state_dir: Path, filename: str) -> str | None:
    """Search sibling consumer state dirs for ``filename``.

    Namespaced state means each consumer owns its own dir
    (``/var/lib/stackwiz/awx-platform/``, ``.../consul-vault-authentik-docker/``
    etc.). Cross-consumer secrets (a consul ACL token written by 081, needed
    by 061) live in one of those siblings. Falling back to a sibling is safe
    — all namespaces on the same host share the same Vault/Consul backend.
    Cross-host installs must provide the token via env or Vault shared path.
    """
    base = state_dir.parent
    if not base.exists():
        return None
    for sibling in sorted(base.iterdir()):
        if sibling == state_dir or not sibling.is_dir():
            continue
        candidate = sibling / filename
        if candidate.exists():
            try:
                value = candidate.read_text().strip()
                if value:
                    return value
            except OSError:
                continue
    return None


def _resolve_consul_token(state_dir: Path, vault: VaultClient | None) -> str | None:
    """Token resolution order: own state > CONSUL_HTTP_TOKEN env > sibling
    state dirs > Vault ``shared/consul_bootstrap_token`` > anonymous."""
    own = state_dir / "consul-http-token"
    if own.exists():
        value = own.read_text().strip()
        if value:
            return value
    env_value = os.environ.get("CONSUL_HTTP_TOKEN", "").strip()
    if env_value:
        return env_value
    sibling = _read_sibling_state_token(state_dir, "consul-http-token")
    if sibling:
        return sibling
    if vault is not None:
        try:
            data = vault.kv_get("shared/consul_bootstrap_token")
            if data and data.get("value"):
                return str(data["value"])
        except Exception:  # noqa: BLE001
            pass
    return None


class InstallerApp(App[int]):
    """Top-level Textual application."""

    TITLE = "stackwiz"
    SUB_TITLE = "modular installer"
    CSS = """
    /* Every screen uses the same layout:
         Header       (dock: top by widget default, 1 row)
         VerticalScroll#main  (height: 1fr — fills remaining space, scrolls)
         Horizontal#button-bar (height: 3 — flex-stacked above Footer)
         Footer       (dock: bottom by widget default, 1 row)

       Header and Footer are auto-docked by their widget classes. Because
       Screen { layout: vertical }, the two non-docked children (#main and
       #button-bar) share the remaining space: #main takes 1fr, #button-bar
       takes exactly 3 rows, and Textual slots them between the docked
       Header and Footer without overlap. Button bar stays visible even
       when the terminal is very small — #main just scrolls. */
    Screen { layout: vertical; }

    VerticalScroll#main {
        height: 1fr;
        padding: 1 2;
        scrollbar-size: 1 1;
    }

    #button-bar {
        height: 3;
        align: center middle;
        background: $panel;
        padding: 0 2;
    }

    /* Buttons sit next to each other, centered as a group. Compose order
       is preserved: cancel/back first, primary Next second. */
    #button-bar Button { margin: 0 1; }

    /* Progress screen overrides — the DataTable + RichLog both need bounded
       heights so the RichLog gets the remaining space without pushing the
       buttons off-screen. */
    #progress-table { height: 40%; min-height: 6; }
    #progress-log { height: 1fr; min-height: 4; background: $surface; }
    """

    def __init__(
        self,
        manifest: Manifest,
        state_dir: Path,
        mode: Mode = "install",
        manifest_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self.manifest = manifest
        self.state_dir = state_dir
        self.manifest_dir: Path = manifest_dir or Path("/manifest")
        self.mode: Mode = mode
        self.sub_title = f"{manifest.display_name} v{manifest.version} ({mode})"

        self.state = State(state_dir)

        self.selected_component_ids: set[str] = set()
        self.selection_locked: bool = False  # True when CLI pre-selected components
        self.forced_refresh: set[str] = set()  # From `run --force` or `refresh` CLI

        # Compute effective config (state > .stackwiz.env > manifest defaults) +
        # effective domain once at startup; welcome probe + config screen both use
        # these so overrides take effect BEFORE the operator sees the TUI.
        env_file = self.manifest_dir / ".stackwiz.env"
        effective_values, effective_domain = effective_config(
            manifest, self.state.config(), env_file,
        )
        self.effective_config: dict[str, object] = dict(effective_values)
        self.effective_domain: str = effective_domain
        self.config_values: dict[str, object] = dict(effective_values)

        self.consul_probe: ProbeResult | None = None
        self.vault_probe: ProbeResult | None = None
        self.consul_client: ConsulClient | None = None
        self.vault_client: VaultClient | None = None

        self.materialized_secrets: dict[str, object] = {}

        log_module.configure(state_dir, mode=f"tui:{mode}")

    def on_mount(self) -> None:
        from stackwiz.screens.welcome import WelcomeScreen

        self.push_screen(WelcomeScreen())

    def build_clients_from_probes(self) -> None:
        """Instantiate Consul + Vault clients from the welcome-screen probes.

        For Vault, we also pick up the persisted token from <state_dir>/vault-token
        when one exists (written by consumers' vault install scripts during the
        initial self-bootstrap run). Without this, re-runs via the TUI fail with
        403 on any Vault operation because `VaultClient(addr)` alone falls back
        to the VAULT_TOKEN env var, which is typically empty on re-runs.
        """
        # Build the Vault client first — it is the fallback source for the
        # Consul ACL token on consumers that don't own consul themselves.
        if self.vault_probe and self.vault_probe.reachable and self.vault_probe.address:
            token_file = self.state_dir / "vault-token"
            token = token_file.read_text().strip() if token_file.exists() else None
            if not token:
                token = _read_sibling_state_token(self.state_dir, "vault-token")
            self.vault_client = VaultClient(self.vault_probe.address, token=token)
            # Re-runs need the KV mount to exist (first run enables it lazily
            # after vault installs; subsequent runs should re-ensure idempotently).
            try:
                self.vault_client.ensure_kv_mount()
            except Exception:  # noqa: BLE001 — non-fatal; engine will log if it later fails
                pass

        if self.consul_probe and self.consul_probe.reachable and self.consul_probe.address:
            consul_token = _resolve_consul_token(self.state_dir, self.vault_client)
            self.consul_client = ConsulClient(
                self.consul_probe.address, token=consul_token
            )
