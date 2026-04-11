"""Welcome screen: host info, Consul + Vault discovery, proceed gate."""
from __future__ import annotations

import logging
import platform
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Label, Static

from stackwiz.discovery import ProbeResult, Source, probe_consul, probe_vault

if TYPE_CHECKING:
    from stackwiz.app import InstallerApp

log = logging.getLogger("stackwiz.welcome")


class WelcomeScreen(Screen):
    BINDINGS = [("q", "app.quit", "Quit"), ("n", "proceed", "Next")]

    def __init__(self) -> None:
        super().__init__()
        self.consul_result: ProbeResult | None = None
        self.vault_result: ProbeResult | None = None

    @property
    def installer(self) -> InstallerApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main"):
            yield Label(f"[b]{self.installer.manifest.display_name}[/b] "
                        f"v{self.installer.manifest.version}", id="title")
            yield Static(f"Mode: [cyan]{self.installer.mode}[/cyan]")
            yield Static(f"Domain: [cyan]{self.installer.effective_domain}[/cyan]")
            yield Static(
                f"Host: {platform.system()} {platform.release()} "
                f"({platform.machine()})"
            )
            yield Static("")
            yield Label("[b]Service discovery[/b]")
            yield Static("Probing Consul...", id="consul-line")
            yield Static("Probing Vault...", id="vault-line")
            yield Static("")
            yield Static("", id="proceed-hint")
        with Horizontal(id="button-bar"):
            yield Button("Next", id="next", variant="primary", disabled=True)
            yield Button("Quit", id="quit", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self.run_probes()

    @work(exclusive=True)
    async def run_probes(self) -> None:
        manifest = self.installer.manifest
        # Use the effective domain (manifest default merged with overrides from
        # .stackwiz.env and prior state.config()) so operator changes take
        # effect BEFORE the welcome screen probes.
        domain = self.installer.effective_domain
        self.consul_result = await probe_consul(domain, manifest.consul_host)
        self.query_one("#consul-line", Static).update(_format_probe("Consul", self.consul_result))
        self.vault_result = await probe_vault(domain, manifest.vault_host)
        self.query_one("#vault-line", Static).update(_format_probe("Vault", self.vault_result))

        missing = []
        if not self.consul_result.reachable:
            missing.append("Consul")
        if not self.vault_result.reachable:
            missing.append("Vault")

        hint = self.query_one("#proceed-hint", Static)
        next_btn = self.query_one("#next", Button)

        if missing:
            component_ids = {c.id for c in manifest.components}
            can_bootstrap = all(
                name.lower() in component_ids for name in missing
            )
            if can_bootstrap:
                hint.update(
                    f"[yellow]{', '.join(missing)} not reachable; will be installed "
                    f"as components in this run.[/yellow]"
                )
                next_btn.disabled = False
            else:
                hint.update(
                    f"[red]{', '.join(missing)} not reachable and not in manifest. "
                    f"Set env / DNS to point at reachable instances, then re-run.[/red]"
                )
                next_btn.disabled = True
        else:
            hint.update("[green]All backends reachable. Press Next to continue.[/green]")
            next_btn.disabled = False

        self.installer.consul_probe = self.consul_result
        self.installer.vault_probe = self.vault_result

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "next":
            self.action_proceed()
        elif event.button.id == "quit":
            self.app.exit()

    def action_proceed(self) -> None:
        btn = self.query_one("#next", Button)
        if btn.disabled:
            return
        self.installer.build_clients_from_probes()

        # If the CLI already locked a selection (`wizinstall run consul vault`),
        # skip the components screen entirely and jump to config (install) or
        # progress (uninstall).
        if self.installer.selection_locked:
            if self.installer.mode == "uninstall" or not self.installer.manifest.config:
                from stackwiz.screens.progress import ProgressScreen
                self.app.push_screen(ProgressScreen())
            else:
                from stackwiz.screens.config import ConfigScreen
                self.app.push_screen(ConfigScreen())
            return

        from stackwiz.screens.components import ComponentsScreen
        self.app.push_screen(ComponentsScreen())


def _format_probe(name: str, result: ProbeResult) -> str:
    if result.source is Source.MISSING:
        return f"[red]✗[/red] {name}: not reachable ({result.detail})"
    return f"[green]✓[/green] {name}: {result.address} [dim]({result.source.value})[/dim]"
