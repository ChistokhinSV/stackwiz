"""Tests for Action.REFRESH: repeatable flag + wizinstall refresh subcommand."""
from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from stackwiz.cli import main
from stackwiz.manifest import load_manifest
from stackwiz.state import Action, State, component_config_hash

FIXTURE = Path(__file__).parent / "manifest_valid.yaml"


def _manifest_with_repeatable(tmp_path: Path, component_id: str) -> Path:
    """Clone the fixture and mark one component as repeatable."""
    data = yaml.safe_load(FIXTURE.read_text())
    for c in data["components"]:
        if c["id"] == component_id:
            c["repeatable"] = True
    out = tmp_path / "manifest.yaml"
    out.write_text(yaml.safe_dump(data))
    return out


def test_repeatable_component_replans_noop_as_refresh(tmp_path: Path) -> None:
    """An installed repeatable component plans as REFRESH instead of NOOP."""
    manifest_path = _manifest_with_repeatable(tmp_path, "app")
    manifest = load_manifest(manifest_path)
    state = State(tmp_path / "state")

    # Install every component at the current version/config.
    for c in manifest.components:
        state.mark_installed(c, component_config_hash(c, {}))

    actions = state.plan_actions(
        manifest,
        selected_ids={c.id for c in manifest.components},
        config_values={},
    )
    # app is repeatable → REFRESH
    assert actions["app"] == Action.REFRESH
    # Others stayed NOOP
    assert actions["k3s"] == Action.NOOP
    assert actions["graylog"] == Action.NOOP


def test_forced_refresh_overrides_noop(tmp_path: Path) -> None:
    """A component in forced_refresh plans as REFRESH even without repeatable."""
    manifest = load_manifest(FIXTURE)
    state = State(tmp_path / "state")
    for c in manifest.components:
        state.mark_installed(c, component_config_hash(c, {}))

    actions = state.plan_actions(
        manifest,
        selected_ids={c.id for c in manifest.components},
        config_values={},
        forced_refresh={"graylog"},
    )
    assert actions["graylog"] == Action.REFRESH
    assert actions["k3s"] == Action.NOOP
    assert actions["app"] == Action.NOOP


def test_forced_refresh_does_not_override_upgrade(tmp_path: Path) -> None:
    """If a version bump would trigger UPGRADE, forced_refresh doesn't downgrade it."""
    manifest = load_manifest(FIXTURE)
    state = State(tmp_path / "state")
    for c in manifest.components:
        state.mark_installed(c, component_config_hash(c, {}))

    # Bump app's version
    bumped = [
        c if c.id != "app" else c.model_copy(update={"version": "9.9.9"})
        for c in manifest.components
    ]
    bumped_manifest = manifest.model_copy(update={"components": bumped})

    actions = state.plan_actions(
        bumped_manifest,
        selected_ids={c.id for c in bumped_manifest.components},
        config_values={},
        forced_refresh={"app"},
    )
    # UPGRADE wins over forced_refresh
    assert actions["app"] == Action.UPGRADE


def test_refresh_subcommand_refuses_when_nothing_installed(tmp_path: Path) -> None:
    """`wizinstall refresh` with no args + empty state → exit 2 with a clear message."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "refresh",
            "--manifest", str(FIXTURE),
            "--state", str(tmp_path / "state"),
            "--auto",
        ],
    )
    assert result.exit_code == 2
    assert "nothing installed" in result.output


def test_refresh_subcommand_selects_only_installed(tmp_path: Path) -> None:
    """Explicit args that aren't installed fire a warning (but don't crash)."""
    runner = CliRunner()
    state_dir = tmp_path / "state"
    state = State(state_dir)
    manifest = load_manifest(FIXTURE)
    # Install only k3s
    state.mark_installed(manifest.components[0], component_config_hash(manifest.components[0], {}))

    # Ask to refresh `graylog` which isn't installed — should warn, not crash.
    # We don't actually dispatch a real install here (no backends); we only
    # verify the CLI reaches the warning.
    result = runner.invoke(
        main,
        [
            "refresh",
            "--manifest", str(FIXTURE),
            "--state", str(state_dir),
            "--auto",
            "graylog",
        ],
        catch_exceptions=True,
    )
    assert "not installed" in result.output


def test_manifest_repeatable_defaults_false() -> None:
    """Un-flagged components keep `repeatable = False`."""
    manifest = load_manifest(FIXTURE)
    assert all(c.repeatable is False for c in manifest.components)


def test_repeatable_flag_parsed(tmp_path: Path) -> None:
    """`repeatable: true` in YAML hits the Pydantic model."""
    manifest_path = _manifest_with_repeatable(tmp_path, "app")
    manifest = load_manifest(manifest_path)
    app = next(c for c in manifest.components if c.id == "app")
    assert app.repeatable is True
