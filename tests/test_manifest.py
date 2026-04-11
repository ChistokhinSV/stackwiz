"""Unit tests for manifest loading and validation."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from stackwiz.manifest import Manifest, load_manifest

FIXTURE = Path(__file__).parent / "manifest_valid.yaml"


def test_load_valid_manifest() -> None:
    m = load_manifest(FIXTURE)
    assert m.name == "example-stack"
    assert m.domain == "example.internal"
    assert m.consul_addr() == "consul.example.internal"
    assert m.vault_addr() == "vault.example.internal"
    assert len(m.components) == 3


def test_topo_order_respects_depends() -> None:
    m = load_manifest(FIXTURE)
    order = [c.id for c in m.topo_order()]
    assert order.index("k3s") < order.index("app")
    assert order.index("app") < order.index("graylog")


def test_override_hosts() -> None:
    m = load_manifest(FIXTURE)
    m2 = m.model_copy(update={"vault_host": "vault.custom.lan", "consul_host": "c.custom.lan"})
    assert m2.vault_addr() == "vault.custom.lan"
    assert m2.consul_addr() == "c.custom.lan"


def test_unknown_dep_rejected(tmp_path: Path) -> None:
    data = yaml.safe_load(FIXTURE.read_text())
    data["components"][1]["depends"] = ["ghost"]
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(Exception, match="unknown component 'ghost'"):
        load_manifest(p)


def test_cycle_rejected(tmp_path: Path) -> None:
    data = yaml.safe_load(FIXTURE.read_text())
    data["components"][0]["depends"] = ["app"]  # k3s -> app -> k3s
    p = tmp_path / "cycle.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(Exception, match="cycle"):
        load_manifest(p)


def test_select_requires_choices(tmp_path: Path) -> None:
    data = yaml.safe_load(FIXTURE.read_text())
    data["config"][1]["choices"] = None
    p = tmp_path / "badconfig.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(Exception, match="choices"):
        load_manifest(p)


def test_manifest_reexport() -> None:
    assert Manifest is not None
