"""Component selection screen with dependency auto-include + action badges."""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Label, SelectionList, Static
from textual.widgets.selection_list import Selection

if TYPE_CHECKING:
    from stackwiz.app import InstallerApp


class ComponentsScreen(Screen):
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
            yield Label("[b]Select components[/b]")
            yield Static(
                "Required components are pre-selected. Dependencies are "
                "auto-included on Next."
            )
            yield SelectionList[str](*self._build_selections(), id="component-list")
            yield Static("", id="components-hint")
        with Horizontal(id="button-bar"):
            yield Button("Back", id="back")
            yield Static("", id="bar-spacer")
            yield Button("Next", id="next", variant="primary")
        yield Footer()

    def _build_selections(self) -> list[Selection[str]]:
        manifest = self.installer.manifest
        installed_map = self.installer.state.installed()
        uninstall_mode = self.installer.mode == "uninstall"

        selections: list[Selection[str]] = []
        for component in manifest.topo_order():
            installed = installed_map.get(component.id)
            badge = ""
            if uninstall_mode:
                if installed is None:
                    continue  # nothing to uninstall
            else:
                if installed is None:
                    badge = "[install]"
                elif installed.version != component.version:
                    badge = f"[upgrade {installed.version}→{component.version}]"
                else:
                    badge = "[ok]"
            label = f"{component.name} [{component.group}] {badge}"
            default_selected = (
                component.required
                or component.default
                or (uninstall_mode and installed is not None)
            )
            selections.append(Selection(label, component.id, default_selected))
        return selections

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "next":
            self.action_proceed()
        elif event.button.id == "back":
            self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_proceed(self) -> None:
        sel_list = self.query_one("#component-list", SelectionList)
        selected = set(sel_list.selected)

        by_id = {c.id: c for c in self.installer.manifest.components}
        # Auto-include dependencies (only relevant in install mode).
        if self.installer.mode == "install":
            changed = True
            while changed:
                changed = False
                for cid in list(selected):
                    for dep in by_id[cid].depends:
                        if dep not in selected:
                            selected.add(dep)
                            changed = True
            # Required components cannot be deselected.
            for c in self.installer.manifest.components:
                if c.required:
                    selected.add(c.id)

        if not selected:
            self.query_one("#components-hint", Static).update(
                "[red]Select at least one component.[/red]"
            )
            return

        self.installer.selected_component_ids = selected

        if self.installer.mode == "uninstall" or not self.installer.manifest.config:
            from stackwiz.screens.progress import ProgressScreen
            self.app.push_screen(ProgressScreen())
        else:
            from stackwiz.screens.config import ConfigScreen
            self.app.push_screen(ConfigScreen())
