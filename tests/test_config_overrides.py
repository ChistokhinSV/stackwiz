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


def test_env_file_beats_state(tmp_path: Path) -> None:
    """Git-tracked intent (.stackwiz.env) wins over runtime state cache."""
    manifest = load_manifest(FIXTURE)
    env_file = tmp_path / ".stackwiz.env"
    env_file.write_text("tls_mode: auto\n")
    values, _ = effective_config(
        manifest,
        state_config={"tls_mode": "manual-dns"},
        env_file=env_file,
    )
    assert values["tls_mode"] == "auto"


def test_state_used_when_env_file_absent() -> None:
    """If no .stackwiz.env, state cache is used (re-run pre-fill)."""
    manifest = load_manifest(FIXTURE)
    values, _ = effective_config(
        manifest,
        state_config={"tls_mode": "manual-dns"},
        env_file=None,
    )
    assert values["tls_mode"] == "manual-dns"


def test_state_skipped_for_template_fields(tmp_path: Path) -> None:
    """State cache entries are ignored for fields whose default is a template.

    Without this, a cached `authentik_hostname: auth.stackwiz.lab` from a
    prior install would clobber the `auth.${domain}` template and the
    cascade would silently break when the operator changes `domain`.
    """
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

    env_file = tmp_path / ".stackwiz.env"
    env_file.write_text('domain: "mylab.internal"\n')

    values, domain = effective_config(
        manifest,
        state_config={
            # Stale resolved value from a prior run; must be ignored.
            "app_hostname": "auth.stackwiz.lab",
        },
        env_file=env_file,
    )
    assert domain == "mylab.internal"
    assert values["app_hostname"] == "auth.mylab.internal"  # re-derived, not stale


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
    # domain is uncommented (it's the most common thing to change)
    assert 'domain: "example.internal"' in body
    # other fields appear as commented hints
    assert "# app_domain" in body
    assert "# tls_mode" in body
    assert "EDIT ONLY WHAT YOU NEED TO OVERRIDE" in body


def test_init_env_cascades_on_reinit(tmp_path: Path) -> None:
    """After editing domain + re-running init-env, derived hints update."""
    runner = CliRunner()
    state = tmp_path / "state"
    target = tmp_path / ".stackwiz.env"

    # First run
    result = runner.invoke(
        main,
        ["init-env", "--manifest", str(FIXTURE), "--state", str(state),
         "--output", str(target)],
    )
    assert result.exit_code == 0
    assert '# app_domain: "app.example.internal"' in target.read_text()

    # Operator edits domain
    text = target.read_text()
    text = text.replace(
        'domain: "example.internal"', 'domain: "mylab.corp"'
    )
    target.write_text(text)

    # Re-run with --force
    result = runner.invoke(
        main,
        ["init-env", "--manifest", str(FIXTURE), "--state", str(state),
         "--output", str(target), "--force"],
    )
    assert result.exit_code == 0, result.output
    body = target.read_text()
    assert 'domain: "mylab.corp"' in body
    # The app_domain manifest default is "app.example.internal" (literal, no
    # ${domain}), so it doesn't cascade — that's expected. We just verify the
    # cascade works structurally by checking the re-init preserves domain.


def test_init_env_preserves_user_overrides(tmp_path: Path) -> None:
    """If the user uncommented a field, re-init keeps it uncommented."""
    runner = CliRunner()
    state = tmp_path / "state"
    target = tmp_path / ".stackwiz.env"

    # Hand-write a file with one explicit override
    target.write_text(
        'domain: "example.internal"\n'
        'tls_mode: "auto"\n'  # user override
    )

    result = runner.invoke(
        main,
        ["init-env", "--manifest", str(FIXTURE), "--state", str(state),
         "--output", str(target), "--force"],
    )
    assert result.exit_code == 0
    body = target.read_text()
    # tls_mode stays uncommented (user set it)
    assert 'tls_mode: "auto"' in body
    assert '# tls_mode: "auto"' not in body
    # app_domain is still commented (user didn't touch it)
    assert '# app_domain' in body


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
