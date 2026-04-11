"""Tests for .stackwiz.secrets.env scaffolding, load, and rewrite-after-upload."""
from __future__ import annotations

from pathlib import Path

import pytest

from stackwiz.engine import Engine
from stackwiz.executor import Executor
from stackwiz.manifest import Manifest, load_manifest
from stackwiz.secrets import materialize_secrets
from stackwiz.secrets_env import (
    SECRETS_ENV_FILENAME,
    filled_entries,
    load_secrets_env,
    rewrite_after_upload,
    user_secret_specs,
    write_secrets_env_scaffold,
)
from stackwiz.state import State

FIXTURE = Path(__file__).parent / "manifest_valid.yaml"


def _with_user_secrets(tmp_path: Path, extra_secrets: str) -> Manifest:
    """Copy the base fixture and append extra `secrets:` entries."""
    base = FIXTURE.read_text(encoding="utf-8")
    target = tmp_path / "components.yaml"
    target.write_text(base + extra_secrets, encoding="utf-8")
    return load_manifest(target)


def test_user_secret_specs_filters_generate_false(tmp_path: Path) -> None:
    manifest = _with_user_secrets(
        tmp_path,
        "\n  - id: smtp_password\n    generate: false\n",
    )
    specs = user_secret_specs(manifest)
    assert [s.id for s in specs] == ["smtp_password"]


def test_scaffold_creates_file_with_empty_values(tmp_path: Path) -> None:
    manifest = _with_user_secrets(
        tmp_path,
        "\n  - id: smtp_password\n    generate: false\n"
        "  - id: api_key\n    generate: false\n",
    )
    target = tmp_path / SECRETS_ENV_FILENAME
    write_secrets_env_scaffold(target, manifest)

    assert target.exists()
    loaded = load_secrets_env(target)
    assert loaded == {"smtp_password": "", "api_key": ""}
    # Comment should name the target vault path for each key.
    text = target.read_text(encoding="utf-8")
    assert "Vault path: example/smtp_password" in text
    assert "Vault path: example/api_key" in text


def test_scaffold_preserves_existing_values(tmp_path: Path) -> None:
    manifest = _with_user_secrets(
        tmp_path,
        "\n  - id: smtp_password\n    generate: false\n",
    )
    target = tmp_path / SECRETS_ENV_FILENAME
    write_secrets_env_scaffold(target, manifest, existing={"smtp_password": "hunter2"})
    assert load_secrets_env(target) == {"smtp_password": "hunter2"}


def test_scaffold_skips_when_no_user_secrets(tmp_path: Path) -> None:
    manifest = load_manifest(FIXTURE)
    target = tmp_path / SECRETS_ENV_FILENAME
    write_secrets_env_scaffold(target, manifest)
    assert not target.exists()


def test_scaffold_deletes_when_all_specs_removed(tmp_path: Path) -> None:
    manifest = _with_user_secrets(
        tmp_path,
        "\n  - id: smtp_password\n    generate: false\n",
    )
    target = tmp_path / SECRETS_ENV_FILENAME
    write_secrets_env_scaffold(target, manifest, existing={"smtp_password": "hunter2"})
    assert target.exists()
    # Now a "manifest" with no user secs → file gone.
    write_secrets_env_scaffold(target, manifest, existing={}, specs=[])
    assert not target.exists()


def test_filled_entries_drops_empty() -> None:
    assert filled_entries({"a": "x", "b": "", "c": "y"}) == {"a": "x", "c": "y"}


def test_load_handles_missing_and_malformed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    assert load_secrets_env(missing) == {}

    bad = tmp_path / "bad.yaml"
    bad.write_text(": : not yaml", encoding="utf-8")
    assert load_secrets_env(bad) == {}

    non_mapping = tmp_path / "list.yaml"
    non_mapping.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert load_secrets_env(non_mapping) == {}


def test_rewrite_after_upload_strips_uploaded_keys(tmp_path: Path) -> None:
    manifest = _with_user_secrets(
        tmp_path,
        "\n  - id: smtp_password\n    generate: false\n"
        "  - id: api_key\n    generate: false\n",
    )
    target = tmp_path / SECRETS_ENV_FILENAME
    write_secrets_env_scaffold(
        target, manifest,
        existing={"smtp_password": "hunter2", "api_key": ""},
    )
    rewrite_after_upload(target, manifest, uploaded_ids={"smtp_password"})

    remaining = load_secrets_env(target)
    assert remaining == {"api_key": ""}


def test_rewrite_deletes_file_when_all_uploaded(tmp_path: Path) -> None:
    manifest = _with_user_secrets(
        tmp_path,
        "\n  - id: smtp_password\n    generate: false\n",
    )
    target = tmp_path / SECRETS_ENV_FILENAME
    write_secrets_env_scaffold(
        target, manifest, existing={"smtp_password": "hunter2"},
    )
    rewrite_after_upload(target, manifest, uploaded_ids={"smtp_password"})
    assert not target.exists()


# --- engine integration -----------------------------------------------------


class _FakeVault:
    """In-memory KV v2 stand-in for engine tests."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, str]] = {}
        self.address = "http://fake-vault"

    def kv_put(self, path: str, data: dict[str, str]) -> None:
        self.store[path] = dict(data)

    def kv_get(self, path: str) -> dict[str, str] | None:
        return dict(self.store[path]) if path in self.store else None


def test_engine_uploads_and_strips_filled_entries(tmp_path: Path) -> None:
    manifest = _with_user_secrets(
        tmp_path,
        "\n  - id: smtp_password\n    generate: false\n"
        "  - id: api_key\n    generate: false\n",
    )
    manifest_dir = tmp_path  # manifest_valid copy lives here
    secrets_file = manifest_dir / SECRETS_ENV_FILENAME
    write_secrets_env_scaffold(
        secrets_file, manifest,
        existing={"smtp_password": "hunter2", "api_key": ""},
    )

    vault = _FakeVault()
    executor = Executor(manifest_dir=manifest_dir)
    state = State(tmp_path / "state")
    engine = Engine(
        manifest=manifest,
        state=state,
        executor=executor,
        consul=None,
        vault=vault,  # type: ignore[arg-type]
    )

    engine._upload_user_secrets()

    assert vault.store == {"example/smtp_password": {"value": "hunter2"}}
    assert load_secrets_env(secrets_file) == {"api_key": ""}


def test_init_env_scaffolds_secrets_file_and_gitignore(tmp_path: Path) -> None:
    """`wizinstall init-env` drops both files and updates .gitignore."""
    from click.testing import CliRunner

    from stackwiz.cli import main

    manifest_src = (
        FIXTURE.read_text(encoding="utf-8")
        + "\n  - id: smtp_password\n    generate: false\n"
    )
    manifest_path = tmp_path / "components.yaml"
    manifest_path.write_text(manifest_src, encoding="utf-8")

    state_dir = tmp_path / "state"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "init-env",
            "--manifest", str(manifest_path),
            "--state", str(state_dir),
        ],
    )
    assert result.exit_code == 0, result.output

    env_file = tmp_path / ".stackwiz.env"
    secrets_file = tmp_path / SECRETS_ENV_FILENAME
    gitignore = tmp_path / ".gitignore"
    assert env_file.exists()
    assert secrets_file.exists()
    assert load_secrets_env(secrets_file) == {"smtp_password": ""}
    assert gitignore.exists()
    gi_lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
    assert ".stackwiz.env" in gi_lines
    assert SECRETS_ENV_FILENAME in gi_lines


def test_init_env_gitignore_is_idempotent(tmp_path: Path) -> None:
    """Re-running init-env does not duplicate gitignore entries."""
    from click.testing import CliRunner

    from stackwiz.cli import main

    manifest_src = (
        FIXTURE.read_text(encoding="utf-8")
        + "\n  - id: smtp_password\n    generate: false\n"
    )
    manifest_path = tmp_path / "components.yaml"
    manifest_path.write_text(manifest_src, encoding="utf-8")

    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules/\n.stackwiz.env\n", encoding="utf-8")

    state_dir = tmp_path / "state"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "init-env", "--force",
            "--manifest", str(manifest_path),
            "--state", str(state_dir),
        ],
    )
    assert result.exit_code == 0, result.output

    lines = gitignore.read_text(encoding="utf-8").splitlines()
    # Only one occurrence of each, secrets file appended.
    assert lines.count(".stackwiz.env") == 1
    assert lines.count(SECRETS_ENV_FILENAME) == 1
    assert "node_modules/" in lines

    # Run a second time — nothing new should be added.
    before = gitignore.read_text(encoding="utf-8")
    result2 = runner.invoke(
        main,
        [
            "init-env", "--force",
            "--manifest", str(manifest_path),
            "--state", str(state_dir),
        ],
    )
    assert result2.exit_code == 0
    assert gitignore.read_text(encoding="utf-8") == before


def test_materialize_errors_with_pointer_when_file_empty(tmp_path: Path) -> None:
    manifest = _with_user_secrets(
        tmp_path,
        "\n  - id: smtp_password\n    generate: false\n",
    )
    vault = _FakeVault()
    # Seed the two auto-generated ones so materialize gets far enough to
    # surface the missing user-supplied one (order is manifest order).
    with pytest.raises(RuntimeError) as exc:
        materialize_secrets(manifest, vault)  # type: ignore[arg-type]
    msg = str(exc.value)
    assert "smtp_password" in msg
    assert SECRETS_ENV_FILENAME in msg
    assert "example/smtp_password" in msg
