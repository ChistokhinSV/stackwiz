"""First configuration screen — prompt only for `domain`.

Every derived field (anything with ``derived: true`` or a ``${domain}``
template in its manifest default) is re-rendered against the typed value
before the main ConfigScreen renders, so operators never have to retype
``auth.example.com``, ``vault.example.com``, etc. — one knob drives the
cascade.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Static

from stackwiz.config_overrides import rederive_for_domain

if TYPE_CHECKING:
    from stackwiz.app import InstallerApp


class DomainScreen(Screen):
    BINDINGS = [
        ("q", "app.quit", "Quit"),
        ("n", "proceed", "Next"),
        ("b", "back", "Back"),
    ]

    @property
    def installer(self) -> InstallerApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main"):
            yield Label("[b]Deployment domain[/b]")
            yield Static(
                "One domain drives every derived hostname, admin email, LDAP "
                "base DN, and TLS SAN in this manifest. Change it here once; "
                "the next screen will show every field pre-filled from this "
                "value."
            )
            yield Label("Domain [red]*[/red]")
            yield Input(
                value=str(self.installer.effective_domain or ""),
                placeholder="example.com",
                id="cfg-domain",
            )
            yield Static("", id="domain-hint")
        with Horizontal(id="button-bar"):
            yield Button("Back", id="back")
            yield Button("Next", id="next", variant="primary")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "next":
            self.action_proceed()
        elif event.button.id == "back":
            self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_proceed(self) -> None:
        raw = self.query_one("#cfg-domain", Input).value.strip()
        if not raw:
            self.query_one("#domain-hint", Static).update(
                "[red]Domain is required.[/red]"
            )
            return

        # Re-render every derived field against the new domain so the next
        # screen's defaults reflect auth.${domain}, vault.${domain}, etc.
        new_values = rederive_for_domain(
            self.installer.manifest,
            dict(self.installer.effective_config),
            raw,
        )
        self.installer.effective_config = new_values
        self.installer.effective_domain = raw
        self.installer.config_values = dict(new_values)

        from stackwiz.screens.config import ConfigScreen
        self.app.push_screen(ConfigScreen())
