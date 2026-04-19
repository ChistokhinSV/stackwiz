"""Pydantic v2 models for components.yaml and loader."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

log = logging.getLogger("stackwiz.manifest")

# Bump this when the manifest shape changes in a non-additive way. Additive
# fields don't need a bump — `_LeafModel` already warns on unknown keys for
# consumer forward-compat. The version is enforced by `load_manifest`.
CURRENT_SCHEMA_VERSION = 1


class _LeafModel(BaseModel):
    """Base class for user-authored manifest leaves.

    Unknown keys are logged at WARNING and dropped — NOT a hard error.
    A framework upgrade that adds a new field to Component / Secret /
    ConfigField must not break older consumer manifests that don't know
    about it yet. ``extra="forbid"`` is reserved for the root ``Manifest``
    model where typos should fail loud.
    """

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _warn_on_extras(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        known = set(cls.model_fields.keys())
        extras = sorted(k for k in values if k not in known)
        if extras:
            log.warning(
                "manifest: ignoring unknown key%s on %s: %s",
                "" if len(extras) == 1 else "s",
                cls.__name__,
                ", ".join(extras),
            )
        return values


class ConsulServiceCheck(_LeafModel):
    http: str | None = None
    tcp: str | None = None
    interval: str = "30s"
    timeout: str = "5s"
    # HTTP-only: skip TLS cert verification. Required when the target cert's
    # SAN doesn't match the host the check is made from — e.g. a vault cert
    # issued for "vault.example.com" but the consul agent reaches it via the
    # docker-network hostname "vault". Consul's TLSSkipVerify is the native
    # way to opt out.
    tls_skip_verify: bool = False

    @model_validator(mode="after")
    def _one_probe(self) -> ConsulServiceCheck:
        if not self.http and not self.tcp:
            raise ValueError("consul check needs either http or tcp")
        if self.tls_skip_verify and not self.http:
            raise ValueError("tls_skip_verify is only meaningful with http checks")
        return self


class ConsulService(_LeafModel):
    name: str
    port: int
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, str] = Field(default_factory=dict)
    check: ConsulServiceCheck | None = None
    # Address to advertise in the Consul catalog. When omitted, the
    # engine uses the node's LAN IP (as before). Set to a public
    # hostname for HTTP services fronted by nginx so consumers
    # discovering the service via Consul get the externally-reachable
    # address, not the host's internal IP. Templated — supports
    # ${config_field} interpolation against manifest config values.
    address: str | None = None


class ConsulDiscover(_LeafModel):
    service: str
    env_var: str
    # When true (default), missing the service at install time aborts the step
    # with a clear message. Set to false for soft dependencies whose absence
    # should leave `env_var` unset rather than fail the install.
    required: bool = True


class ConsulConfig(_LeafModel):
    required: bool = True
    service_prefix: str


class RegistryEntry(_LeafModel):
    """Cross-stack-discoverable resource a component exposes.

    Canonical replacement for the current triad of (docker labels,
    Consul service tags, Vault bearer-path conventions) used by
    kb-source-sync + kb-mcp-registrar. The engine writes every entry
    to Vault (stackwiz/data/registry/<kind>/<name>/{config,token}) and
    mirrors a reference into Consul KV (stackwiz/registry/<kind>/<name>)
    so a hub daemon can discover via a single blocking query.

    One schema handles both KB sources (pull/push tarball paths) and
    MCP servers (single url). Future kinds (authentik-app, nginx-vhost)
    fit the same shape with different `paths` semantics.
    """

    kind: Literal["kb-source", "mcp-server"]
    name: str
    endpoint_url: str
    # `http` for kb-source content endpoints; `streamable_http` / `sse`
    # for MCP servers. The hub uses this to pick the right client.
    transport: Literal["http", "streamable_http", "sse"] = "http"
    # For kb-source: {pull: "/.kb/snapshot", push: "/.kb/push",
    #   health: "/.kb/health"}. For mcp-server: typically empty
    # (endpoint_url IS the MCP endpoint). Extra keys allowed for
    # future kinds; the hub reads only the keys its kind uses.
    paths: dict[str, str] = Field(default_factory=dict)
    # Name of a secret declared in the manifest's top-level `secrets:`
    # block. The engine materializes it and stores the value at the
    # registry `token` path; the hub reads via its token_ref.
    # Optional — omit for anonymous endpoints.
    bearer_secret: str | None = None
    tags: list[str] = Field(default_factory=list)
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name_slug(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError(
                f"registry entry name must be alphanumeric/-/_: {v!r}",
            )
        return v


class Component(_LeafModel):
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
    # Declarative cross-stack discovery. Each entry yields one
    # stackwiz/data/registry/<kind>/<name>/{config,token} write at
    # install time. Hub (framework-owned daemon) reads these to drive
    # KB sync + MCP registration. Supersedes the current mix of docker
    # labels + consul tags + manual Vault publishes.
    registry: list[RegistryEntry] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # Config keys this component publishes to Consul KV under
    # ``{service_prefix}/config/<key>``. Empty list (the default) means
    # NOTHING is published — previously the engine mirrored every config
    # value for every component, producing two sources of truth with
    # ``state/config.yaml``. Declare only the keys other components /
    # external consumers need to discover at runtime.
    publishes: list[str] = Field(default_factory=list)

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


class ConfigField(_LeafModel):
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


class Secret(_LeafModel):
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
    # Root: typos here should fail loud. Leaves use _LeafModel which tolerates
    # unknown keys so consumers survive framework field additions.
    model_config = ConfigDict(extra="forbid")

    schema_version: int = CURRENT_SCHEMA_VERSION
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
        # RegistryEntry.bearer_secret must name a declared secret so the
        # engine can materialize the value at publish time.
        secret_ids = {s.id for s in self.secrets}
        seen_registry: set[tuple[str, str]] = set()
        for c in self.components:
            for r in c.registry:
                if r.bearer_secret and r.bearer_secret not in secret_ids:
                    raise ValueError(
                        f"component {c.id!r}: registry entry {r.name!r} references "
                        f"bearer_secret {r.bearer_secret!r} which is not declared "
                        f"in the manifest's `secrets:` block",
                    )
                key = (r.kind, r.name)
                if key in seen_registry:
                    raise ValueError(
                        f"duplicate registry entry: kind={r.kind!r} name={r.name!r} "
                        f"(second occurrence in component {c.id!r})",
                    )
                seen_registry.add(key)
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
    version = data.get("schema_version", CURRENT_SCHEMA_VERSION)
    if not isinstance(version, int) or version < 1:
        raise ValueError(
            f"manifest {p}: schema_version must be a positive integer, got {version!r}"
        )
    if version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"manifest {p}: schema_version={version} is newer than this "
            f"framework supports (max={CURRENT_SCHEMA_VERSION}). "
            f"Upgrade stackwiz."
        )
    return Manifest.model_validate(data)
