"""Tests for effective_config merging + ${var} substitution."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from stackwiz.cli import main
from stackwiz.config_overrides import effective_config
from stackwiz.manifest import load_manifest

FIXTURE = Path(__file__).parent / "manifest_valid.yaml"


def test_defaults_returned_without_overrides() -> None:
    manifest = load_manifest(FIXTURE)
    values, domain = effective_config(manifest, state_config={}, env_file=None)
    assert domain == "example.internal"
    assert values["app_domain"] == "app.example.internal"
    assert values["tls_mode"] == "self-signed"


def test_env_file_override(tmp_path: Path) -> None:
    manifest = load_manifest(FIXTURE)
    env_file = tmp_path / ".stackwiz.env"
    env_file.write_text("tls_mode: auto\napp_domain: custom.example.internal\n")
    values, domain = effective_config(manifest, state_config={}, env_file=env_file)
    assert values["tls_mode"] == "auto"
    assert values["app_domain"] == "custom.example.internal"
    assert domain == "example.internal"


def test_state_beats_env_file(tmp_path: Path) -> None:
    manifest = load_manifest(FIXTURE)
    env_file = tmp_path / ".stackwiz.env"
    env_file.write_text("tls_mode: auto\n")
    values, _ = effective_config(
        manifest,
        state_config={"tls_mode": "manual-dns"},
        env_file=env_file,
    )
    assert values["tls_mode"] == "manual-dns"


def test_domain_override_cascades(tmp_path: Path) -> None:
    """If a field default is `auth.${domain}`, overriding domain updates it."""
    import yaml as pyyaml
    data = pyyaml.safe_load(FIXTURE.read_text())
    data["config"].append({
        "id": "app_hostname",
        "label": "App hostname",
        "type": "text",
        "default": "auth.${domain}",
    })
    modified = tmp_path / "manifest.yaml"
    modified.write_text(pyyaml.safe_dump(data))
    manifest = load_manifest(modified)

    # Default domain → rendered hostname
    values, domain = effective_config(manifest, state_config={}, env_file=None)
    assert domain == "example.internal"
    assert values["app_hostname"] == "auth.example.internal"

    # Override domain → hostname updates
    env_file = tmp_path / ".stackwiz.env"
    env_file.write_text('domain: "my-lab.internal"\n')
    values, domain = effective_config(manifest, state_config={}, env_file=env_file)
    assert domain == "my-lab.internal"
    assert values["app_hostname"] == "auth.my-lab.internal"


def test_unknown_placeholder_stays_literal(tmp_path: Path) -> None:
    import yaml as pyyaml
    data = pyyaml.safe_load(FIXTURE.read_text())
    data["config"][0]["default"] = "app.${unknown_var}.internal"
    modified = tmp_path / "manifest.yaml"
    modified.write_text(pyyaml.safe_dump(data))
    manifest = load_manifest(modified)
    values, _ = effective_config(manifest, state_config={}, env_file=None)
    assert values["app_domain"] == "app.${unknown_var}.internal"


def test_init_env_writes_scaffold(tmp_path: Path) -> None:
    runner = CliRunner()
    state = tmp_path / "state"
    target = tmp_path / ".stackwiz.env"
    result = runner.invoke(
        main,
        [
            "init-env",
            "--manifest", str(FIXTURE),
            "--state", str(state),
            "--output", str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert 'domain: "example.internal"' in body
    assert "app_domain" in body
    assert "tls_mode" in body


def test_init_env_refuses_to_overwrite(tmp_path: Path) -> None:
    runner = CliRunner()
    state = tmp_path / "state"
    target = tmp_path / ".stackwiz.env"
    target.write_text("existing: true\n")
    result = runner.invoke(
        main,
        [
            "init-env",
            "--manifest", str(FIXTURE),
            "--state", str(state),
            "--output", str(target),
        ],
    )
    assert result.exit_code == 2
    assert "already exists" in result.output

    # --force overwrites
    result = runner.invoke(
        main,
        [
            "init-env",
            "--manifest", str(FIXTURE),
            "--state", str(state),
            "--output", str(target),
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    assert 'domain:' in target.read_text(encoding="utf-8")
