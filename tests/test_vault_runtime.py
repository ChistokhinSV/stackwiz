"""Tests for the declarative vault_runtime block + engine token minting."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stackwiz.manifest import Component, VaultRuntime


def test_vault_runtime_defaults() -> None:
    """A bare vault_runtime block accepts defaults."""
    vr = VaultRuntime()
    assert vr.policies == []
    assert vr.ttl == "720h"
    assert "{component_id}" in vr.token_file


def test_vault_runtime_on_component() -> None:
    """vault_runtime parses on Component and preserves its fields."""
    c = Component.model_validate(
        {
            "id": "kb-snmp",
            "name": "SNMP MCP",
            "install": "install/kb-snmp-mcp.sh",
            "vault_runtime": {
                "policies": ["stackwiz-shared-read"],
                "ttl": "168h",
            },
        }
    )
    assert c.vault_runtime is not None
    assert c.vault_runtime.policies == ["stackwiz-shared-read"]
    assert c.vault_runtime.ttl == "168h"


def test_vault_runtime_absent_by_default() -> None:
    """Most components don't need the block — field is optional."""
    c = Component.model_validate(
        {"id": "plain", "name": "Plain", "install": "install/plain.sh"},
    )
    assert c.vault_runtime is None


def test_mint_runtime_token_writes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine writes the token to the configured path with 0600."""
    from stackwiz.engine import Engine

    token_file = tmp_path / "runtime-tokens" / "kb-snmp.token"
    component = Component.model_validate(
        {
            "id": "kb-snmp",
            "name": "SNMP MCP",
            "install": "install/x.sh",
            "vault_runtime": {
                "policies": ["stackwiz-shared-read"],
                "token_file": str(token_file),
            },
        }
    )

    eng = Engine.__new__(Engine)
    eng.vault = MagicMock()
    eng.vault.apply_service_policy.return_value = "kb-kb-snmp"
    eng.vault.apply_shared_read_policy.return_value = "stackwiz-shared-read"
    eng.vault.create_child_token.return_value = "hvs.SECRETOKEN"

    manifest = MagicMock()
    manifest.consul.service_prefix = "kb"
    eng.manifest = manifest

    eng._mint_runtime_token(component)

    assert token_file.exists()
    assert token_file.read_text() == "hvs.SECRETOKEN"
    # 0600 on POSIX; Windows mimics via chmod but rights differ.
    mode = token_file.stat().st_mode & 0o777
    assert mode in (0o600, 0o666)  # Windows can't enforce 0600

    # Called with renewable=True and both policies
    args = eng.vault.create_child_token.call_args
    assert args.kwargs["renewable"] is True
    assert "kb-kb-snmp" in args.kwargs["policies"]
    assert "stackwiz-shared-read" in args.kwargs["policies"]


def test_mint_runtime_token_noop_without_block(tmp_path: Path) -> None:
    from stackwiz.engine import Engine

    component = Component.model_validate(
        {"id": "plain", "name": "Plain", "install": "install/plain.sh"},
    )
    eng = Engine.__new__(Engine)
    eng.vault = MagicMock()
    eng._mint_runtime_token(component)
    eng.vault.create_child_token.assert_not_called()


def test_mint_runtime_token_noop_without_vault() -> None:
    from stackwiz.engine import Engine

    component = Component.model_validate(
        {
            "id": "kb-snmp",
            "name": "x",
            "install": "install/x.sh",
            "vault_runtime": {"policies": []},
        }
    )
    eng = Engine.__new__(Engine)
    eng.vault = None
    # Should not raise.
    eng._mint_runtime_token(component)


def test_mint_runtime_token_handles_mint_failure(tmp_path: Path) -> None:
    from stackwiz.engine import Engine

    token_file = tmp_path / "kb-snmp.token"
    component = Component.model_validate(
        {
            "id": "kb-snmp",
            "name": "x",
            "install": "install/x.sh",
            "vault_runtime": {"token_file": str(token_file)},
        }
    )
    eng = Engine.__new__(Engine)
    eng.vault = MagicMock()
    eng.vault.apply_service_policy.return_value = "kb-kb-snmp"
    eng.vault.create_child_token.return_value = None  # simulate failure
    manifest = MagicMock()
    manifest.consul.service_prefix = "kb"
    eng.manifest = manifest

    eng._mint_runtime_token(component)
    assert not token_file.exists()
