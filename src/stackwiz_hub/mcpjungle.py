"""Thin MCPJungle REST client — just upsert / delete.

MCPJungle's /api/v0/servers endpoints are dev-mode (no auth on the
MCPJungle side itself; MCPs behind MCPJungle auth via bearer tokens
forwarded through). We POST to create + PUT-idempotent by name on
subsequent calls.
"""
from __future__ import annotations

import logging

import httpx

from stackwiz_hub.models import RegistryDoc

log = logging.getLogger(__name__)


class MCPJungleClient:
    def __init__(self, *, base_url: str, timeout_s: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def list_servers(self) -> list[str]:
        """Return registered server names — used to detect deletes."""
        try:
            resp = self._client.get(f"{self.base_url}/api/v0/servers")
            resp.raise_for_status()
            return [s["name"] for s in resp.json() or []]
        except Exception as exc:  # noqa: BLE001
            log.warning("mcpjungle list: %s", exc)
            return []

    def upsert_server(self, doc: RegistryDoc, bearer: str | None) -> bool:
        """Idempotent register-or-replace by name.

        MCPJungle's POST /api/v0/servers rejects duplicates with 409.
        We DELETE first (ignore 404) and re-POST — avoids state drift
        when a re-install changes the URL or bearer.
        """
        body: dict[str, object] = {
            "name": doc.name,
            "url": doc.endpoint.url,
            "transport": doc.endpoint.transport,
            "description": doc.description,
        }
        if bearer:
            body["bearer_token"] = bearer
        try:
            # Best-effort delete first — DELETE /api/v0/servers/<name>
            self._client.delete(f"{self.base_url}/api/v0/servers/{doc.name}")
            resp = self._client.post(
                f"{self.base_url}/api/v0/servers", json=body,
            )
            if resp.status_code >= 400:
                log.warning(
                    "mcpjungle register %s: HTTP %d %s",
                    doc.name, resp.status_code, resp.text[:200],
                )
                return False
            log.info(
                "mcpjungle registered %s (%s) -> %s",
                doc.name, doc.endpoint.transport, doc.endpoint.url,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("mcpjungle register %s: %s", doc.name, exc)
            return False

    def delete_server(self, name: str) -> bool:
        try:
            resp = self._client.delete(f"{self.base_url}/api/v0/servers/{name}")
            if resp.status_code in (200, 204, 404):
                log.info("mcpjungle deleted %s (was %d)", name, resp.status_code)
                return True
            log.warning("mcpjungle delete %s: HTTP %d", name, resp.status_code)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("mcpjungle delete %s: %s", name, exc)
            return False
