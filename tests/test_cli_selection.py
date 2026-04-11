"""Selective installation — positional component ids / indices."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from stackwiz.cli import main, resolve_selection
from stackwiz.manifest import load_manifest

FIXTURE = Path(__file__).parent / "manifest_valid.yaml"


def test_resolve_by_id() -> None:
    manifest = load_manifest(FIXTURE)
    assert resolve_selection(manifest, ("k3s",)) == {"k3s"}
    assert resolve_selection(manifest, ("app", "graylog")) == {"app", "graylog"}


def test_resolve_by_index_1based() -> None:
    manifest = load_manifest(FIXTURE)
    # topo_order: k3s (1) -> app (2) -> graylog (3)
    assert resolve_selection(manifest, ("1",)) == {"k3s"}
    assert resolve_selection(manifest, ("2", "3")) == {"app", "graylog"}


def test_resolve_mixed() -> None:
    manifest = load_manifest(FIXTURE)
    assert resolve_selection(manifest, ("1", "graylog")) == {"k3s", "graylog"}


def test_resolve_unknown_id_exits_2() -> None:
    manifest = load_manifest(FIXTURE)
    with pytest.raises(SystemExit) as excinfo:
        resolve_selection(manifest, ("ghost",))
    assert excinfo.value.code == 2


def test_resolve_index_out_of_range_exits_2() -> None:
    manifest = load_manifest(FIXTURE)
    with pytest.raises(SystemExit) as excinfo:
        resolve_selection(manifest, ("99",))
    assert excinfo.value.code == 2


def test_list_command_prints_components() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["list", "--manifest", str(FIXTURE), "--state", "/tmp/stackwiz-test-list"],
    )
    assert result.exit_code == 0, result.output
    assert "k3s" in result.output
    assert "app" in result.output
    assert "graylog" in result.output
    # Each row has an index column.
    assert "  1  k3s" in result.output
    assert "  2  app" in result.output


def test_validate_still_works() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["validate", "--manifest", str(FIXTURE), "--state", "/tmp/stackwiz-test-valid"],
    )
    assert result.exit_code == 0, result.output
    assert "Example Stack" in result.output
    assert "k3s -> app -> graylog" in result.output
