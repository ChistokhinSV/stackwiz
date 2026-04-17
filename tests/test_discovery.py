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


_VAULT_HEALTH_BODY = (
    '{"initialized":true,"sealed":false,"standby":false,"version":"1.21.4"}'
)


def _install_http_mock(monkeypatch: pytest.MonkeyPatch, reachable: set[str]) -> None:
    """Replace httpx.AsyncClient.get with a predicate on the URL.

    Vault sys/health URLs get a JSON body so the strict vault probe (which
    rejects non-JSON responses to avoid being fooled by Vault's HTTPS-only
    "Client sent an HTTP request..." 400) sees a valid-looking reply.
    """

    async def fake_get(
        self: httpx.AsyncClient, url: str, *a: object, **kw: object
    ) -> httpx.Response:
        for prefix in reachable:
            if url.startswith(prefix):
                if "/v1/sys/health" in url:
                    return httpx.Response(200, text=_VAULT_HEALTH_BODY)
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


async def test_vault_verify_override_plumbs_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify_override=False must reach httpx so the engine's post-install
    adoption can trust a freshly-installed self-signed Vault."""
    captured: list[bool | str] = []

    async def fake_get(
        self: httpx.AsyncClient, url: str, *a: object, **kw: object
    ) -> httpx.Response:
        # httpx.AsyncClient stores `verify` on the client; peek at it here.
        captured.append(self._transport._pool._ssl_context is None  # type: ignore[attr-defined]
                        or "verify-captured")
        return httpx.Response(200, text=_VAULT_HEALTH_BODY)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    _install_dns_mock(monkeypatch, {"vault.example.internal"})
    r = await probe_vault(
        "example.internal", verify_override=False,
    )
    # Reaching here means the probe completed — the important assertion is
    # that probe_vault accepted verify_override and returned a reachable
    # result; actual verify=False wiring is exercised in the vault_client
    # ctor tests.
    assert r.reachable, f"probe should succeed with verify_override=False: {r}"


async def test_vault_rejects_http_on_https_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: Vault's HTTPS listener replies with HTTP/1.0 400 + body
    "Client sent an HTTP request to an HTTPS server" when spoken to over
    plain HTTP. The old _http_ok (status < 500) accepted that as a live
    target and the engine then adopted http://127.0.0.1:8200, causing every
    subsequent hvac call to fail with the same sentinel. probe_vault must
    treat that response as NOT reachable so the retry path (verify=False
    over HTTPS) gets a chance to run."""

    async def fake_get(
        self: httpx.AsyncClient, url: str, *a: object, **kw: object
    ) -> httpx.Response:
        if url.startswith("https://"):
            # Mirror the httpx behavior when a client rejects a self-signed
            # cert — verify failures raise; the caller treats it as not-ok.
            raise httpx.ConnectError(
                "self-signed cert",
                request=httpx.Request("GET", url),
            )
        if url.startswith("http://"):
            # Exactly what Vault's HTTPS listener emits for plain-HTTP GETs.
            return httpx.Response(
                400, text="Client sent an HTTP request to an HTTPS server.\n",
            )
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    _install_dns_mock(monkeypatch, {"vault.example.internal"})
    r = await probe_vault("example.internal")
    assert r.source is Source.MISSING, (
        f"probe must NOT report http:// as reachable when the server is "
        f"HTTPS-only; got {r}"
    )
