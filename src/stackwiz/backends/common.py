"""Shared configuration primitives for backend clients.

Previously lived in ``vault_client.py``, which forced ``consul_client.py``
and ``discovery.py`` to import from vault — a textual coupling since
neither is Vault-specific. Moved here so the shared utilities have a home
that matches their scope.

All logic here is pure (no I/O) except ``suppress_insecure_warnings``,
which mutates the global ``warnings`` filter.
"""
from __future__ import annotations

import logging
import os
import warnings

import urllib3

log = logging.getLogger("stackwiz.backends")

DEFAULT_BACKEND_TIMEOUT = 30.0


def resolve_backend_timeout() -> float:
    """Read ``STACKWIZ_BACKEND_TIMEOUT`` (seconds) with a sane default.

    A hung backend will wedge install forever without this. Shared by both
    the Vault and Consul clients so one knob tunes both.
    """
    raw = os.environ.get("STACKWIZ_BACKEND_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_BACKEND_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_BACKEND_TIMEOUT
    return value if value > 0 else DEFAULT_BACKEND_TIMEOUT


def resolve_verify(explicit: bool | str | None = None) -> bool | str:
    """Resolve the TLS-verify setting for requests to Vault.

    Precedence (first match wins):
      1. ``explicit`` arg (tests / callers that already know)
      2. ``VAULT_CACERT`` env — path to a CA bundle (returned as the path)
      3. ``STACKWIZ_VAULT_VERIFY=false|0|no`` — explicit opt-out (returns False)
      4. Default: True (system CA trust)

    A False result is logged at WARNING the first time we return one per
    process, so a misconfiguration doesn't slip through silently.
    """
    if explicit is not None:
        return explicit
    cacert = os.environ.get("VAULT_CACERT", "").strip()
    if cacert:
        return cacert
    raw = os.environ.get("STACKWIZ_VAULT_VERIFY", "").strip().lower()
    if raw in {"false", "0", "no"}:
        if not getattr(resolve_verify, "_warned", False):
            log.warning(
                "TLS verification against Vault is DISABLED "
                "(STACKWIZ_VAULT_VERIFY=%s). Set VAULT_CACERT to a trust "
                "bundle to re-enable.", raw,
            )
            resolve_verify._warned = True  # type: ignore[attr-defined]
        return False
    return True


def suppress_insecure_warnings() -> None:
    """Silence urllib3's InsecureRequestWarning for verify=False flows."""
    warnings.filterwarnings(
        "ignore", category=urllib3.exceptions.InsecureRequestWarning
    )
