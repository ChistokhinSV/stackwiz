"""Unit tests for state + action diff."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from stackwiz.manifest import load_manifest
from stackwiz.state import (
    STATE_FILENAME,
    STATE_SCHEMA_VERSION,
    Action,
    State,
    component_config_hash,
)

FIXTURE = Path(__file__).parent / "manifest_valid.yaml"


def test_fresh_state_all_install(tmp_path: Path) -> None:
    manifest = load_manifest(FIXTURE)
    state = State(tmp_path)
    actions = state.plan_actions(manifest, {"k3s", "app"}, {})
    assert actions["k3s"] == Action.INSTALL
    assert actions["app"] == Action.INSTALL
    assert actions["graylog"] == Action.NOOP  # not selected


def test_upgrade_detected(tmp_path: Path) -> None:
    manifest = load_manifest(FIXTURE)
    state = State(tmp_path)
    k3s = manifest.components[0]
    state.mark_installed(k3s, component_config_hash(k3s, {}))
    old_version = k3s.version
    upgraded = k3s.model_copy(update={"version": "1.31.0"})
    manifest2 = manifest.model_copy(update={"components": [upgraded, *manifest.components[1:]]})
    actions = State(tmp_path).plan_actions(manifest2, {"k3s"}, {})
    assert actions["k3s"] == Action.UPGRADE
    assert old_version != "1.31.0"


def test_reconfigure_on_config_change(tmp_path: Path) -> None:
    manifest = load_manifest(FIXTURE)
    state = State(tmp_path)
    app = manifest.components[1]
    state.mark_installed(app, component_config_hash(app, {"app_domain": "a.example"}))
    actions = State(tmp_path).plan_actions(
        manifest, {"app"}, {"app_domain": "b.example"}
    )
    assert actions["app"] == Action.RECONFIGURE


def test_noop_when_unchanged(tmp_path: Path) -> None:
    manifest = load_manifest(FIXTURE)
    state = State(tmp_path)
    app = manifest.components[1]
    cfg = {"app_domain": "a.example"}
    state.mark_installed(app, component_config_hash(app, cfg))
    actions = State(tmp_path).plan_actions(manifest, {"app"}, cfg)
    assert actions["app"] == Action.NOOP


def test_uninstall_plan(tmp_path: Path) -> None:
    manifest = load_manifest(FIXTURE)
    state = State(tmp_path)
    for c in manifest.components:
        state.mark_installed(c, component_config_hash(c, {}))
    reloaded = State(tmp_path)
    actions = reloaded.plan_uninstall(manifest, {"app", "graylog"})
    assert actions["app"] == Action.UNINSTALL
    assert actions["graylog"] == Action.UNINSTALL
    assert actions["k3s"] == Action.NOOP


def test_config_persistence(tmp_path: Path) -> None:
    state = State(tmp_path)
    state.save_config({"x": 1, "y": "two"})
    reloaded = State(tmp_path)
    assert reloaded.config() == {"x": 1, "y": "two"}


def test_hash_is_stable_across_dict_order() -> None:
    manifest = load_manifest(FIXTURE)
    c = manifest.components[0]
    h1 = component_config_hash(c, {"a": 1, "b": 2})
    h2 = component_config_hash(c, {"b": 2, "a": 1})
    assert h1 == h2


def test_future_state_schema_rejected(tmp_path: Path) -> None:
    """An older stackwiz reading a newer state file must refuse to load
    instead of silently corrupting it on save."""
    payload = {"schema": STATE_SCHEMA_VERSION + 5, "components": {}}
    (tmp_path / STATE_FILENAME).write_text(
        yaml.safe_dump(payload), encoding="utf-8",
    )
    with pytest.raises(Exception, match="schema"):
        State(tmp_path)


def test_invalid_state_schema_rejected(tmp_path: Path) -> None:
    payload = {"schema": "not-a-number", "components": {}}
    (tmp_path / STATE_FILENAME).write_text(
        yaml.safe_dump(payload), encoding="utf-8",
    )
    with pytest.raises(Exception, match="invalid schema"):
        State(tmp_path)


def test_missing_state_schema_defaults_to_1(tmp_path: Path) -> None:
    """Pre-schema state files must still load (they were all schema 1
    implicitly; the field was always written but readers ignored it)."""
    payload = {"components": {}}
    (tmp_path / STATE_FILENAME).write_text(
        yaml.safe_dump(payload), encoding="utf-8",
    )
    state = State(tmp_path)
    assert state.installed() == {}
