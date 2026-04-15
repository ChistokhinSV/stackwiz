"""Pydantic v2 models for components.yaml and loader."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConsulServiceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    http: str | None = None
    tcp: str | None = None
    interval: str = "30s"
    timeout: str = "5s"

    @model_validator(mode="after")
    def _one_probe(self) -> ConsulServiceCheck:
        if not self.http and not self.tcp:
            raise ValueError("consul check needs either http or tcp")
        return self


class ConsulService(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    port: int
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, str] = Field(default_factory=dict)
    check: ConsulServiceCheck | None = None


class ConsulDiscover(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    env_var: str
    # When true (default), missing the service at install time aborts the step
    # with a clear message. Set to false for soft dependencies whose absence
    # should leave `env_var` unset rather than fail the install.
    required: bool = True


class ConsulConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = True
    service_prefix: str


class Component(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    version: str = "0.0.0"
    required: bool = False
    default: bool = True
    repeatable: bool = False
    group: str = "default"
    depends: list[str] = Field(default_factory=list)
    install: Path
    uninstall: Path | None = None
    upgrade: Path | None = None
    verify: str | None = None
    # Either `consul_service` (singular) for one service, OR `consul_services`
    # (plural) for components that register multiple endpoints (e.g. authentik
    # exposing both HTTP and LDAPS). Mutually exclusive.
    consul_service: ConsulService | None = None
    consul_services: list[ConsulService] = Field(default_factory=list)
    consul_discover: list[ConsulDiscover] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_slug(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError(f"component id must be alphanumeric/-/_: {v!r}")
        return v

    @model_validator(mode="after")
    def _service_exclusivity(self) -> Component:
        if self.consul_service is not None and self.consul_services:
            svc_names = ", ".join(s.name for s in self.consul_services) or "<unnamed>"
            raise ValueError(
                f"component {self.id!r}: `consul_service` and `consul_services` "
                f"are mutually exclusive. Use `consul_service:` for a single "
                f"service (got {self.consul_service.name!r}) OR "
                f"`consul_services:` for a list (got [{svc_names}]) — "
                f"not both."
            )
        return self

    def all_consul_services(self) -> list[ConsulService]:
        """Unified view — callers iterate this instead of the two fields."""
        if self.consul_services:
            return list(self.consul_services)
        if self.consul_service is not None:
            return [self.consul_service]
        return []


class ConfigField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    type: Literal["text", "select", "bool", "int", "password"]
    default: Any = None
    choices: list[str] | None = None
    required: bool = False
    help: str | None = None
    # Derived fields are always re-rendered from their manifest default at run
    # time; any value cached in state or typed into the TUI is ignored. Use
    # for defaults that reference other config via ``${...}`` so one knob
    # (e.g. domain) propagates through every derived hostname on every run.
    # If omitted, fields whose default contains ``${...}`` are treated as
    # derived (backward-compat heuristic). Set to ``false`` to freeze a
    # resolved value into state.
    derived: bool | None = None

    @model_validator(mode="after")
    def _select_has_choices(self) -> ConfigField:
        if self.type == "select" and not self.choices:
            raise ValueError(f"config field {self.id!r} is type=select but has no choices")
        return self

    @property
    def is_derived(self) -> bool:
        if self.derived is not None:
            return self.derived
        return isinstance(self.default, str) and "${" in self.default


class Secret(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    generate: bool = True
    # `length` semantics depend on `type`:
    #   password → output chars
    #   hex      → random bytes (output = 2 × length chars)
    #   base64   → random bytes (output length is base64 of those bytes)
    #   uuid/cmd → ignored
    length: int = 32
    type: Literal["password", "hex", "base64", "uuid", "cmd"] = "password"
    command: str | None = None
    immutable: bool = False
    optional: bool = False  # if True and generate=False, missing value is OK (empty string)
    vault_path: str | None = None  # defaults to <service_prefix>/<id> if unset
    # Earlier ids used for this secret. On first run, the engine migrates any
    # value found at an old `<prefix>/<previous_id>` path to the new path,
    # preserving immutable values across renames. The old path is deleted
    # after the migration succeeds.
    previous_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _type_contract(self) -> Secret:
        if self.type == "cmd":
            if not self.command:
                raise ValueError(
                    f"secret {self.id!r}: type=cmd requires a `command:` value"
                )
        elif self.command is not None:
            raise ValueError(
                f"secret {self.id!r}: `command:` is only valid with type=cmd "
                f"(got type={self.type})"
            )
        if self.type in {"password", "hex", "base64"} and self.length <= 0:
            raise ValueError(
                f"secret {self.id!r}: length must be > 0 for type={self.type}"
            )
        return self


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    version: str
    domain: str
    vault_host: str | None = None
    consul_host: str | None = None
    consul: ConsulConfig
    components: list[Component]
    config: list[ConfigField] = Field(default_factory=list)
    secrets: list[Secret] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_deps(self) -> Manifest:
        ids = {c.id for c in self.components}
        for c in self.components:
            for d in c.depends:
                if d not in ids:
                    raise ValueError(f"component {c.id!r} depends on unknown component {d!r}")
        self._topo_sort()  # raises on cycle
        return self

    def _topo_sort(self) -> list[str]:
        by_id = {c.id: c for c in self.components}
        visited: dict[str, int] = {}  # 0=white, 1=gray, 2=black
        order: list[str] = []

        def visit(cid: str, stack: list[str]) -> None:
            color = visited.get(cid, 0)
            if color == 2:
                return
            if color == 1:
                cycle = " -> ".join([*stack, cid])
                raise ValueError(f"dependency cycle: {cycle}")
            visited[cid] = 1
            for d in by_id[cid].depends:
                visit(d, [*stack, cid])
            visited[cid] = 2
            order.append(cid)

        for c in self.components:
            visit(c.id, [])
        return order

    def topo_order(self) -> list[Component]:
        """Components sorted so dependencies come first."""
        by_id = {c.id: c for c in self.components}
        return [by_id[i] for i in self._topo_sort()]

    def consul_addr(self) -> str:
        return self.consul_host or f"consul.{self.domain}"

    def vault_addr(self) -> str:
        return self.vault_host or f"vault.{self.domain}"


def load_manifest(path: Path | str) -> Manifest:
    """Load and validate a components.yaml file."""
    p = Path(path)
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"manifest root must be a mapping: {p}")
    return Manifest.model_validate(data)
