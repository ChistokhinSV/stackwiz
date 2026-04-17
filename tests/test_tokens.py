"""Tests for the backend-token resolver (security-adjacent fallback chain)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stackwiz.discovery import ProbeResult, Source
from stackwiz.tokens import (
    build_backends,
    read_sibling_state_token,
    resolve_consul_token,
    resolve_vault_token,
)

# --- read_sibling_state_token -----------------------------------------------


def test_sibling_lookup_finds_token(tmp_path: Path) -> None:
    own = tmp_path / "own"
    sibling = tmp_path / "other"
    own.mkdir()
    sibling.mkdir()
    (sibling / "consul-http-token").write_text("t-from-sibling")
    assert read_sibling_state_token(own, "consul-http-token") == "t-from-sibling"


def test_sibling_lookup_skips_self_and_empty(tmp_path: Path) -> None:
    own = tmp_path / "own"
    own.mkdir()
    (own / "consul-http-token").write_text("own-only")  # must be ignored
    (tmp_path / "empty_sibling").mkdir()
    (tmp_path / "empty_sibling" / "consul-http-token").write_text("   ")  # blanks
    assert read_sibling_state_token(own, "consul-http-token") is None


def test_sibling_lookup_returns_none_when_base_missing(tmp_path: Path) -> None:
    # state_dir whose parent does not exist shouldn't raise.
    assert read_sibling_state_token(tmp_path / "nope" / "child", "x") is None


# --- resolve_vault_token ----------------------------------------------------


def test_resolve_vault_prefers_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "vault-token").write_text("own-tok")
    monkeypatch.setenv("VAULT_TOKEN", "env-tok")
    assert resolve_vault_token(tmp_path) == "own-tok"


def test_resolve_vault_env_beats_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = tmp_path / "own"
    own.mkdir()
    sibling = tmp_path / "other"
    sibling.mkdir()
    (sibling / "vault-token").write_text("from-sibling")
    monkeypatch.setenv("VAULT_TOKEN", "from-env")
    assert resolve_vault_token(own) == "from-env"


def test_resolve_vault_sibling_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = tmp_path / "own"
    own.mkdir()
    sibling = tmp_path / "other"
    sibling.mkdir()
    (sibling / "vault-token").write_text("from-sibling")
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    assert resolve_vault_token(own) == "from-sibling"


# --- resolve_consul_token (the 4-source chain) ------------------------------


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("CONSUL_HTTP_TOKEN", "VAULT_TOKEN", "VAULT_ADDR"):
        monkeypatch.delenv(v, raising=False)


def test_consul_own_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    own = tmp_path / "own"
    own.mkdir()
    (own / "consul-http-token").write_text("own-consul")
    monkeypatch.setenv("CONSUL_HTTP_TOKEN", "env-would-lose")
    assert resolve_consul_token(own, vault=None) == "own-consul"


def test_consul_env_caches_to_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_env(monkeypatch)
    own = tmp_path / "own"
    own.mkdir()
    monkeypatch.setenv("CONSUL_HTTP_TOKEN", "env-tok")
    assert resolve_consul_token(own, vault=None) == "env-tok"
    # Subsequent call must now hit the cached own file (no env needed).
    monkeypatch.delenv("CONSUL_HTTP_TOKEN")
    assert resolve_consul_token(own, vault=None) == "env-tok"


def test_consul_sibling_then_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_env(monkeypatch)
    own = tmp_path / "own"
    own.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "consul-http-token").write_text("sib-tok")
    assert resolve_consul_token(own, vault=None) == "sib-tok"
    assert (own / "consul-http-token").read_text() == "sib-tok"


def test_consul_vault_kv_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_env(monkeypatch)
    own = tmp_path / "own"
    own.mkdir()
    vault = MagicMock()
    vault.token = "vault-tok"
    vault.kv_get.return_value = {"value": "from-vault"}
    assert resolve_consul_token(own, vault=vault) == "from-vault"
    vault.kv_get.assert_called_once_with("shared/consul_bootstrap_token")


def test_consul_returns_none_when_all_sources_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_env(monkeypatch)
    own = tmp_path / "own"
    own.mkdir()
    assert resolve_consul_token(own, vault=None) is None


# --- build_backends ---------------------------------------------------------


def _probe(reachable: bool, addr: str = "http://example:8500") -> ProbeResult:
    return ProbeResult(
        Source.DOMAIN if reachable else Source.MISSING,
        addr if reachable else None,
        "",
    )


def test_build_backends_both_unreachable(tmp_path: Path) -> None:
    c, v = build_backends(tmp_path, _probe(False), _probe(False))
    assert c is None and v is None


def test_build_backends_builds_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_env(monkeypatch)
    with patch("stackwiz.tokens.VaultClient") as vault_cls, \
         patch("stackwiz.tokens.ConsulClient") as consul_cls:
        vault_cls.return_value = MagicMock()
        vault_cls.return_value.token = "v-tok"
        consul_cls.return_value = MagicMock()
        c, v = build_backends(
            tmp_path,
            _probe(True, "http://consul:8500"),
            _probe(True, "https://vault:8200"),
        )
        assert c is not None and v is not None
        vault_cls.assert_called_once()
        consul_cls.assert_called_once()


def test_build_backends_ensure_kv_mount_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_env(monkeypatch)
    with patch("stackwiz.tokens.VaultClient") as vault_cls:
        vc = MagicMock()
        vc.token = None
        vault_cls.return_value = vc
        build_backends(
            tmp_path,
            _probe(False),
            _probe(True, "https://vault:8200"),
            ensure_kv_mount=True,
        )
        vc.ensure_kv_mount.assert_called_once()


def test_build_backends_ensure_kv_mount_swallows_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_env(monkeypatch)
    with patch("stackwiz.tokens.VaultClient") as vault_cls:
        vc = MagicMock()
        vc.token = None
        vc.ensure_kv_mount.side_effect = RuntimeError("mount exists")
        vault_cls.return_value = vc
        # Must not raise.
        build_backends(
            tmp_path, _probe(False), _probe(True, "https://vault:8200"),
            ensure_kv_mount=True,
        )
