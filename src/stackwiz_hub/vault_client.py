"""Read-only Vault client for the hub.

Reads two kinds of paths:
  {mount}/data/registry/<kind>/<name>/config  -> RegistryDoc (JSON-encoded
                                                  in the KV value.value)
  {mount}/data/<token_ref>                     -> {"value": "<bearer>"}

The hub's Vault token has policy stackwiz-hub-reader (see
vault_client.apply_hub_reader_policy in the engine). It has READ on
registry/* and nothing else — intentionally minimal.
"""
from __future__ import annotations

import json
import logging

import httpx

from stackwiz_hub.models import RegistryDoc

log = logging.getLogger(__name__)


class VaultReader:
    def __init__(
        self,
        *,
        addr: str,
        token: str,
        kv_mount: str = "stackwiz",
        verify: bool = False,
        timeout_s: int = 10,
    ) -> None:
        self.addr = addr.rstrip("/")
        self.kv_mount = kv_mount.strip("/") or "stackwiz"
        self._client = httpx.Client(
            timeout=timeout_s,
            verify=verify,
            headers={"X-Vault-Token": token},
        )

    def close(self) -> None:
        self._client.close()

    def read_registry_config(self, config_path: str) -> RegistryDoc | None:
        """config_path is the value of RegistryPointer.config_vault_path.

        Returns None (with a warning logged) on any error — the reconcile
        loop skips the entry and retries next cycle.
        """
        url = f"{self.addr}/v1/{self.kv_mount}/data/{config_path.lstrip('/')}"
        try:
            resp = self._client.get(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("vault read %s: %s", config_path, exc)
            return None
        if resp.status_code != 200:
            log.warning("vault read %s: HTTP %d", config_path, resp.status_code)
            return None
        try:
            inner = resp.json().get("data", {}).get("data", {})
            raw = inner.get("value")
            if not raw:
                log.warning("vault read %s: no 'value' field in KV data", config_path)
                return None
            return RegistryDoc.model_validate(json.loads(raw))
        except Exception as exc:  # noqa: BLE001
            log.warning("vault parse %s: %s", config_path, exc)
            return None

    def read_token(self, token_ref: str | None) -> str | None:
        """Resolve an auth.token_ref to its bearer value; None if missing/ref-less."""
        if not token_ref:
            return None
        url = f"{self.addr}/v1/{self.kv_mount}/data/{token_ref.lstrip('/')}"
        try:
            resp = self._client.get(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("vault token %s: %s", token_ref, exc)
            return None
        if resp.status_code != 200:
            log.warning("vault token %s: HTTP %d", token_ref, resp.status_code)
            return None
        try:
            return (
                resp.json().get("data", {}).get("data", {}).get("value") or None
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("vault token %s parse: %s", token_ref, exc)
            return None
