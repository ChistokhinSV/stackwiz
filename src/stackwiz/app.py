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
from stackwiz.tokens import build_backends
from stackwiz.vault_client import VaultClient

Mode = Literal["install", "uninstall"]


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
        """Instantiate Consul + Vault clients from the welcome-screen probes."""
        if self.consul_probe is None or self.vault_probe is None:
            return
        self.consul_client, self.vault_client = build_backends(
            self.state_dir,
            self.consul_probe,
            self.vault_probe,
            ensure_kv_mount=True,
            service_prefix=self.manifest.consul.service_prefix,
        )
