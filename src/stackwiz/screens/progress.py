"""Live install-progress screen: DataTable of components + streaming RichLog."""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Label, RichLog

from stackwiz.engine import Engine, Status, StepEvent
from stackwiz.executor import Executor

if TYPE_CHECKING:
    from stackwiz.app import InstallerApp

STATUS_ICON = {
    Status.PENDING: "•",
    Status.RUNNING: "⟳",
    Status.DONE: "✓",
    Status.FAILED: "✗",
    Status.SKIPPED: "–",
}


class ProgressScreen(Screen):
    BINDINGS = [
        ("q", "app.quit", "Quit"),
        ("n", "proceed", "Next"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._failed = False
        self._done = False

    @property
    def installer(self) -> InstallerApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="main"):
            yield Label("[b]Installing components[/b]", id="progress-title")
            yield DataTable(id="progress-table", cursor_type="row")
            yield RichLog(id="progress-log", highlight=False, markup=False, wrap=True)
        with Horizontal(id="button-bar"):
            yield Button("Abort", id="abort", variant="error")
            yield Button("Next", id="next", variant="primary", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#progress-table", DataTable)
        table.add_columns("status", "component", "action", "message")
        for component in self.installer.manifest.topo_order():
            table.add_row(
                STATUS_ICON[Status.PENDING],
                component.id,
                "",
                "",
                key=component.id,
            )
        title = self.query_one("#progress-title", Label)
        if self.installer.mode == "uninstall":
            title.update("[b]Removing components[/b]")
        self.run_engine()

    @work(exclusive=True)
    async def run_engine(self) -> None:
        engine = self._build_engine()
        log_widget = self.query_one("#progress-log", RichLog)
        table = self.query_one("#progress-table", DataTable)

        try:
            if self.installer.mode == "uninstall":
                iterator = engine.uninstall(self.installer.selected_component_ids)
            else:
                iterator = engine.install(
                    self.installer.selected_component_ids,
                    self.installer.config_values,
                    forced_refresh=self.installer.forced_refresh or None,
                )
            async for event in iterator:
                self._apply_event(table, log_widget, event)
        except Exception as exc:  # noqa: BLE001
            log_widget.write(f"engine error: {exc}")
            self._failed = True

        if engine.last_run is not None:
            self.installer.materialized_secrets = dict(engine.last_run.secrets)

        self._done = True
        next_btn = self.query_one("#next", Button)
        next_btn.disabled = False
        if self._failed:
            next_btn.label = "Continue (failures)"
            next_btn.variant = "error"
        else:
            next_btn.label = "Next"

    def _apply_event(self, table: DataTable, log_widget: RichLog, event: StepEvent) -> None:
        if event.line is not None:
            prefix = "ERR " if event.stream == "stderr" else "    "
            log_widget.write(f"{prefix}[{event.component_id}] {event.line}")
            # Mirror the latest output line into the DataTable message column
            # so the operator sees progress at a glance (e.g. "Downloading 80MB"
            # during a docker pull) without watching the log scroll.
            snippet = event.line.strip()[:60]
            if snippet:
                try:
                    table.update_cell(event.component_id, "message", snippet)
                except Exception:  # noqa: BLE001
                    pass
            return

        if event.status is Status.FAILED:
            self._failed = True

        try:
            table.update_cell(event.component_id, "status", STATUS_ICON[event.status])
            table.update_cell(event.component_id, "action", event.action.value)
            if event.message:
                table.update_cell(event.component_id, "message", event.message)
        except Exception:  # noqa: BLE001 — row may not exist yet for bootstrap injection
            pass

    def _build_engine(self) -> Engine:
        manifest = self.installer.manifest
        executor = Executor(manifest_dir=self.installer.manifest_dir)
        return Engine(
            manifest=manifest,
            state=self.installer.state,
            executor=executor,
            consul=self.installer.consul_client,
            vault=self.installer.vault_client,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "next" and self._done:
            self.action_proceed()
        elif event.button.id == "abort":
            self.app.exit(1)

    def action_proceed(self) -> None:
        if not self._done:
            return
        from stackwiz.screens.summary import SummaryScreen
        self.app.push_screen(SummaryScreen(failed=self._failed))
