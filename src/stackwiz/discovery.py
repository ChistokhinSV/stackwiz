"""Domain-based probe ladder for Consul and Vault.

The manifest declares a `domain`. The framework tries, in order:
    1. the domain-based hostname (consul.<domain> / vault.<domain>)
    2. 127.0.0.1 on the standard port
    3. offer to install locally

An explicit env override (CONSUL_HTTP_ADDR / VAULT_ADDR) always wins.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum

import dns.exception
import dns.resolver
import httpx

from stackwiz.vault_client import resolve_verify, suppress_insecure_warnings

log = logging.getLogger("stackwiz.discovery")

CONSUL_DEFAULT_PORT = 8500
VAULT_DEFAULT_PORT = 8200


class Source(StrEnum):
    ENV = "env"              # CONSUL_HTTP_ADDR / VAULT_ADDR
    DOMAIN = "domain"        # consul.<domain> / vault.<domain>
    LOCALHOST = "localhost"
    MISSING = "missing"      # not reachable anywhere


@dataclass
class ProbeResult:
    source: Source
    address: str | None        # e.g. "http://consul.example.internal:8500"
    detail: str = ""

    @property
    def reachable(self) -> bool:
        return self.source is not Source.MISSING


def _dns_resolves(host: str) -> bool:
    try:
        dns.resolver.resolve(host, "A", lifetime=2.0)
        return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.Timeout):
        return False
    except dns.exception.DNSException:
        return False


async def _http_ok(
    url: str, timeout: float = 2.5, verify: bool | str = True
) -> bool:
    if verify is False:
        suppress_insecure_warnings()
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
            response = await client.get(url)
        return response.status_code < 500
    except (httpx.RequestError, httpx.HTTPError):
        return False


def _normalize_addr(raw: str, default_port: int, default_scheme: str = "http") -> str:
    addr = raw.strip()
    if not addr:
        return ""
    if "://" not in addr:
        addr = f"{default_scheme}://{addr}"
    if addr.count(":") < 2:
        addr = f"{addr}:{default_port}"
    return addr.rstrip("/")


async def probe_consul(
    domain: str,
    host_override: str | None = None,
) -> ProbeResult:
    """Find a reachable Consul agent.

    Probe order:
        1. `CONSUL_HTTP_ADDR` env
        2. consul.<domain>:8500 (or host_override)
        3. 127.0.0.1:8500
    """
    env_addr = os.environ.get("CONSUL_HTTP_ADDR", "").strip()
    if env_addr:
        addr = _normalize_addr(env_addr, CONSUL_DEFAULT_PORT)
        if await _http_ok(f"{addr}/v1/status/leader"):
            return ProbeResult(Source.ENV, addr, "CONSUL_HTTP_ADDR")

    host = host_override or f"consul.{domain}"
    addr = f"http://{host}:{CONSUL_DEFAULT_PORT}"
    if _dns_resolves(host) and await _http_ok(f"{addr}/v1/status/leader"):
        return ProbeResult(Source.DOMAIN, addr, host)

    addr = f"http://127.0.0.1:{CONSUL_DEFAULT_PORT}"
    if await _http_ok(f"{addr}/v1/status/leader"):
        return ProbeResult(Source.LOCALHOST, addr, "127.0.0.1")

    return ProbeResult(Source.MISSING, None, f"tried {host}, 127.0.0.1")


async def probe_vault(
    domain: str,
    host_override: str | None = None,
) -> ProbeResult:
    """Find a reachable Vault. Probe order mirrors `probe_consul`.

    TLS verification honors STACKWIZ_VAULT_VERIFY / VAULT_CACERT (see
    stackwiz.vault_client.resolve_verify). A token in env with an HTTP
    (plaintext) fallback is a misconfiguration; the caller is warn-logged.
    """
    health = "/v1/sys/health?standbyok=true&sealedok=true"
    verify = resolve_verify()
    has_token = bool(os.environ.get("VAULT_TOKEN", "").strip())

    async def _try(host: str) -> str | None:
        # HTTPS first (verify-aware) — TLS-enabled Vault refuses plain HTTP.
        url = f"https://{host}:{VAULT_DEFAULT_PORT}"
        if await _http_ok(f"{url}{health}", verify=verify):
            return url
        # HTTP fallback only when no token is in scope; otherwise skip so we
        # don't leak the token over plaintext.
        if has_token:
            log.warning(
                "probe_vault: HTTPS to %s failed and VAULT_TOKEN is set — "
                "refusing HTTP fallback to avoid leaking the token over "
                "plaintext.", host,
            )
            return None
        url = f"http://{host}:{VAULT_DEFAULT_PORT}"
        if await _http_ok(f"{url}{health}", verify=False):
            return url
        return None

    env_addr = os.environ.get("VAULT_ADDR", "").strip()
    if env_addr:
        addr = _normalize_addr(env_addr, VAULT_DEFAULT_PORT)
        if await _http_ok(f"{addr}{health}", verify=verify):
            return ProbeResult(Source.ENV, addr, "VAULT_ADDR")

    host = host_override or f"vault.{domain}"
    if _dns_resolves(host):
        addr = await _try(host)
        if addr is not None:
            return ProbeResult(Source.DOMAIN, addr, host)

    addr = await _try("127.0.0.1")
    if addr is not None:
        return ProbeResult(Source.LOCALHOST, addr, "127.0.0.1")

    return ProbeResult(Source.MISSING, None, f"tried {host}, 127.0.0.1")
