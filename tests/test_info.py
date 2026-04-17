"""Info renderer tests — mocks out Consul and Vault."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from stackwiz.info import (
    collect,
    render_json,
    render_markdown,
    render_text,
    write_summary_md,
)
from stackwiz.manifest import load_manifest
from stackwiz.state import State, component_config_hash

FIXTURE = Path(__file__).parent / "manifest_valid.yaml"


@dataclass
class FakeCatalog:
    name: str
    address: str
    port: int
    tags: list


class FakeConsul:
    def __init__(self, catalog: dict[str, FakeCatalog]) -> None:
        self._catalog = catalog

    def discover(self, service):
        name = service.name if hasattr(service, "name") else service
        return self._catalog.get(name)


class FakeVault:
    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def kv_get(self, path: str, mount: str = "stackwiz"):
        if path in self._store:
            return {"value": self._store[path]}
        return None


def _prime(tmp_path: Path) -> tuple:
    manifest = load_manifest(FIXTURE)
    state = State(tmp_path)
    for c in manifest.components:
        state.mark_installed(c, component_config_hash(c, {}))
    state.save_config({"app_domain": "a.example.internal", "tls_mode": "self-signed"})
    consul = FakeConsul({
        "k3s":     FakeCatalog("k3s", "127.0.0.1", 6443, []),
        "app":     FakeCatalog("app", "127.0.0.1", 8080, ["primary"]),
        "graylog": FakeCatalog("graylog", "127.0.0.1", 9000, ["logging"]),
    })
    vault = FakeVault({
        "example/admin_password":  "hunter2hunter2",
        "example/cluster_secret":  "s" * 96,
    })
    return manifest, state, consul, vault


def test_collect_populates_everything(tmp_path: Path) -> None:
    manifest, state, consul, vault = _prime(tmp_path)
    report = collect(manifest, state, consul, vault, show_secrets=False)
    assert report.manifest_name == "Example Stack"
    assert len(report.components) == 3
    assert report.components[0].services[0].address == "127.0.0.1"
    assert report.config["app_domain"] == "a.example.internal"
    masked = {s.id: s.masked for s in report.secrets}
    assert "hunt" in masked["admin_password"]
    # secret value is not populated when show_secrets=False
    assert all(s.value is None for s in report.secrets)


def test_collect_show_secrets(tmp_path: Path) -> None:
    manifest, state, consul, vault = _prime(tmp_path)
    report = collect(manifest, state, consul, vault, show_secrets=True)
    values = {s.id: s.value for s in report.secrets}
    assert values["admin_password"] == "hunter2hunter2"


def test_render_text_mentions_components(tmp_path: Path) -> None:
    manifest, state, consul, vault = _prime(tmp_path)
    report = collect(manifest, state, consul, vault, show_secrets=False)
    out = render_text(report)
    assert "Example Stack" in out
    assert "k3s" in out
    assert "app" in out
    assert "graylog" in out


def test_render_markdown_masks_by_default(tmp_path: Path) -> None:
    manifest, state, consul, vault = _prime(tmp_path)
    report = collect(manifest, state, consul, vault, show_secrets=False)
    out = render_markdown(report, show_secrets=False)
    assert "hunter2hunter2" not in out  # masked
    assert "hunt" in out  # mask prefix shown


def test_render_json_shape(tmp_path: Path) -> None:
    manifest, state, consul, vault = _prime(tmp_path)
    report = collect(manifest, state, consul, vault, show_secrets=True)
    data = json.loads(render_json(report, show_secrets=True))
    assert data["manifest"]["name"] == "Example Stack"
    assert len(data["components"]) == 3
    secrets_by_id = {s["id"]: s for s in data["secrets"]}
    assert secrets_by_id["admin_password"]["value"] == "hunter2hunter2"


def test_write_summary_md_atomic(tmp_path: Path) -> None:
    manifest, state, consul, vault = _prime(tmp_path)
    path = write_summary_md(manifest, state, consul, vault)
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "Example Stack" in content
    # Re-run — should still succeed (atomic replace).
    write_summary_md(manifest, state, consul, vault)


def test_collect_picks_up_stackwiz_env_override_when_manifest_dir_set(
    tmp_path: Path,
) -> None:
    """info.collect with manifest_dir runs the full effective_config cascade,
    so an operator edit to .stackwiz.env is visible without re-running install."""
    manifest, state, consul, vault = _prime(tmp_path)
    # State cache says app_domain=a.example.internal. Now the operator edits
    # .stackwiz.env to set a different domain — wizinstall info should
    # surface the new value even though state wasn't re-written.
    (tmp_path / ".stackwiz.env").write_text(
        'domain: "operator-edited.lan"\n', encoding="utf-8",
    )
    report = collect(
        manifest, state, consul, vault,
        show_secrets=False, manifest_dir=tmp_path,
    )
    assert report.domain == "operator-edited.lan", (
        "domain must follow .stackwiz.env when manifest_dir is threaded through"
    )


def test_collect_falls_back_to_state_without_manifest_dir(tmp_path: Path) -> None:
    """Legacy call without manifest_dir preserves state-cache behaviour."""
    manifest, state, consul, vault = _prime(tmp_path)
    (tmp_path / ".stackwiz.env").write_text(
        'domain: "should-be-ignored.lan"\n', encoding="utf-8",
    )
    report = collect(manifest, state, consul, vault, show_secrets=False)
    # .stackwiz.env is NOT read without manifest_dir; falls back to state
    # cache (or manifest default).
    assert report.domain != "should-be-ignored.lan"


def test_host_path_reflected_in_rendered_output(
    tmp_path: Path, monkeypatch
) -> None:
    """When STACKWIZ_HOST_STATE_DIR is set, rendered output uses it."""
    monkeypatch.setenv("STACKWIZ_HOST_STATE_DIR", "/var/lib/stackwiz")
    manifest, state, consul, vault = _prime(tmp_path)
    # Sanity: State picked up the host path
    assert state.host_path() == "/var/lib/stackwiz"
    assert state.host_path("install.log") == "/var/lib/stackwiz/install.log"

    report = collect(manifest, state, consul, vault, show_secrets=False)
    # ReportData carries the host path, not the container path
    assert report.host_state_dir == "/var/lib/stackwiz"

    # All three renderers should mention the host path, not the tmp_path
    text = render_text(report)
    md = render_markdown(report)
    j = render_json(report)
    for rendered in (text, md, j):
        assert "/var/lib/stackwiz" in rendered
        assert str(tmp_path) not in rendered
