"""Textual App shell and screen routing."""
from __future__ import annotations

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


class InstallerApp(App[int]):
    """Top-level Textual application."""

    TITLE = "stackwiz"
    SUB_TITLE = "modular installer"
    CSS = """
    Screen { align: center top; }
    #welcome-box, #components-box, #config-box, #summary-box { padding: 1 2; width: 100%; }
    #progress-table { height: 40%; min-height: 8; }
    #progress-log { height: 1fr; min-height: 6; background: $surface; }
    Button { margin: 1 1; }
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

        log_module.configure(state_dir)

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
        if self.consul_probe and self.consul_probe.reachable and self.consul_probe.address:
            self.consul_client = ConsulClient(self.consul_probe.address)
        if self.vault_probe and self.vault_probe.reachable and self.vault_probe.address:
            token_file = self.state_dir / "vault-token"
            token = token_file.read_text().strip() if token_file.exists() else None
            self.vault_client = VaultClient(self.vault_probe.address, token=token)
            # Re-runs need the KV mount to exist (first run enables it lazily
            # after vault installs; subsequent runs should re-ensure idempotently).
            try:
                self.vault_client.ensure_kv_mount()
            except Exception:  # noqa: BLE001 — non-fatal; engine will log if it later fails
                pass
