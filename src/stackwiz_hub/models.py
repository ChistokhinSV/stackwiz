"""Pydantic models for the registry-entry documents the hub reads.

Mirrors the schema that `stackwiz.engine._publish_registry` writes to
Vault — intentionally duplicated rather than imported so the hub has
no transitive dependency on the full engine tree (the installed image
would otherwise drag in textual, click, etc.).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    transport: Literal["http", "streamable_http", "sse"] = "http"
    paths: dict[str, str] = Field(default_factory=dict)


class Auth(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["bearer", "none"] = "none"
    # Relative path under stackwiz/data/: points to a KV entry with a
    # single `value` key holding the token string. None when mode=none.
    token_ref: str | None = None


class RegistryDoc(BaseModel):
    """The JSON document stored at
    stackwiz/data/registry/<kind>/<name>/config (KV v2's inner `value`
    field — the engine JSON-encodes it once so this stays one KV read)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    kind: Literal["kb-source", "mcp-server"]
    name: str
    owner: str
    component_id: str = ""
    endpoint: Endpoint
    auth: Auth = Field(default_factory=Auth)
    tags: list[str] = Field(default_factory=list)
    description: str = ""


class RegistryPointer(BaseModel):
    """The JSON blob stored at Consul KV stackwiz/registry/<kind>/<name>.

    Thin pointer — hub uses this to key blocking queries and locate the
    full doc in Vault. Keeping it minimal means bearer rotation is a
    single Vault write that doesn't dirty the Consul KV modify-index
    (so every rotation wakes every hub, but each hub only re-fetches
    the affected Vault entry, not the full registry).
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    kind: Literal["kb-source", "mcp-server"]
    name: str
    # Path under {vault_kv_mount}/data/ — e.g. "registry/mcp-server/graylog-mcp/config"
    config_vault_path: str
