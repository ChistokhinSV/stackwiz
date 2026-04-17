"""Tests for Engine's extracted helpers — the parts that can be unit-tested
without spinning up a real Docker + Vault + Consul stack.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stackwiz.engine import Engine
from stackwiz.executor import Executor
from stackwiz.manifest import load_manifest
from stackwiz.state import State

FIXTURE = Path(__file__).parent / "manifest_valid.yaml"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    target = tmp_path / "components.yaml"
    target.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    manifest = load_manifest(target)
    state = State(tmp_path / "state")
    executor = Executor(manifest_dir=tmp_path, mode="direct")
    return Engine(manifest=manifest, state=state, executor=executor)


# --- _resolve_node_ip -------------------------------------------------------


def test_node_ip_prefers_node_ip_key(engine: Engine) -> None:
    assert engine._resolve_node_ip({"node_ip": "10.0.0.5"}) == "10.0.0.5"


def test_node_ip_falls_back_to_internal_ip_suffix(engine: Engine) -> None:
    assert engine._resolve_node_ip({"consul_internal_ip": "172.16.1.4"}) == "172.16.1.4"


def test_node_ip_ignores_empty_values(engine: Engine) -> None:
    assert engine._resolve_node_ip(
        {"node_ip": "", "other_internal_ip": "192.168.1.10"}
    ) == "192.168.1.10"


def test_node_ip_defaults_to_loopback(engine: Engine) -> None:
    assert engine._resolve_node_ip({}) == "127.0.0.1"


# --- _mint_install_token ----------------------------------------------------


def test_mint_install_token_returns_none_without_vault(engine: Engine) -> None:
    assert engine.vault is None
    component = engine.manifest.topo_order()[0]
    assert engine._mint_install_token(component) is None


def test_mint_install_token_returns_none_for_vault_component(engine: Engine) -> None:
    # Attach a fake vault backend but hit the hardcoded "vault" short-circuit.
    fake = MagicMock()
    fake.token = "root"
    engine.vault = fake
    # Fabricate a component with id "vault".
    vault_component = MagicMock()
    vault_component.id = "vault"
    assert engine._mint_install_token(vault_component) is None
    # Policy / token creation must not have been attempted.
    fake.create_install_policy.assert_not_called()
    fake.create_child_token.assert_not_called()


def test_mint_install_token_falls_back_on_policy_error(engine: Engine) -> None:
    fake = MagicMock()
    fake.token = "root"
    fake.create_install_policy.side_effect = RuntimeError("denied")
    engine.vault = fake
    component = engine.manifest.topo_order()[0]
    assert engine._mint_install_token(component) is None


def test_mint_install_token_success(engine: Engine) -> None:
    fake = MagicMock()
    fake.token = "root"
    fake.create_install_policy.return_value = "example-k3s-install"
    fake.create_child_token.return_value = "s.child123"
    engine.vault = fake
    component = engine.manifest.topo_order()[0]
    assert engine._mint_install_token(component) == "s.child123"
    fake.create_child_token.assert_called_once()
    kwargs = fake.create_child_token.call_args.kwargs
    assert kwargs["policies"] == ["example-k3s-install"]
    assert kwargs["display_name"].startswith("stackwiz-")


# --- _register_component_services (idempotent primitive) --------------------


def test_register_services_noop_without_consul(engine: Engine) -> None:
    component = engine.manifest.topo_order()[0]
    engine._register_component_services(component)  # no consul → quietly returns


def test_register_services_skips_existing(engine: Engine) -> None:
    component = engine.manifest.topo_order()[0]  # k3s
    fake = MagicMock()
    fake.discover.return_value = object()  # already registered
    engine.consul = fake
    engine._node_ip = "127.0.0.1"
    engine._register_component_services(component)
    fake.register_service.assert_not_called()


def test_register_services_registers_missing(engine: Engine) -> None:
    component = engine.manifest.topo_order()[0]  # k3s, has a consul_service
    fake = MagicMock()
    fake.discover.return_value = None
    engine.consul = fake
    engine._node_ip = "10.0.0.5"
    engine._register_component_services(component)
    fake.register_service.assert_called_once()
    kwargs = fake.register_service.call_args.kwargs
    assert kwargs["node_address"] == "10.0.0.5"


def test_register_services_swallows_discover_errors(engine: Engine) -> None:
    component = engine.manifest.topo_order()[0]
    fake = MagicMock()
    fake.discover.side_effect = RuntimeError("conn refused")
    engine.consul = fake
    engine._node_ip = "127.0.0.1"
    # Must not raise; skips registration when discover fails.
    engine._register_component_services(component)
    fake.register_service.assert_not_called()


def test_register_services_force_skips_discover_and_always_registers(
    engine: Engine,
) -> None:
    """force=True must re-register even when the service is already in the
    catalog — that's how a manifest check-config edit lands on re-run."""
    component = engine.manifest.topo_order()[0]  # k3s
    fake = MagicMock()
    fake.discover.return_value = object()  # already registered
    engine.consul = fake
    engine._node_ip = "10.0.0.5"
    engine._register_component_services(component, force=True)
    fake.discover.assert_not_called()  # skipped entirely under force
    fake.register_service.assert_called_once()


# --- _catchup_service_policies ---------------------------------------------


def test_catchup_policies_noop_without_vault(engine: Engine) -> None:
    engine._catchup_service_policies()  # must not raise


def test_catchup_policies_reapplies_for_installed(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_vault = MagicMock()
    engine.vault = fake_vault
    # Pretend the k3s component is already installed.
    installed = {"k3s": MagicMock(version="1.30.0", config_hash="h")}
    monkeypatch.setattr(engine.state, "installed", lambda: installed)
    engine._catchup_service_policies()
    fake_vault.apply_service_policy.assert_called_once_with("example", "k3s")


def test_catchup_policies_swallows_errors(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_vault = MagicMock()
    fake_vault.apply_service_policy.side_effect = RuntimeError("vault down")
    engine.vault = fake_vault
    installed = {"k3s": MagicMock(version="1.30.0", config_hash="h")}
    monkeypatch.setattr(engine.state, "installed", lambda: installed)
    # Must not raise.
    engine._catchup_service_policies()


# --- _kv_payload (publishes: filter) ----------------------------------------


def test_kv_payload_empty_by_default(engine: Engine) -> None:
    """Components without `publishes:` publish nothing (not everything)."""
    component = engine.manifest.topo_order()[0]  # k3s — no publishes in fixture
    assert engine._kv_payload({"a": 1, "b": 2, "domain": "x"}, component) == {}


def test_kv_payload_filters_to_publishes(engine: Engine) -> None:
    component = engine.manifest.topo_order()[0]
    # Manifest fixture doesn't declare publishes, so inject it on the model.
    published = component.model_copy(update={"publishes": ["a", "domain"]})
    config = {"a": 1, "b": 2, "domain": "example.com"}
    assert engine._kv_payload(config, published) == {
        "a": 1, "domain": "example.com",
    }


def test_kv_payload_missing_key_silently_skipped(engine: Engine) -> None:
    component = engine.manifest.topo_order()[0]
    published = component.model_copy(update={"publishes": ["a", "missing"]})
    assert engine._kv_payload({"a": 1}, published) == {"a": 1}
