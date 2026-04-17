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


# --- forward-compat (SCR-169) ------------------------------------------------


def test_unknown_leaf_field_is_ignored_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A new field landed on Component in a future stackwiz version must not
    break older consumer manifests — it's warn-logged and dropped."""
    data = yaml.safe_load(FIXTURE.read_text())
    data["components"][0]["future_field_we_dont_know"] = "whatever"
    p = tmp_path / "future.yaml"
    p.write_text(yaml.safe_dump(data))
    with caplog.at_level("WARNING", logger="stackwiz.manifest"):
        m = load_manifest(p)
    assert m.components[0].id == "k3s"
    assert any("future_field_we_dont_know" in r.message for r in caplog.records)


def test_unknown_root_field_still_rejected(tmp_path: Path) -> None:
    """Root-level typos stay hard errors — `extra="forbid"` on Manifest."""
    data = yaml.safe_load(FIXTURE.read_text())
    data["xomponents"] = []  # typo
    p = tmp_path / "root_typo.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(Exception, match="xomponents"):
        load_manifest(p)


def test_schema_version_in_future_rejected(tmp_path: Path) -> None:
    data = yaml.safe_load(FIXTURE.read_text())
    data["schema_version"] = 999
    p = tmp_path / "future_schema.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(Exception, match="schema_version=999"):
        load_manifest(p)


def test_schema_version_1_accepted(tmp_path: Path) -> None:
    data = yaml.safe_load(FIXTURE.read_text())
    data["schema_version"] = 1
    p = tmp_path / "v1_explicit.yaml"
    p.write_text(yaml.safe_dump(data))
    m = load_manifest(p)
    assert m.schema_version == 1


def test_schema_version_missing_defaults_to_current(tmp_path: Path) -> None:
    # Existing manifests without schema_version still load.
    m = load_manifest(FIXTURE)
    assert m.schema_version == 1


def test_schema_version_negative_rejected(tmp_path: Path) -> None:
    data = yaml.safe_load(FIXTURE.read_text())
    data["schema_version"] = -1
    p = tmp_path / "neg.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(Exception, match="positive integer"):
        load_manifest(p)
