"""Tests for stackwiz/scaffold.py (init-env + extract-bootstrap business logic)."""
from __future__ import annotations

from pathlib import Path

import pytest

from stackwiz.manifest import Manifest, load_manifest
from stackwiz.scaffold import (
    BOOTSTRAP_CONFIG_TEMPLATE,
    read_bootstrap_launcher_text,
    read_bootstrap_library_text,
    scaffold_env_files,
    write_bootstrap,
)

FIXTURE = Path(__file__).parent / "manifest_valid.yaml"


@pytest.fixture
def manifest(tmp_path: Path) -> Manifest:
    target = tmp_path / "components.yaml"
    target.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return load_manifest(target)


# --- scaffold_env_files -----------------------------------------------------


def test_scaffold_writes_env_file(manifest: Manifest, tmp_path: Path) -> None:
    target = tmp_path / ".stackwiz.env"
    result = scaffold_env_files(manifest, tmp_path, target, force=False)
    assert result.env_file == target.resolve()
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "stackwiz consumer config overrides" in text
    assert "domain:" in text


def test_scaffold_raises_when_exists_without_force(
    manifest: Manifest, tmp_path: Path
) -> None:
    target = tmp_path / ".stackwiz.env"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        scaffold_env_files(manifest, tmp_path, target, force=False)


def test_scaffold_overwrites_with_force(
    manifest: Manifest, tmp_path: Path
) -> None:
    target = tmp_path / ".stackwiz.env"
    target.write_text("x", encoding="utf-8")
    result = scaffold_env_files(manifest, tmp_path, target, force=True)
    assert "stackwiz consumer config overrides" in result.env_file.read_text()


def test_scaffold_preserves_existing_override_as_uncommented(
    tmp_path: Path,
) -> None:
    base = FIXTURE.read_text(encoding="utf-8")
    manifest_path = tmp_path / "components.yaml"
    manifest_path.write_text(base, encoding="utf-8")
    manifest = load_manifest(manifest_path)
    # Pre-populate target with a user-edited value for one config field.
    first_field = manifest.config[0]
    target = tmp_path / ".stackwiz.env"
    target.write_text(
        f'{first_field.id}: "custom-value"\n',
        encoding="utf-8",
    )
    scaffold_env_files(manifest, tmp_path, target, force=True)
    out = target.read_text(encoding="utf-8")
    assert f'{first_field.id}: "custom-value"' in out


def test_scaffold_updates_gitignore(manifest: Manifest, tmp_path: Path) -> None:
    target = tmp_path / ".stackwiz.env"
    result = scaffold_env_files(manifest, tmp_path, target, force=False)
    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    text = gitignore.read_text(encoding="utf-8")
    assert ".stackwiz.env" in text
    assert ".stackwiz.secrets.env" in text
    assert ".stackwiz.env" in result.gitignore_added


def test_scaffold_gitignore_idempotent(
    manifest: Manifest, tmp_path: Path
) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".stackwiz.env\n.stackwiz.secrets.env\n.env\n", encoding="utf-8")
    result = scaffold_env_files(
        manifest, tmp_path, tmp_path / ".stackwiz.env", force=False,
    )
    assert result.gitignore_added == []


def test_scaffold_env_file_chmod_best_effort(
    manifest: Manifest, tmp_path: Path
) -> None:
    # Just verify the call doesn't raise even when chmod would fail (POSIX-only
    # semantics on Windows — mode returns 0o666 but operation succeeds).
    target = tmp_path / ".stackwiz.env"
    result = scaffold_env_files(manifest, tmp_path, target, force=False)
    assert result.env_file.exists()


# --- domain override (positional arg) ---------------------------------------


def test_scaffold_uses_manifest_default_domain_when_unset(
    manifest: Manifest, tmp_path: Path
) -> None:
    target = tmp_path / ".stackwiz.env"
    scaffold_env_files(manifest, tmp_path, target, force=False)
    text = target.read_text(encoding="utf-8")
    # The fixture's manifest has `domain: example.internal`.
    assert 'domain: "example.internal"' in text


def test_scaffold_domain_override_injects_into_env_file(
    manifest: Manifest, tmp_path: Path
) -> None:
    target = tmp_path / ".stackwiz.env"
    scaffold_env_files(
        manifest, tmp_path, target, force=False, domain="mycompany.lan",
    )
    text = target.read_text(encoding="utf-8")
    assert 'domain: "mycompany.lan"' in text
    # Fixture manifest's default is example.internal — make sure it's gone.
    assert 'domain: "example.internal"' not in text


def test_scaffold_domain_override_propagates_to_derived_hints(
    tmp_path: Path,
) -> None:
    """Derived fields (auth.${domain}, admin@${domain}) must render against
    the operator-supplied domain, not the manifest default."""
    import yaml as _yaml
    data = _yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    # Inject a ${domain}-derived field into the config: section.
    data["config"].append({
        "id": "app_hostname",
        "label": "App hostname",
        "type": "text",
        "default": "app.${domain}",
    })
    target_manifest = tmp_path / "components.yaml"
    target_manifest.write_text(_yaml.safe_dump(data), encoding="utf-8")
    m = load_manifest(target_manifest)
    env = tmp_path / ".stackwiz.env"
    scaffold_env_files(m, tmp_path, env, force=False, domain="acme.test")
    text = env.read_text(encoding="utf-8")
    assert 'domain: "acme.test"' in text
    # The derived hint line (commented) should show the substituted value,
    # not the literal ${domain}.
    assert "app.acme.test" in text


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "has spaces", "no/slashes", "foo..com", "-leadinghyphen.com",
     "ends.with-", "foo.com/../etc"],
)
def test_scaffold_rejects_invalid_domain(
    manifest: Manifest, tmp_path: Path, bad: str
) -> None:
    target = tmp_path / ".stackwiz.env"
    with pytest.raises(ValueError, match="invalid domain"):
        scaffold_env_files(manifest, tmp_path, target, force=False, domain=bad)


@pytest.mark.parametrize(
    "ok",
    ["example.com", "sub.example.com", "my-lab.internal",
     "a1.b2.c3", "localhost"],
)
def test_scaffold_accepts_valid_domains(
    manifest: Manifest, tmp_path: Path, ok: str
) -> None:
    target = tmp_path / ".stackwiz.env"
    scaffold_env_files(manifest, tmp_path, target, force=False, domain=ok)
    assert target.exists()


# --- write_bootstrap --------------------------------------------------------


def test_write_bootstrap_creates_all_three_files(tmp_path: Path) -> None:
    result = write_bootstrap(tmp_path, force=False)
    assert result.lib_path.name == "stackwiz-bootstrap.sh"
    assert result.launcher_path.name == "bootstrap.sh"
    assert result.config_path.name == "bootstrap.conf.sh"
    assert result.lib_path.exists()
    assert result.launcher_path.exists()
    assert result.config_path.exists()
    assert result.config_created is True
    assert "sw_bootstrap_main" in result.lib_path.read_text(encoding="utf-8")
    # Launcher sources both config and library.
    launcher = result.launcher_path.read_text(encoding="utf-8")
    assert "bootstrap.conf.sh" in launcher
    assert "stackwiz-bootstrap.sh" in launcher
    assert "sw_bootstrap_main" in launcher


def test_write_bootstrap_custom_launcher_name(tmp_path: Path) -> None:
    result = write_bootstrap(tmp_path, launcher_name="install.sh", force=False)
    assert result.launcher_path.name == "install.sh"


def test_write_bootstrap_refuses_overwrite(tmp_path: Path) -> None:
    write_bootstrap(tmp_path, force=False)
    with pytest.raises(FileExistsError):
        write_bootstrap(tmp_path, force=False)


def test_write_bootstrap_force_overwrites_framework_files(tmp_path: Path) -> None:
    write_bootstrap(tmp_path, force=False)
    (tmp_path / "stackwiz-bootstrap.sh").write_text("old lib", encoding="utf-8")
    (tmp_path / "bootstrap.sh").write_text("old launcher", encoding="utf-8")
    result = write_bootstrap(tmp_path, force=True)
    assert "sw_bootstrap_main" in result.lib_path.read_text(encoding="utf-8")
    assert "sw_bootstrap_main" in result.launcher_path.read_text(encoding="utf-8")


def test_write_bootstrap_force_preserves_config(tmp_path: Path) -> None:
    """--force refreshes library+launcher but must NEVER touch bootstrap.conf.sh."""
    write_bootstrap(tmp_path, force=False)
    custom = "SW_EXTRA_ENV=(MY_CUSTOM_TOKEN)\n"
    (tmp_path / "bootstrap.conf.sh").write_text(custom, encoding="utf-8")
    result = write_bootstrap(tmp_path, force=True)
    assert result.config_created is False
    assert result.config_path.read_text(encoding="utf-8") == custom


def test_read_bootstrap_library_text_contains_public_api() -> None:
    text = read_bootstrap_library_text()
    assert "sw_bootstrap_main" in text
    assert "sw_bootstrap_ensure_docker" in text


def test_read_bootstrap_launcher_text_sources_library() -> None:
    text = read_bootstrap_launcher_text()
    assert "stackwiz-bootstrap.sh" in text
    assert "bootstrap.conf.sh" in text
    assert "sw_bootstrap_main" in text


def test_bootstrap_config_template_is_commented_out(tmp_path: Path) -> None:
    """Default config template should leave all SW_* commented out so the
    library defaults apply until the consumer explicitly overrides them."""
    # Every SW_ assignment must be behind a leading `#`.
    for line in BOOTSTRAP_CONFIG_TEMPLATE.splitlines():
        stripped = line.strip()
        if stripped.startswith("SW_") and "=" in stripped:
            raise AssertionError(f"uncommented default in config template: {line!r}")


def test_bootstrap_config_template_sourced_by_launcher() -> None:
    """Launcher and config-template filenames must agree."""
    assert "bootstrap.conf.sh" in read_bootstrap_launcher_text()
