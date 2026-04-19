"""Consul KV blocking queries for reactive registry updates.

We use the raw HTTP v1 API rather than python-consul2 for two reasons:

1. Blocking-query semantics (`?index=<N>&wait=<duration>`) are opaque
   under python-consul2 — you can do them but the client re-reads
   headers awkwardly. Raw httpx is three lines.
2. We only need *two* Consul operations (list-recurse with blocking,
   list-recurse without blocking) so dragging in a client just to
   call two endpoints is poor value.

Blocking query semantics: the first call returns immediately with the
current state + an X-Consul-Index header. Subsequent calls pass that
index via ?index=<N>&wait=<duration> and block server-side until the
KV changes (new entry, updated value, or delete) OR the wait expires.
We set wait to `reconcile_safety_interval_s` so a silent network gap
still wakes the hub on a regular cadence.
"""
from __future__ import annotations

import json
import logging
from base64 import b64decode
from dataclasses import dataclass

import httpx

from stackwiz_hub.models import RegistryPointer

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RegistryChangeset:
    """One snapshot of `stackwiz/registry/` contents at a Consul index."""

    index: int
    pointers: list[RegistryPointer]


class ConsulWatcher:
    def __init__(
        self,
        *,
        addr: str,
        token: str = "",
        prefix: str = "stackwiz/registry/",
        wait_s: int = 300,
    ) -> None:
        self.addr = addr.rstrip("/")
        self._headers: dict[str, str] = {}
        if token:
            self._headers["X-Consul-Token"] = token
        self.prefix = prefix.lstrip("/")
        self.wait_s = wait_s
        # timeout is intentionally wait_s + small grace — a healthy
        # blocking query always returns within wait_s, but network
        # latency + Consul's own overshoot of up to ~1.5x the wait
        # means we need slack to avoid spurious httpx timeouts.
        self._client = httpx.Client(timeout=wait_s + 30)

    def close(self) -> None:
        self._client.close()

    def fetch(self, since_index: int = 0) -> RegistryChangeset:
        """One blocking read; returns as soon as the index advances.

        A fresh hub passes since_index=0 → returns current state
        immediately. Subsequent calls pass the last-seen index; Consul
        blocks until anything under the prefix changes, or wait_s
        elapses (returns the same data with a new index — harmless
        idempotent re-reconcile).
        """
        url = f"{self.addr}/v1/kv/{self.prefix}"
        params: dict[str, str] = {"recurse": "true"}
        if since_index > 0:
            params["index"] = str(since_index)
            params["wait"] = f"{self.wait_s}s"
        resp = self._client.get(url, params=params, headers=self._headers)
        # 404 on the prefix is normal before any consumer has registered.
        if resp.status_code == 404:
            index = int(resp.headers.get("X-Consul-Index", since_index + 1))
            return RegistryChangeset(index=index, pointers=[])
        resp.raise_for_status()

        index = int(resp.headers.get("X-Consul-Index", since_index + 1))
        body = resp.json() or []
        pointers: list[RegistryPointer] = []
        for entry in body:
            raw = entry.get("Value")
            if not raw:
                # The prefix key itself can appear with Value=None —
                # Consul stores directories as entries with no body.
                continue
            try:
                decoded = b64decode(raw).decode("utf-8")
                pointer_doc = json.loads(decoded)
                pointers.append(RegistryPointer.model_validate(pointer_doc))
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "consul KV %s: skipping unparseable entry: %s",
                    entry.get("Key", "?"), exc,
                )
                continue
        return RegistryChangeset(index=index, pointers=pointers)
