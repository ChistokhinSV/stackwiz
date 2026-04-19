"""Tests for stackwiz_hub.reconciler dispatch + KB tree handling.

Heavy mocking — the reconciler is mostly orchestration, so we verify
it dispatches the right method with the right args for each entry
kind, prunes deleted MCPs, and doesn't crash on partial failures.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stackwiz_hub.consul_watch import RegistryChangeset
from stackwiz_hub.kb_source import _replace_tree_from_tar
from stackwiz_hub.models import Auth, Endpoint, RegistryDoc, RegistryPointer
from stackwiz_hub.reconciler import Reconciler
from stackwiz_hub.write_back import _tar_tree, _tree_sha


def _pointer(kind: str, name: str) -> RegistryPointer:
    return RegistryPointer(
        kind=kind, name=name,
        config_vault_path=f"registry/{kind}/{name}/config",
    )


def _mcp_doc(name: str) -> RegistryDoc:
    return RegistryDoc(
        kind="mcp-server", name=name, owner="test",
        endpoint=Endpoint(url=f"http://{name}:8080/mcp", transport="streamable_http"),
        auth=Auth(mode="bearer", token_ref=f"registry/mcp-server/{name}/token"),
    )


def _kb_doc(name: str) -> RegistryDoc:
    return RegistryDoc(
        kind="kb-source", name=name, owner="test",
        endpoint=Endpoint(
            url=f"http://{name}:8000",
            paths={"pull": "/.kb/snapshot", "push": "/.kb/push",
                   "health": "/.kb/health"},
        ),
        auth=Auth(mode="bearer", token_ref=f"registry/kb-source/{name}/token"),
    )


@pytest.fixture
def reconciler(tmp_path: Path) -> Reconciler:
    consul = MagicMock()
    vault = MagicMock()
    mcpjungle = MagicMock()
    kb_source = MagicMock()
    write_back = MagicMock()
    r = Reconciler(
        consul=consul, vault=vault, mcpjungle=mcpjungle,
        kb_source=kb_source, write_back=write_back,
        kb_repo=tmp_path,
        author_name="test-hub", author_email="test@example.com",
    )
    # Skip the git invocations in tests.
    r._commit_kb_changes = lambda report: None  # type: ignore[assignment]
    return r


def test_reconcile_empty_registry(reconciler: Reconciler) -> None:
    reconciler.consul.fetch.return_value = RegistryChangeset(index=1, pointers=[])
    idx, report = reconciler.reconcile_once()
    assert idx == 1
    assert report.summary() == "no-op"
    reconciler.mcpjungle.upsert_server.assert_not_called()


def test_reconcile_registers_mcp(reconciler: Reconciler) -> None:
    reconciler.consul.fetch.return_value = RegistryChangeset(
        index=7, pointers=[_pointer("mcp-server", "graylog-mcp")],
    )
    reconciler.vault.read_registry_config.return_value = _mcp_doc("graylog-mcp")
    reconciler.vault.read_token.return_value = "bearer-xyz"
    reconciler.mcpjungle.upsert_server.return_value = True

    idx, report = reconciler.reconcile_once()
    assert idx == 7
    assert "graylog-mcp" in report.mcp_registered
    # Upsert got the doc + bearer.
    call = reconciler.mcpjungle.upsert_server.call_args
    assert call.args[0].name == "graylog-mcp"
    assert call.args[1] == "bearer-xyz"


def test_reconcile_prunes_removed_mcp(reconciler: Reconciler) -> None:
    # First cycle: register one.
    reconciler.consul.fetch.return_value = RegistryChangeset(
        index=1, pointers=[_pointer("mcp-server", "graylog-mcp")],
    )
    reconciler.vault.read_registry_config.return_value = _mcp_doc("graylog-mcp")
    reconciler.vault.read_token.return_value = "t"
    reconciler.mcpjungle.upsert_server.return_value = True
    reconciler.reconcile_once()

    # Second cycle: pointer list is now empty → hub should delete it.
    reconciler.consul.fetch.return_value = RegistryChangeset(index=2, pointers=[])
    reconciler.mcpjungle.delete_server.return_value = True
    idx, report = reconciler.reconcile_once()
    assert idx == 2
    assert "graylog-mcp" in report.mcp_removed
    reconciler.mcpjungle.delete_server.assert_called_once_with("graylog-mcp")


def test_reconcile_kb_source_pulls(reconciler: Reconciler) -> None:
    reconciler.consul.fetch.return_value = RegistryChangeset(
        index=5, pointers=[_pointer("kb-source", "graylog-kb")],
    )
    reconciler.vault.read_registry_config.return_value = _kb_doc("graylog-kb")
    reconciler.vault.read_token.return_value = "bearer"
    reconciler.kb_source.pull_if_changed.return_value = True
    reconciler.write_back.maybe_push.return_value = False

    _, report = reconciler.reconcile_once()
    assert "graylog-kb" in report.kb_synced
    reconciler.kb_source.pull_if_changed.assert_called_once()


def test_reconcile_kb_write_back(reconciler: Reconciler) -> None:
    reconciler.consul.fetch.return_value = RegistryChangeset(
        index=5, pointers=[_pointer("kb-source", "graylog-kb")],
    )
    reconciler.vault.read_registry_config.return_value = _kb_doc("graylog-kb")
    reconciler.vault.read_token.return_value = "bearer"
    reconciler.kb_source.pull_if_changed.return_value = False  # nothing new from source
    reconciler.write_back.maybe_push.return_value = True       # but we have central edits

    _, report = reconciler.reconcile_once()
    assert "graylog-kb" not in report.kb_synced
    assert "graylog-kb" in report.kb_pushed_back


def test_reconcile_skips_entry_on_vault_read_failure(reconciler: Reconciler) -> None:
    reconciler.consul.fetch.return_value = RegistryChangeset(
        index=9, pointers=[
            _pointer("mcp-server", "broken"),
            _pointer("mcp-server", "working"),
        ],
    )
    # First vault read returns None, second returns a valid doc.
    reconciler.vault.read_registry_config.side_effect = [
        None, _mcp_doc("working"),
    ]
    reconciler.vault.read_token.return_value = "t"
    reconciler.mcpjungle.upsert_server.return_value = True

    _, report = reconciler.reconcile_once()
    assert report.mcp_registered == ["working"]
    assert any("broken" in e for e in report.errors)


# --- kb_source._replace_tree_from_tar + write_back helpers ------------------


def test_replace_tree_from_tar_roundtrip(tmp_path: Path) -> None:
    # Build a tarball of a small tree; replace an existing dir with it.
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("alpha")
    (src / "sub").mkdir()
    (src / "sub" / "b.md").write_text("beta")

    tarball = _tar_tree(src)
    target = tmp_path / "target"
    target.mkdir()
    (target / "stale.md").write_text("old")

    _replace_tree_from_tar(target, tarball)

    assert (target / "a.md").read_text() == "alpha"
    assert (target / "sub" / "b.md").read_text() == "beta"
    assert not (target / "stale.md").exists()


def test_replace_tree_rejects_path_traversal(tmp_path: Path) -> None:
    """A malicious tarball with '../' must be refused."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="../escape.md")
        data = b"evil"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    with pytest.raises(ValueError, match="unsafe tar path"):
        _replace_tree_from_tar(tmp_path / "out", buf.getvalue())


def test_tree_sha_stable(tmp_path: Path) -> None:
    """Same content → same hash; different content → different hash."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("x")
    h1 = _tree_sha(src)
    (src / "a.md").write_text("x")    # identical rewrite
    assert _tree_sha(src) == h1
    (src / "a.md").write_text("y")    # real change
    assert _tree_sha(src) != h1
