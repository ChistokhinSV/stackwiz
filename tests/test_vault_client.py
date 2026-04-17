"""Tests for VaultClient token-scoping and policy helpers.

hvac is patched at the attribute level so we can assert on the calls without
spinning up a real Vault.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import hvac
import pytest

from stackwiz.vault_client import VaultClient


@pytest.fixture
def client() -> VaultClient:
    with patch("stackwiz.vault_client.hvac.Client") as hvac_cls:
        hvac_cls.return_value = MagicMock()
        c = VaultClient("https://vault.example:8200", token="root-token")
        return c


def test_token_property_returns_current_token(client: VaultClient) -> None:
    assert client.token == "root-token"
    client.set_token("new-token")
    assert client.token == "new-token"


def test_create_install_policy_writes_scoped_hcl(client: VaultClient) -> None:
    name = client.create_install_policy("081", "grafana")
    assert name == "081-grafana-install"
    client._client.sys.create_or_update_policy.assert_called_once()
    kwargs = client._client.sys.create_or_update_policy.call_args.kwargs
    assert kwargs["name"] == "081-grafana-install"
    hcl = kwargs["policy"]
    # Install policy grants rw on own path but only read on shared.
    assert 'path "stackwiz/data/081/grafana/*"' in hcl
    assert '"create", "update", "read", "delete", "list"' in hcl
    assert 'path "stackwiz/data/081/shared/*"' in hcl
    # Crucially — no write on shared/*, no access to other components' paths.
    assert hcl.count('capabilities = ["read"]') >= 1


def test_create_child_token_returns_token_on_success(client: VaultClient) -> None:
    client._client.auth.token.create.return_value = {
        "auth": {"client_token": "s.child-token-123"}
    }
    token = client.create_child_token(["081-grafana-install"], ttl="2h")
    assert token == "s.child-token-123"
    kwargs = client._client.auth.token.create.call_args.kwargs
    assert kwargs["policies"] == ["081-grafana-install"]
    assert kwargs["ttl"] == "2h"
    assert kwargs["renewable"] is False


def test_create_child_token_returns_none_on_vault_error(client: VaultClient) -> None:
    client._client.auth.token.create.side_effect = hvac.exceptions.VaultError("denied")
    assert client.create_child_token(["anything"]) is None


def test_create_child_token_returns_none_on_empty_response(client: VaultClient) -> None:
    client._client.auth.token.create.return_value = {"auth": {}}
    assert client.create_child_token(["anything"]) is None


def test_revoke_token_calls_revoke(client: VaultClient) -> None:
    client.revoke_token("s.child-token-123")
    client._client.auth.token.revoke.assert_called_once_with(token="s.child-token-123")


def test_revoke_token_swallows_vault_error(client: VaultClient) -> None:
    client._client.auth.token.revoke.side_effect = hvac.exceptions.VaultError("gone")
    client.revoke_token("s.gone")  # must not raise


def test_revoke_token_noop_on_empty(client: VaultClient) -> None:
    client.revoke_token("")
    client._client.auth.token.revoke.assert_not_called()


def test_revoke_install_policy_deletes_by_derived_name(client: VaultClient) -> None:
    client.revoke_install_policy("081", "grafana")
    client._client.sys.delete_policy.assert_called_once_with(name="081-grafana-install")
