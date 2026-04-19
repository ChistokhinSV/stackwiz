"""Settings for stackwiz-hub, sourced from env vars at startup."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Consul (registry source of truth) ------------------------------------
    consul_http_addr: str = Field(
        default="http://consul:8500",
        description="Consul agent address. Hub reads stackwiz/registry/* via blocking queries.",
    )
    consul_token: str = Field(default="", description="Consul ACL token (optional).")
    registry_prefix: str = Field(
        default="stackwiz/registry/",
        description="KV prefix watched for registry entries.",
    )

    # ---- Vault (bearer + config doc resolution) -------------------------------
    vault_addr: str = Field(
        default="",
        description="Vault address. Empty disables Vault lookups (anonymous mode).",
    )
    vault_token: str = Field(
        default="",
        description="Hub's read-only token (policy: stackwiz-hub-reader).",
    )
    vault_kv_mount: str = Field(
        default="stackwiz",
        description="KV v2 mount name. Stackwiz mounts at 'stackwiz/', Vault's default is 'secret/'.",
    )
    vault_verify: bool = Field(
        default=False,
        description="Verify Vault's TLS cert. Default off for self-signed installs.",
    )

    # ---- MCPJungle (MCP registration target) ----------------------------------
    mcpjungle_url: str = Field(
        default="http://mcpjungle:8080",
        description="MCPJungle base URL. Empty disables MCP registration.",
    )

    # ---- KB content sync ------------------------------------------------------
    kb_repo_path: Path = Field(
        default=Path("/data/kb-repo"),
        description="Local kb-repo directory where _sources/<name>/ trees land.",
    )
    kb_commit_author_name: str = Field(default="stackwiz-hub")
    kb_commit_author_email: str = Field(default="stackwiz-hub@local")

    # ---- Reconcile loop -------------------------------------------------------
    reconcile_safety_interval_s: int = Field(
        default=300,
        description="Max time between reconciles even if Consul blocking query doesn't fire.",
    )
    http_timeout_s: int = Field(
        default=60,
        description="Timeout for HTTP fetches from sources + MCPJungle.",
    )

    # ---- Observability --------------------------------------------------------
    log_level: str = "INFO"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
