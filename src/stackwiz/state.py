"""Persistent installer state: /state/installed.yaml + config hashing + action diff."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from stackwiz.manifest import Component, Manifest

STATE_FILENAME = "installed.yaml"
CONFIG_FILENAME = "config.yaml"


class Action(StrEnum):
    """What should happen to a component on the next run."""

    INSTALL = "install"          # not present in state
    UPGRADE = "upgrade"          # version differs
    RECONFIGURE = "reconfigure"  # version same, config hash differs
    NOOP = "noop"                # nothing to do
    UNINSTALL = "uninstall"      # teardown


@dataclass
class InstalledComponent:
    id: str
    version: str
    config_hash: str
    installed_at: str
    consul_service: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "config_hash": self.config_hash,
            "installed_at": self.installed_at,
            "consul_service": self.consul_service,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstalledComponent:
        return cls(
            id=data["id"],
            version=data["version"],
            config_hash=data["config_hash"],
            installed_at=data["installed_at"],
            consul_service=data.get("consul_service"),
        )


class State:
    """Read/write `/state/installed.yaml` and `/state/config.yaml`."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.installed_path = self.state_dir / STATE_FILENAME
        self.config_path = self.state_dir / CONFIG_FILENAME
        self._installed: dict[str, InstalledComponent] = {}
        self._config: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self.installed_path.exists():
            raw = yaml.safe_load(self.installed_path.read_text(encoding="utf-8")) or {}
            for cid, entry in raw.get("components", {}).items():
                self._installed[cid] = InstalledComponent.from_dict({"id": cid, **entry})
        if self.config_path.exists():
            self._config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}

    # --- installed components ---------------------------------------------------

    def installed(self) -> dict[str, InstalledComponent]:
        return dict(self._installed)

    def get(self, component_id: str) -> InstalledComponent | None:
        return self._installed.get(component_id)

    def mark_installed(
        self,
        component: Component,
        config_hash: str,
    ) -> None:
        svc_names = [s.name for s in component.all_consul_services()]
        entry = InstalledComponent(
            id=component.id,
            version=component.version,
            config_hash=config_hash,
            installed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            consul_service=",".join(svc_names) if svc_names else None,
        )
        self._installed[component.id] = entry
        self._save_installed()

    def mark_uninstalled(self, component_id: str) -> None:
        self._installed.pop(component_id, None)
        self._save_installed()

    def _save_installed(self) -> None:
        payload = {
            "schema": 1,
            "components": {
                cid: {k: v for k, v in entry.to_dict().items() if k != "id"}
                for cid, entry in sorted(self._installed.items())
            },
        }
        self.installed_path.write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )

    # --- config -----------------------------------------------------------------

    def config(self) -> dict[str, Any]:
        return dict(self._config)

    def save_config(self, values: dict[str, Any]) -> None:
        self._config = dict(values)
        self.config_path.write_text(
            yaml.safe_dump(self._config, sort_keys=True), encoding="utf-8"
        )

    # --- action diff ------------------------------------------------------------

    def plan_actions(
        self,
        manifest: Manifest,
        selected_ids: set[str],
        config_values: dict[str, Any],
    ) -> dict[str, Action]:
        """Decide the per-component action for an install-mode run."""
        actions: dict[str, Action] = {}
        for component in manifest.topo_order():
            if component.id not in selected_ids:
                actions[component.id] = Action.NOOP
                continue
            installed = self._installed.get(component.id)
            cfg_hash = component_config_hash(component, config_values)
            if installed is None:
                actions[component.id] = Action.INSTALL
            elif installed.version != component.version:
                actions[component.id] = Action.UPGRADE
            elif installed.config_hash != cfg_hash:
                actions[component.id] = Action.RECONFIGURE
            else:
                actions[component.id] = Action.NOOP
        return actions

    def plan_uninstall(
        self,
        manifest: Manifest,
        selected_ids: set[str],
    ) -> dict[str, Action]:
        """Uninstall-mode: selected components get UNINSTALL, others NOOP."""
        actions: dict[str, Action] = {}
        for component in manifest.topo_order():
            if component.id in selected_ids and component.id in self._installed:
                actions[component.id] = Action.UNINSTALL
            else:
                actions[component.id] = Action.NOOP
        return actions


def component_config_hash(component: Component, config_values: dict[str, Any]) -> str:
    """Stable SHA256 of the component's inputs (version + env + config).

    Sensitive values live in Vault, not here — this hash is purely for detecting
    whether a re-run needs to reconfigure.
    """
    payload = {
        "version": component.version,
        "env": dict(sorted(component.env.items())),
        "config": _canonical(config_values),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value
