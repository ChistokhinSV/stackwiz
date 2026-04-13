"""Consul wrapper: service register/deregister + non-secret KV config."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import consul

from stackwiz.manifest import Component, ConsulService


@dataclass
class CatalogEntry:
    name: str
    address: str
    port: int
    tags: list[str]


class ConsulClient:
    """Thin facade around python-consul2 for the operations stackwiz needs."""

    def __init__(self, address: str, token: str | None = None) -> None:
        self.address = address.rstrip("/")
        parsed = urlparse(address if "://" in address else f"http://{address}")
        self._client = consul.Consul(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 8500,
            scheme=parsed.scheme or "http",
            token=token,
        )

    # --- health / probe ---------------------------------------------------------

    def leader(self) -> str | None:
        try:
            return self._client.status.leader()
        except Exception:  # noqa: BLE001 — connection errors become "no leader"
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
            # so Consul agent can reach the service on remote hosts.
            def _rewrite(url: str) -> str:
                if node_address != "127.0.0.1":
                    return url.replace("127.0.0.1", node_address)
                return url

            if svc.check.http:
                check = consul.Check.http(
                    _rewrite(svc.check.http),
                    interval=svc.check.interval,
                    timeout=svc.check.timeout,
                )
            elif svc.check.tcp:
                tcp = _rewrite(svc.check.tcp)
                check = consul.Check.tcp(
                    tcp.split(":")[0],
                    int(tcp.split(":")[1]),
                    interval=svc.check.interval,
                    timeout=svc.check.timeout,
                )
        self._client.agent.service.register(
            name=svc.name,
            service_id=f"{svc.name}-{component.id}",
            address=node_address,
            port=svc.port,
            tags=list(svc.tags),
            meta=dict(svc.meta) if svc.meta else None,
            check=check,
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
                self._client.agent.service.deregister(f"{svc.name}-{component.id}")
            except Exception:  # noqa: BLE001 — idempotent teardown
                pass

    def discover(self, service: ConsulService | str) -> CatalogEntry | None:
        name = service.name if isinstance(service, ConsulService) else service
        _, nodes = self._client.catalog.service(name)
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
        self._client.kv.put(key, value)

    def kv_get(self, key: str) -> str | None:
        _, data = self._client.kv.get(key)
        if data is None:
            return None
        val = data.get("Value")
        return val.decode("utf-8") if isinstance(val, (bytes, bytearray)) else val

    def kv_delete_tree(self, prefix: str) -> None:
        self._client.kv.delete(prefix, recurse=True)
