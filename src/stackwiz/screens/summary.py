"""Final summary screen: installed components, credentials, log path."""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Label, Static

if TYPE_CHECKING:
    from stackwiz.app import InstallerApp


class SummaryScreen(Screen):
    BINDINGS = [("q", "app.quit", "Quit")]

    def __init__(self, failed: bool) -> None:
        super().__init__()
        self.failed = failed

    @property
    def installer(self) -> InstallerApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main"):
            status = (
                "[red]COMPLETED WITH ERRORS[/red]"
                if self.failed
                else "[green]SUCCESS[/green]"
            )
            yield Label(f"[b]{status}[/b]")
            yield Static(f"Manifest: {self.installer.manifest.display_name} "
                         f"v{self.installer.manifest.version}")
            yield Static(f"Log: {self.installer.state_dir}/install.log")
            yield Static("")

            yield Label("[b]Installed components[/b]")
            table = DataTable(id="installed-table", cursor_type="row")
            yield table

            secrets = getattr(self.installer, "materialized_secrets", {})
            if secrets:
                yield Static("")
                yield Label("[b]Secrets[/b] (stored in Vault)")
                for sid, info in secrets.items():
                    marker = (
                        "[yellow]new[/yellow]"
                        if info.regenerated
                        else "[dim]existing[/dim]"
                    )
                    yield Static(
                        f"  {sid}: vault kv get {info.vault_path}  {marker}"
                    )
        with Horizontal(id="button-bar"):
            yield Static("", id="bar-spacer")
            yield Button("Quit", id="quit", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#installed-table", DataTable)
        table.add_columns("component", "version", "installed at", "consul service")
        for cid, entry in sorted(self.installer.state.installed().items()):
            table.add_row(
                cid,
                entry.version,
                entry.installed_at,
                entry.consul_service or "-",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit":
            self.app.exit(1 if self.failed else 0)
