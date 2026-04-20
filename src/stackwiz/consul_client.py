"""Consul wrapper: service register/deregister + non-secret KV config."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import consul

from stackwiz.backends.common import resolve_backend_timeout
from stackwiz.manifest import Component, ConsulService

log = logging.getLogger("stackwiz.consul")


@dataclass
class CatalogEntry:
    name: str
    address: str
    port: int
    tags: list[str]


class ConsulClient:
    """Thin facade around python-consul2 for the operations stackwiz needs.

    ``is_local_native_agent`` signals that the consul agent this client
    talks to is a **native process on the same host** as the services
    being registered (e.g. 079's stackwiz-consul-agent systemd unit, or
    a host-network consul container). When True, the check-target
    rewrite in register_service is a no-op — 127.0.0.1 in a service
    check already points to the same loopback the agent will probe.
    Default False matches the historical case of a containerized
    consul-server whose netns isolates it from host loopback.
    """

    def __init__(
        self,
        address: str,
        token: str | None = None,
        is_local_native_agent: bool = False,
    ) -> None:
        self.address = address.rstrip("/")
        self._token = token
        self.is_local_native_agent = is_local_native_agent
        parsed = urlparse(address if "://" in address else f"http://{address}")
        self._client = consul.Consul(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 8500,
            scheme=parsed.scheme or "http",
            token=token,
            timeout=resolve_backend_timeout(),
        )

    @property
    def token(self) -> str | None:
        return self._token

    # --- health / probe ---------------------------------------------------------

    def leader(self) -> str | None:
        try:
            return self._client.status.leader()
        except Exception as exc:  # noqa: BLE001 — connection errors → "no leader"
            log.debug("consul leader lookup failed: %s", exc)
            return None

    # --- service registration ---------------------------------------------------

    def register_service(
        self,
        component: Component,
        service: ConsulService | None = None,
        node_address: str = "127.0.0.1",
    ) -> None:
        svc = service or component.consul_service
        if svc is None:
            return
        check = None
        if svc.check is not None:
            # Replace 127.0.0.1 in check URLs with the actual node address
            # so a REMOTE or CONTAINERIZED consul agent can reach the
            # service over the LAN. With a native local agent (systemd
            # unit on the same host), 127.0.0.1 already maps to the same
            # loopback the service binds, so no rewrite.
            def _rewrite(url: str) -> str:
                if self.is_local_native_agent:
                    return url
                if node_address != "127.0.0.1":
                    return url.replace("127.0.0.1", node_address)
                return url

            if svc.check.http:
                check = consul.Check.http(
                    _rewrite(svc.check.http),
                    interval=svc.check.interval,
                    timeout=svc.check.timeout,
                    tls_skip_verify=(
                        svc.check.tls_skip_verify or None
                    ),
                )
            elif svc.check.tcp:
                tcp = _rewrite(svc.check.tcp)
                check = consul.Check.tcp(
                    tcp.split(":")[0],
                    int(tcp.split(":")[1]),
                    interval=svc.check.interval,
                    timeout=svc.check.timeout,
                )
        # Address advertised in the catalog. Operators override via
        # manifest's consul_service.address to publish a public hostname
        # (resolved through nginx) instead of the node's internal IP —
        # consumers discovering the service via Consul get the
        # externally-reachable address. Falls back to node IP when not
        # set (legacy behaviour).
        register_address = svc.address or node_address
        self._client.agent.service.register(
            name=svc.name,
            service_id=f"{svc.name}-{component.id}",
            address=register_address,
            port=svc.port,
            tags=list(svc.tags),
            meta=dict(svc.meta) if svc.meta else None,
            check=check,
            token=self._token,
        )

    def deregister_service(
        self,
        component: Component,
        service: ConsulService | None = None,
    ) -> None:
        candidates = (
            [service] if service is not None else component.all_consul_services()
        )
        for svc in candidates:
            try:
                self._client.agent.service.deregister(
                    f"{svc.name}-{component.id}", token=self._token,
                )
            except Exception as exc:  # noqa: BLE001 — idempotent teardown
                log.debug(
                    "consul deregister %s-%s: %s",
                    svc.name, component.id, exc,
                )

    def discover(self, service: ConsulService | str) -> CatalogEntry | None:
        name = service.name if isinstance(service, ConsulService) else service
        _, nodes = self._client.catalog.service(name, token=self._token)
        if not nodes:
            return None
        node = nodes[0]
        return CatalogEntry(
            name=name,
            address=node.get("ServiceAddress") or node.get("Address", ""),
            port=int(node.get("ServicePort", 0)),
            tags=list(node.get("ServiceTags") or []),
        )

    # --- KV (non-secret config) -------------------------------------------------

    def kv_put(self, key: str, value: str) -> None:
        self._client.kv.put(key, value, token=self._token)

    def kv_get(self, key: str) -> str | None:
        _, data = self._client.kv.get(key, token=self._token)
        if data is None:
            return None
        val = data.get("Value")
        return val.decode("utf-8") if isinstance(val, (bytes, bytearray)) else val

    def kv_delete_tree(self, prefix: str) -> None:
        self._client.kv.delete(prefix, recurse=True, token=self._token)
