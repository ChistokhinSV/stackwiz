"""Smoke tests for the stackwiz.backends package boundary.

Verifies the new canonical import path works and that the legacy top-level
``stackwiz.vault_client`` / ``stackwiz.consul_client`` / ``stackwiz.discovery``
modules still expose the shared utilities (back-compat).
"""
from __future__ import annotations


def test_backends_package_reexports() -> None:
    from stackwiz.backends import (  # noqa: F401
        DEFAULT_BACKEND_TIMEOUT,
        resolve_backend_timeout,
        resolve_verify,
        suppress_insecure_warnings,
    )


def test_common_module_canonical_import() -> None:
    from stackwiz.backends.common import (  # noqa: F401
        DEFAULT_BACKEND_TIMEOUT,
        resolve_backend_timeout,
        resolve_verify,
        suppress_insecure_warnings,
    )


def test_vault_client_still_reexports_for_backcompat() -> None:
    # Existing callers and tests import these from stackwiz.vault_client.
    # The helpers now live in stackwiz.backends.common; the shim keeps the
    # old path valid.
    import stackwiz.backends.common as common
    import stackwiz.vault_client as vc

    assert vc.resolve_backend_timeout is common.resolve_backend_timeout
    assert vc.resolve_verify is common.resolve_verify
    assert vc.suppress_insecure_warnings is common.suppress_insecure_warnings
    assert vc.DEFAULT_BACKEND_TIMEOUT == common.DEFAULT_BACKEND_TIMEOUT


def test_consul_discovery_use_canonical_imports() -> None:
    # These modules should now pull the common utilities from
    # stackwiz.backends.common, not from vault_client. This test doesn't
    # assert the import path textually — it just verifies the runtime
    # identity of the functions they consume matches backends.common.
    import stackwiz.backends.common as common
    import stackwiz.consul_client  # noqa: F401
    import stackwiz.discovery  # noqa: F401

    # Each module imports into its own namespace; the identities should
    # match common (not vault_client-local copies).
    from stackwiz.consul_client import resolve_backend_timeout as from_consul
    from stackwiz.discovery import resolve_verify as from_discovery

    assert from_consul is common.resolve_backend_timeout
    assert from_discovery is common.resolve_verify
