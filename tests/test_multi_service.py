"""Multi-service Component validation (consul_services list)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from stackwiz.manifest import load_manifest

FIXTURE = Path(__file__).parent / "manifest_valid.yaml"


def _component_with(**overrides):
    data = yaml.safe_load(FIXTURE.read_text())
    data["components"][1].pop("consul_service", None)
    data["components"][1].pop("consul_services", None)
    data["components"][1].update(overrides)
    return data


def test_consul_services_list(tmp_path: Path) -> None:
    data = _component_with(consul_services=[
        {"name": "app-http",  "port": 8080, "tags": ["http"],
         "check": {"http": "http://127.0.0.1:8080/health", "interval": "15s"}},
        {"name": "app-grpc",  "port": 9090, "tags": ["grpc"],
         "check": {"tcp": "127.0.0.1:9090", "interval": "15s"}},
    ])
    p = tmp_path / "multi.yaml"
    p.write_text(yaml.safe_dump(data))
    manifest = load_manifest(p)
    app = manifest.components[1]
    services = app.all_consul_services()
    assert [s.name for s in services] == ["app-http", "app-grpc"]


def test_both_singular_and_plural_rejected(tmp_path: Path) -> None:
    data = _component_with(
        consul_service={
            "name": "a", "port": 1, "check": {"http": "http://x/h", "interval": "10s"}
        },
        consul_services=[{
            "name": "b", "port": 2, "check": {"http": "http://x/h", "interval": "10s"}
        }],
    )
    p = tmp_path / "both.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(Exception, match="mutually exclusive"):
        load_manifest(p)


def test_singular_still_works() -> None:
    manifest = load_manifest(FIXTURE)
    k3s = manifest.components[0]
    services = k3s.all_consul_services()
    assert len(services) == 1
    assert services[0].name == "k3s"
