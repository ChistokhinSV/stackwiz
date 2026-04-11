"""Tests for the discovery probe ladder — DNS and HTTP are mocked."""
from __future__ import annotations

import httpx
import pytest

import stackwiz.discovery as discovery
from stackwiz.discovery import Source, probe_consul, probe_vault


@pytest.fixture(autouse=True)
def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONSUL_HTTP_ADDR", raising=False)
    monkeypatch.delenv("VAULT_ADDR", raising=False)


def _install_http_mock(monkeypatch: pytest.MonkeyPatch, reachable: set[str]) -> None:
    """Replace httpx.AsyncClient.get with a predicate on the URL."""

    async def fake_get(
        self: httpx.AsyncClient, url: str, *a: object, **kw: object
    ) -> httpx.Response:
        for prefix in reachable:
            if url.startswith(prefix):
                return httpx.Response(200, text="ok")
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


def _install_dns_mock(monkeypatch: pytest.MonkeyPatch, resolvable: set[str]) -> None:
    monkeypatch.setattr(discovery, "_dns_resolves", lambda host: host in resolvable)


async def test_consul_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONSUL_HTTP_ADDR", "10.0.0.5:8500")
    _install_http_mock(monkeypatch, {"http://10.0.0.5:8500"})
    _install_dns_mock(monkeypatch, set())
    r = await probe_consul("example.internal")
    assert r.source is Source.ENV
    assert r.address == "http://10.0.0.5:8500"


async def test_consul_domain_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_mock(monkeypatch, {"http://consul.example.internal:8500"})
    _install_dns_mock(monkeypatch, {"consul.example.internal"})
    r = await probe_consul("example.internal")
    assert r.source is Source.DOMAIN
    assert r.address == "http://consul.example.internal:8500"


async def test_consul_domain_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_mock(monkeypatch, {"http://c.custom.lan:8500"})
    _install_dns_mock(monkeypatch, {"c.custom.lan"})
    r = await probe_consul("example.internal", host_override="c.custom.lan")
    assert r.source is Source.DOMAIN
    assert r.address == "http://c.custom.lan:8500"


async def test_consul_falls_back_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_mock(monkeypatch, {"http://127.0.0.1:8500"})
    _install_dns_mock(monkeypatch, set())
    r = await probe_consul("example.internal")
    assert r.source is Source.LOCALHOST


async def test_consul_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_mock(monkeypatch, set())
    _install_dns_mock(monkeypatch, set())
    r = await probe_consul("example.internal")
    assert r.source is Source.MISSING
    assert not r.reachable


async def test_vault_domain_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_mock(monkeypatch, {"http://vault.example.internal:8200"})
    _install_dns_mock(monkeypatch, {"vault.example.internal"})
    r = await probe_vault("example.internal")
    assert r.source is Source.DOMAIN
    assert r.address == "http://vault.example.internal:8200"


async def test_vault_env_with_explicit_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_ADDR", "https://vault.prod.lan:8200")
    _install_http_mock(monkeypatch, {"https://vault.prod.lan:8200"})
    _install_dns_mock(monkeypatch, set())
    r = await probe_vault("example.internal")
    assert r.source is Source.ENV
    assert r.address == "https://vault.prod.lan:8200"


async def test_vault_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_mock(monkeypatch, set())
    _install_dns_mock(monkeypatch, set())
    r = await probe_vault("example.internal")
    assert r.source is Source.MISSING
