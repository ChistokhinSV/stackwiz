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
