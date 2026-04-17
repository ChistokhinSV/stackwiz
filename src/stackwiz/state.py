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
# Bump when the on-disk state shape changes in a non-additive way. Old
# stackwiz readers refuse state files with a higher schema number on load.
STATE_SCHEMA_VERSION = 1


class Action(StrEnum):
    """What should happen to a component on the next run."""

    INSTALL = "install"          # not present in state
    UPGRADE = "upgrade"          # version differs
    RECONFIGURE = "reconfigure"  # version same, config hash differs
    REFRESH = "refresh"          # idempotent re-run (repeatable component or --force)
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
    """Read/write `/state/installed.yaml` and `/state/config.yaml`.

    `state_dir` is the container-side path where the engine actually does
    I/O. `host_state_dir` is the operator-facing path (same filesystem on
    the host side of the bind mount) — use it in every message printed to
    the TUI, summary.md, install.log, or CLI output so operators can find
    files without knowing container internals. `STACKWIZ_HOST_STATE_DIR`
    is set by bootstrap.sh to the host path.
    """

    def __init__(self, state_dir: Path) -> None:
        import os
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        host = os.environ.get("STACKWIZ_HOST_STATE_DIR")
        self.host_state_dir: Path = Path(host) if host else self.state_dir
        self.installed_path = self.state_dir / STATE_FILENAME
        self.config_path = self.state_dir / CONFIG_FILENAME
        self._installed: dict[str, InstalledComponent] = {}
        self._config: dict[str, Any] = {}
        self._load()

    def host_path(self, *parts: str) -> str:
        """Render a host-side path for display. Joins parts POSIX-style
        regardless of the container OS (Windows dev runs work too)."""
        base = str(self.host_state_dir).replace("\\", "/").rstrip("/")
        if not parts:
            return base
        suffix = "/".join(p.strip("/") for p in parts if p)
        return f"{base}/{suffix}" if suffix else base

    def _load(self) -> None:
        if self.installed_path.exists():
            raw = yaml.safe_load(self.installed_path.read_text(encoding="utf-8")) or {}
            # Validate the state schema matches what this version of stackwiz
            # knows how to read. An older installer could otherwise silently
            # accept a newer-schema state file and corrupt data on save.
            schema = raw.get("schema", 1)
            if not isinstance(schema, int) or schema < 1:
                raise ValueError(
                    f"state {self.installed_path}: invalid schema {schema!r}"
                )
            if schema > STATE_SCHEMA_VERSION:
                raise ValueError(
                    f"state {self.installed_path}: schema={schema} is newer "
                    f"than this stackwiz version supports "
                    f"(max={STATE_SCHEMA_VERSION}). Upgrade stackwiz."
                )
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
            "schema": STATE_SCHEMA_VERSION,
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
        forced_refresh: set[str] | None = None,
    ) -> dict[str, Action]:
        """Decide the per-component action for an install-mode run.

        `forced_refresh` is a set of component ids that should run with
        `Action.REFRESH` even when their version + config-hash haven't
        changed. Used by `wizinstall refresh` to force re-execution.
        Components with `repeatable: true` in the manifest are treated
        as if they were always in `forced_refresh`.
        """
        forced = set(forced_refresh or ())
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
            elif component.id in forced or component.repeatable:
                actions[component.id] = Action.REFRESH
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
