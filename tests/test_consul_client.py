"""Tests for ConsulClient token passthrough + error tolerance."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from stackwiz.consul_client import ConsulClient


@pytest.fixture
def client() -> ConsulClient:
    with patch("stackwiz.consul_client.consul.Consul") as consul_cls:
        consul_cls.return_value = MagicMock()
        return ConsulClient("http://consul.example:8500", token="acl-token")


def test_token_property_exposes_constructor_token(client: ConsulClient) -> None:
    assert client.token == "acl-token"


def test_constructor_passes_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STACKWIZ_BACKEND_TIMEOUT", "9")
    with patch("stackwiz.consul_client.consul.Consul") as consul_cls:
        consul_cls.return_value = MagicMock()
        ConsulClient("http://consul.example:8500", token="t")
        assert consul_cls.call_args.kwargs["timeout"] == 9.0


def test_constructor_parses_address_components() -> None:
    with patch("stackwiz.consul_client.consul.Consul") as consul_cls:
        consul_cls.return_value = MagicMock()
        ConsulClient("https://consul.lab.internal:9443", token=None)
        kwargs = consul_cls.call_args.kwargs
        assert kwargs["host"] == "consul.lab.internal"
        assert kwargs["port"] == 9443
        assert kwargs["scheme"] == "https"


def test_leader_returns_none_on_exception(client: ConsulClient) -> None:
    client._client.status.leader.side_effect = RuntimeError("unreachable")
    assert client.leader() is None


def test_deregister_service_swallows_errors(client: ConsulClient) -> None:
    client._client.agent.service.deregister.side_effect = RuntimeError("gone")
    component = MagicMock()
    svc = MagicMock()
    svc.name = "foo"
    component.id = "bar"
    component.all_consul_services.return_value = [svc]
    # Must not raise (idempotent teardown).
    client.deregister_service(component)


def test_kv_put_forwards_token(client: ConsulClient) -> None:
    client.kv_put("x/y", "value")
    client._client.kv.put.assert_called_once_with("x/y", "value", token="acl-token")


def test_kv_get_returns_string_when_found(client: ConsulClient) -> None:
    client._client.kv.get.return_value = (0, {"Value": b"hello"})
    assert client.kv_get("x/y") == "hello"


def test_kv_get_returns_none_when_missing(client: ConsulClient) -> None:
    client._client.kv.get.return_value = (0, None)
    assert client.kv_get("x/y") is None


def test_register_service_forwards_tls_skip_verify(
    client: ConsulClient,
) -> None:
    """tls_skip_verify must reach python-consul2's Check.http so the
    consul agent's HTTP probe skips SAN verification against a cert
    the agent can't validate (e.g. vault cert SAN != docker hostname)."""
    from stackwiz.manifest import ConsulService, ConsulServiceCheck

    component = MagicMock()
    component.id = "vault"
    svc = ConsulService(
        name="vault", port=8200,
        check=ConsulServiceCheck(
            http="https://vault:8200/v1/sys/health",
            interval="30s",
            tls_skip_verify=True,
        ),
    )
    client.register_service(component, svc, node_address="10.0.0.1")
    kwargs = client._client.agent.service.register.call_args.kwargs
    check = kwargs["check"]
    assert check.get("TLSSkipVerify") is True, check


def test_register_service_no_tls_skip_verify_by_default(
    client: ConsulClient,
) -> None:
    from stackwiz.manifest import ConsulService, ConsulServiceCheck

    component = MagicMock()
    component.id = "foo"
    svc = ConsulService(
        name="foo", port=1234,
        check=ConsulServiceCheck(http="http://foo:1234/ping", interval="30s"),
    )
    client.register_service(component, svc, node_address="127.0.0.1")
    kwargs = client._client.agent.service.register.call_args.kwargs
    check = kwargs["check"]
    # When the operator didn't opt in, the key should be absent entirely —
    # a False value would still skip the check in some python-consul2
    # branches; the safe default is "do not emit the key".
    assert "TLSSkipVerify" not in check, check


def test_tls_skip_verify_requires_http() -> None:
    """tls_skip_verify is nonsense on a TCP check — schema should reject it."""
    from pydantic import ValidationError

    from stackwiz.manifest import ConsulServiceCheck

    with pytest.raises(ValidationError, match="tls_skip_verify"):
        ConsulServiceCheck(tcp="127.0.0.1:161", tls_skip_verify=True)
