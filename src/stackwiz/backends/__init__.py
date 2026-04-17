"""Shared backend primitives: clients (Vault, Consul), discovery, token resolution.

The individual clients live at the top level of ``stackwiz.*`` for now
(``stackwiz.vault_client``, ``stackwiz.consul_client``, ``stackwiz.discovery``,
``stackwiz.tokens``). This package owns the shared utilities — timeout
resolution, TLS-verify policy — that those modules consume. Cross-module
imports should go through ``stackwiz.backends.common`` so the dependency
graph stays uni-directional:

    common --> vault --> discovery --> tokens

instead of consul and discovery reaching into vault_client for utilities.
"""
from stackwiz.backends.common import (
    DEFAULT_BACKEND_TIMEOUT,
    resolve_backend_timeout,
    resolve_verify,
    suppress_insecure_warnings,
)

__all__ = [
    "DEFAULT_BACKEND_TIMEOUT",
    "resolve_backend_timeout",
    "resolve_verify",
    "suppress_insecure_warnings",
]
