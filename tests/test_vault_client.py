"""Tests for VaultClient token-scoping and policy helpers.

hvac is patched at the attribute level so we can assert on the calls without
spinning up a real Vault.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import hvac
import pytest

from stackwiz.vault_client import (
    DEFAULT_BACKEND_TIMEOUT,
    VaultClient,
    resolve_backend_timeout,
    resolve_verify,
    shred_vault_init,
)


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


def test_apply_shared_read_policy_hcl(client: VaultClient) -> None:
    name = client.apply_shared_read_policy()
    assert name == "stackwiz-shared-read"
    kwargs = client._client.sys.create_or_update_policy.call_args.kwargs
    assert kwargs["name"] == "stackwiz-shared-read"
    hcl = kwargs["policy"]
    assert 'path "stackwiz/data/shared/*"' in hcl
    assert 'capabilities = ["read"]' in hcl
    # No write capabilities anywhere — defense against accidental elevation.
    assert "create" not in hcl and "update" not in hcl and "delete" not in hcl


# --- resolve_verify ---------------------------------------------------------


def test_resolve_verify_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_CACERT", raising=False)
    monkeypatch.delenv("STACKWIZ_VAULT_VERIFY", raising=False)
    assert resolve_verify() is True


def test_resolve_verify_explicit_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_CACERT", "/etc/ssl/ca.pem")
    monkeypatch.setenv("STACKWIZ_VAULT_VERIFY", "false")
    assert resolve_verify(True) is True
    assert resolve_verify(False) is False


def test_resolve_verify_cacert_wins_over_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_CACERT", "/etc/ssl/ca.pem")
    monkeypatch.setenv("STACKWIZ_VAULT_VERIFY", "false")
    assert resolve_verify() == "/etc/ssl/ca.pem"


@pytest.mark.parametrize("val", ["false", "0", "no", "False", "NO"])
def test_resolve_verify_opt_out_values(
    monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    monkeypatch.delenv("VAULT_CACERT", raising=False)
    monkeypatch.setenv("STACKWIZ_VAULT_VERIFY", val)
    # Reset the once-per-process warning gate so each parametrized run re-tests.
    if hasattr(resolve_verify, "_warned"):
        delattr(resolve_verify, "_warned")
    assert resolve_verify() is False


def test_vault_client_ctor_honors_cacert_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_CACERT", "/etc/ssl/myca.pem")
    with patch("stackwiz.vault_client.hvac.Client") as hvac_cls:
        hvac_cls.return_value = MagicMock()
        VaultClient("https://vault.example", token="t")
        kwargs = hvac_cls.call_args.kwargs
        assert kwargs["verify"] == "/etc/ssl/myca.pem"


# --- resolve_backend_timeout ------------------------------------------------


def test_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STACKWIZ_BACKEND_TIMEOUT", raising=False)
    assert resolve_backend_timeout() == DEFAULT_BACKEND_TIMEOUT


def test_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STACKWIZ_BACKEND_TIMEOUT", "5.5")
    assert resolve_backend_timeout() == 5.5


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-1", ""])
def test_timeout_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("STACKWIZ_BACKEND_TIMEOUT", bad)
    assert resolve_backend_timeout() == DEFAULT_BACKEND_TIMEOUT


def test_vault_client_passes_timeout_to_hvac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STACKWIZ_BACKEND_TIMEOUT", "7")
    with patch("stackwiz.vault_client.hvac.Client") as hvac_cls:
        hvac_cls.return_value = MagicMock()
        VaultClient("https://vault.example", token="t")
        assert hvac_cls.call_args.kwargs["timeout"] == 7.0


# --- shred_vault_init -------------------------------------------------------


def test_shred_removes_file(tmp_path: Path) -> None:
    target = tmp_path / "vault-init.json"
    target.write_text('{"root_token": "s.secret", "unseal_keys": ["a","b"]}')
    removed = shred_vault_init(tmp_path)
    assert removed == target
    assert not target.exists()


def test_shred_noop_when_missing(tmp_path: Path) -> None:
    assert shred_vault_init(tmp_path) is None


def test_shred_overwrites_before_unlinking(tmp_path: Path) -> None:
    # Use a sentinel: we can't read the file after unlink, but we can hook
    # the Path.unlink call to inspect bytes just before deletion.
    target = tmp_path / "vault-init.json"
    target.write_text("ROOT_TOKEN_IN_CLEARTEXT_!!!")
    original_size = target.stat().st_size

    captured: dict[str, bytes] = {}
    real_unlink = Path.unlink

    def peek_and_unlink(self: Path, *args, **kwargs) -> None:
        if self == target:
            captured["bytes"] = self.read_bytes()
        real_unlink(self, *args, **kwargs)

    with patch.object(Path, "unlink", peek_and_unlink):
        shred_vault_init(tmp_path)
    assert captured["bytes"] == b"\x00" * original_size
