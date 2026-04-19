"""Tests for the declarative registry (Phase 1 of the hub rework).

Covers:
  * RegistryEntry field validation (kind enum, name slug, defaults).
  * Manifest-root validation: bearer_secret refs a declared secret,
    (kind, name) pairs are unique across the manifest.
  * Engine `_publish_registry`: Vault config+token writes, Consul KV
    pointer mirror, behaviour when registry is empty, when Vault /
    Consul clients are None.

These are pure-unit tests — the engine is exercised via mocks so the
tests stay fast + independent of any live Consul/Vault.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from stackwiz.manifest import (
    Component,
    ConsulConfig,
    Manifest,
    RegistryEntry,
    Secret,
)
from stackwiz.secrets import MaterializedSecret


# --- RegistryEntry field validation -----------------------------------------


def test_registry_entry_defaults() -> None:
    r = RegistryEntry(
        kind="mcp-server",
        name="graylog-mcp",
        endpoint_url="http://graylog-mcp:8000/mcp",
    )
    assert r.transport == "http"
    assert r.paths == {}
    assert r.bearer_secret is None
    assert r.tags == []
    assert r.description == ""


def test_registry_entry_kind_enum() -> None:
    with pytest.raises(ValidationError):
        RegistryEntry(kind="bogus", name="x", endpoint_url="http://x")


@pytest.mark.parametrize("bad", ["bad name", "has space", "", "..", "x/y"])
def test_registry_entry_name_slug_rejects_bad(bad: str) -> None:
    with pytest.raises(ValidationError):
        RegistryEntry(kind="kb-source", name=bad, endpoint_url="http://x")


@pytest.mark.parametrize("good", ["graylog-mcp", "awx_mcp", "Config-Analyzer", "a"])
def test_registry_entry_name_slug_accepts_good(good: str) -> None:
    RegistryEntry(kind="kb-source", name=good, endpoint_url="http://x")


# --- Manifest-root validation ----------------------------------------------


def _make_manifest(**component_kwargs) -> Manifest:
    """Build a minimal manifest with a single component + optional secrets."""
    secrets = component_kwargs.pop("manifest_secrets", [])
    return Manifest(
        name="test-stack",
        display_name="Test Stack",
        version="1.0.0",
        domain="test.local",
        consul=ConsulConfig(required=True, service_prefix="test"),
        components=[
            Component(
                id="c1",
                name="Component 1",
                install="install/c1.sh",
                **component_kwargs,
            ),
        ],
        secrets=secrets,
    )


def test_manifest_bearer_secret_must_be_declared() -> None:
    with pytest.raises(ValidationError, match="bearer_secret"):
        _make_manifest(
            registry=[RegistryEntry(
                kind="mcp-server",
                name="my-mcp",
                endpoint_url="http://x:8080/mcp",
                bearer_secret="not_declared",
            )],
        )


def test_manifest_bearer_secret_declared_passes() -> None:
    m = _make_manifest(
        manifest_secrets=[Secret(id="my_bearer", generate=True, length=32)],
        registry=[RegistryEntry(
            kind="mcp-server",
            name="my-mcp",
            endpoint_url="http://x:8080/mcp",
            bearer_secret="my_bearer",
        )],
    )
    assert m.components[0].registry[0].bearer_secret == "my_bearer"


def test_manifest_duplicate_kind_name_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate registry entry"):
        _make_manifest(
            registry=[
                RegistryEntry(kind="mcp-server", name="dup", endpoint_url="http://x"),
                RegistryEntry(kind="mcp-server", name="dup", endpoint_url="http://y"),
            ],
        )


def test_manifest_same_name_different_kind_allowed() -> None:
    """A kb-source and an mcp-server can share a name — keyed by (kind,name)."""
    m = _make_manifest(
        registry=[
            RegistryEntry(kind="mcp-server", name="graylog", endpoint_url="http://x"),
            RegistryEntry(kind="kb-source", name="graylog", endpoint_url="http://x"),
        ],
    )
    assert len(m.components[0].registry) == 2


# --- Engine _publish_registry ----------------------------------------------


def _component_with_registry(entries: list[RegistryEntry]) -> Component:
    return Component(
        id="graylog_mcp",
        name="Graylog MCP",
        install="install/graylog-mcp.sh",
        registry=entries,
    )


def _engine_with_stubs() -> MagicMock:
    """Build a tiny shim matching what `_publish_registry` reads off self."""
    from stackwiz.engine import Engine

    engine = Engine.__new__(Engine)  # don't run __init__; we stub fields.
    engine.manifest = MagicMock(name="manifest")
    engine.manifest.name = "081.consul_vault_authentik_docker"
    engine.vault = MagicMock(name="vault")
    engine.consul = MagicMock(name="consul")
    return engine


def test_publish_registry_writes_config_and_token() -> None:
    engine = _engine_with_stubs()
    component = _component_with_registry([
        RegistryEntry(
            kind="mcp-server",
            name="graylog-mcp",
            endpoint_url="http://graylog-mcp:8000/mcp",
            transport="streamable_http",
            bearer_secret="mcp_bearer",
            tags=["logging"],
            description="Graylog log queries",
        ),
    ])
    materialized = {
        "mcp_bearer": MaterializedSecret(
            id="mcp_bearer",
            vault_path="prod/mcp_bearer",
            value="s3cret",
            regenerated=True,
        ),
    }
    engine._publish_registry(component, materialized)

    # Vault: config + token written.
    calls = {c.args[0]: c.args[1] for c in engine.vault.kv_put.call_args_list}
    assert "registry/mcp-server/graylog-mcp/config" in calls
    assert "registry/mcp-server/graylog-mcp/token" in calls
    cfg_doc = json.loads(calls["registry/mcp-server/graylog-mcp/config"]["value"])
    assert cfg_doc["kind"] == "mcp-server"
    assert cfg_doc["name"] == "graylog-mcp"
    assert cfg_doc["owner"] == "081.consul_vault_authentik_docker"
    assert cfg_doc["endpoint"]["url"] == "http://graylog-mcp:8000/mcp"
    assert cfg_doc["endpoint"]["transport"] == "streamable_http"
    assert cfg_doc["auth"]["mode"] == "bearer"
    assert cfg_doc["auth"]["token_ref"] == "registry/mcp-server/graylog-mcp/token"
    assert cfg_doc["tags"] == ["logging"]
    assert cfg_doc["description"] == "Graylog log queries"
    assert calls["registry/mcp-server/graylog-mcp/token"]["value"] == "s3cret"

    # Consul KV: pointer mirror.
    engine.consul.kv_put.assert_called_once()
    ck_args = engine.consul.kv_put.call_args.args
    assert ck_args[0] == "stackwiz/registry/mcp-server/graylog-mcp"
    pointer = json.loads(ck_args[1])
    assert pointer["kind"] == "mcp-server"
    assert pointer["name"] == "graylog-mcp"
    assert pointer["config_vault_path"] == "registry/mcp-server/graylog-mcp/config"


def test_publish_registry_anonymous_entry_omits_token() -> None:
    engine = _engine_with_stubs()
    component = _component_with_registry([
        RegistryEntry(
            kind="kb-source",
            name="readonly-kb",
            endpoint_url="http://public:8080",
            paths={"pull": "/.kb/snapshot", "health": "/.kb/health"},
        ),
    ])
    engine._publish_registry(component, {})

    paths = [c.args[0] for c in engine.vault.kv_put.call_args_list]
    assert "registry/kb-source/readonly-kb/config" in paths
    assert "registry/kb-source/readonly-kb/token" not in paths

    cfg_doc = json.loads(
        engine.vault.kv_put.call_args_list[0].args[1]["value"],
    )
    assert cfg_doc["auth"] == {"mode": "none", "token_ref": None}
    assert cfg_doc["endpoint"]["paths"] == {
        "pull": "/.kb/snapshot",
        "health": "/.kb/health",
    }


def test_publish_registry_empty_is_noop() -> None:
    engine = _engine_with_stubs()
    component = _component_with_registry([])
    engine._publish_registry(component, {})
    engine.vault.kv_put.assert_not_called()
    engine.consul.kv_put.assert_not_called()


def test_publish_registry_skips_consul_when_unavailable() -> None:
    engine = _engine_with_stubs()
    engine.consul = None
    component = _component_with_registry([
        RegistryEntry(
            kind="mcp-server",
            name="x",
            endpoint_url="http://x:8080/mcp",
        ),
    ])
    engine._publish_registry(component, {})
    # Vault still writes; no crash on missing consul.
    assert engine.vault.kv_put.call_count == 1


def test_publish_registry_skips_vault_when_unavailable() -> None:
    engine = _engine_with_stubs()
    engine.vault = None
    component = _component_with_registry([
        RegistryEntry(
            kind="mcp-server",
            name="x",
            endpoint_url="http://x:8080/mcp",
        ),
    ])
    engine._publish_registry(component, {})
    # Consul pointer still written; no crash on missing vault.
    assert engine.consul.kv_put.call_count == 1


def test_publish_registry_vault_failure_does_not_break_other_entries() -> None:
    engine = _engine_with_stubs()
    engine.vault.kv_put.side_effect = [
        None,  # first entry config writes
        Exception("boom"),  # token write fails
        None,  # second entry config writes
        None,  # (no token for entry 2)
    ]
    component = _component_with_registry([
        RegistryEntry(
            kind="mcp-server",
            name="e1",
            endpoint_url="http://x",
            bearer_secret="b1",
        ),
        RegistryEntry(
            kind="mcp-server",
            name="e2",
            endpoint_url="http://y",
        ),
    ])
    materialized = {
        "b1": MaterializedSecret(
            id="b1", vault_path="x", value="v", regenerated=False,
        ),
    }
    # Must not raise; e2 still publishes despite e1's token failure.
    engine._publish_registry(component, materialized)
    # e1's token attempt failed → we continue on in the loop but skip
    # the consul mirror for that entry. e2 publishes fully.
    kinds = [c.args[0] for c in engine.consul.kv_put.call_args_list]
    assert "stackwiz/registry/mcp-server/e2" in kinds


# --- vault_client.apply_hub_reader_policy ----------------------------------


def test_apply_hub_reader_policy_shape() -> None:
    from stackwiz.vault_client import VaultClient

    vc = VaultClient.__new__(VaultClient)  # bypass __init__
    vc._client = MagicMock()
    name = vc.apply_hub_reader_policy()
    assert name == "stackwiz-hub-reader"
    hcl = vc._client.sys.create_or_update_policy.call_args.kwargs["policy"]
    assert "stackwiz/data/registry/*" in hcl
    assert "capabilities = [\"read\"]" in hcl
    assert "stackwiz/metadata/registry/*" in hcl
    # No write: registry is fan-in via project token, not hub token.
    assert "\"create\"" not in hcl
    assert "\"update\"" not in hcl
