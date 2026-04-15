"""Dynamic configuration form driven by the manifest's `config:` section."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Select, Static

from stackwiz.manifest import ConfigField

if TYPE_CHECKING:
    from stackwiz.app import InstallerApp


class ConfigScreen(Screen):
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
            yield Label("[b]Configuration[/b]")
            state = self.installer.state
            yield Static(
                f"Domain: [cyan]{self.installer.effective_domain}[/cyan] "
                "(change on the previous screen to re-derive all hostnames)"
            )
            yield Static(
                "Priority: .stackwiz.env > saved state > manifest defaults. "
                f"Values saved to {state.host_path('config.yaml')} on Next."
            )
            existing = self._initial_values()
            for field in self.installer.manifest.config:
                # `domain` is handled by the dedicated DomainScreen; skip here.
                if field.id == "domain":
                    continue
                default = existing.get(field.id, field.default)
                yield Label(
                    f"{field.label}"
                    + (" [red]*[/red]" if field.required else "")
                )
                if field.help:
                    yield Static(f"[dim]{field.help}[/dim]")
                yield self._build_widget(field, default)
            yield Static("", id="config-hint")
        with Horizontal(id="button-bar"):
            yield Button("Back", id="back")
            yield Button("Next", id="next", variant="primary")
        yield Footer()

    def _initial_values(self) -> dict[str, Any]:
        """Use the pre-computed effective_config from the app.

        The app already merged state > .stackwiz.env > manifest defaults and
        ran `${var}` substitution, so we just read from there. The welcome
        screen uses the same map for its probe, keeping everything consistent.
        """
        return dict(self.installer.effective_config)

    def _build_widget(self, field: ConfigField, default: Any):
        wid = f"cfg-{field.id}"
        if field.type in {"text", "int"}:
            return Input(
                value="" if default is None else str(default),
                placeholder=field.label,
                password=False,
                id=wid,
            )
        if field.type == "password":
            return Input(
                value="" if default is None else str(default),
                placeholder=field.label,
                password=True,
                id=wid,
            )
        if field.type == "bool":
            return Checkbox(field.label, value=bool(default), id=wid)
        if field.type == "select":
            choices = [(choice, choice) for choice in (field.choices or [])]
            return Select(
                options=choices,
                value=default if default in (field.choices or []) else Select.BLANK,
                id=wid,
            )
        raise ValueError(f"unsupported config field type: {field.type}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "next":
            self.action_proceed()
        elif event.button.id == "back":
            self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_proceed(self) -> None:
        values: dict[str, Any] = {}
        missing: list[str] = []
        # Preserve the domain set on DomainScreen (no widget for it here).
        values["domain"] = self.installer.effective_domain
        for field in self.installer.manifest.config:
            if field.id == "domain":
                continue
            wid = f"#cfg-{field.id}"
            widget = self.query_one(wid)
            value: Any
            if isinstance(widget, Checkbox):
                value = bool(widget.value)
            elif isinstance(widget, Select):
                value = widget.value if widget.value is not Select.BLANK else None
            elif isinstance(widget, Input):
                raw = widget.value
                if field.type == "int":
                    value = int(raw) if raw else None
                else:
                    value = raw or None
            else:
                value = None
            if field.required and (value is None or value == ""):
                missing.append(field.id)
            values[field.id] = value

        if missing:
            self.query_one("#config-hint", Static).update(
                f"[red]Required: {', '.join(missing)}[/red]"
            )
            return

        self.installer.config_values = values
        from stackwiz.screens.progress import ProgressScreen
        self.app.push_screen(ProgressScreen())
