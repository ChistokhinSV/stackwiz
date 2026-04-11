"""Tests for secret generator dispatch (password/hex/base64/uuid/cmd)."""
from __future__ import annotations

import base64
import re
import uuid as uuid_module
from pathlib import Path

import pytest

from stackwiz.manifest import Secret, load_manifest
from stackwiz.secrets import (
    generate_base64,
    generate_hex,
    generate_password,
    generate_uuid,
    generate_value,
    materialize_secrets,
    run_cmd_generator,
)

FIXTURE = Path(__file__).parent / "manifest_valid.yaml"


# --- direct generator unit tests --------------------------------------------


def test_generate_password_alnum_and_length() -> None:
    value = generate_password(24)
    assert len(value) == 24
    assert re.fullmatch(r"[A-Za-z0-9]+", value) is not None


def test_generate_hex_length_is_2x_bytes() -> None:
    value = generate_hex(16)
    assert len(value) == 32
    assert re.fullmatch(r"[0-9a-f]+", value) is not None


def test_generate_base64_decodes_to_expected_byte_count() -> None:
    value = generate_base64(32)  # consul gossip key shape
    decoded = base64.b64decode(value)
    assert len(decoded) == 32


def test_generate_uuid_is_uuid4() -> None:
    value = generate_uuid()
    parsed = uuid_module.UUID(value)
    assert parsed.version == 4


def test_run_cmd_generator_returns_stripped_stdout() -> None:
    # Cross-platform: python -c always works and doesn't need a shell builtin.
    assert run_cmd_generator("python -c \"print('hello')\"") == "hello"


def test_run_cmd_generator_raises_on_nonzero_exit() -> None:
    with pytest.raises(RuntimeError, match="exit"):
        run_cmd_generator("python -c \"import sys; sys.exit(2)\"")


def test_run_cmd_generator_raises_on_empty_output() -> None:
    with pytest.raises(RuntimeError, match="empty output"):
        run_cmd_generator("python -c \"pass\"")


# --- dispatch via generate_value(Secret) ------------------------------------


def _secret(**kwargs: object) -> Secret:
    defaults: dict[str, object] = {"id": "test"}
    defaults.update(kwargs)
    return Secret.model_validate(defaults)


def test_dispatch_password_default() -> None:
    spec = _secret(length=8)
    assert len(generate_value(spec)) == 8


def test_dispatch_hex() -> None:
    spec = _secret(type="hex", length=4)
    value = generate_value(spec)
    assert len(value) == 8
    assert re.fullmatch(r"[0-9a-f]+", value) is not None


def test_dispatch_base64_consul_gossip() -> None:
    spec = _secret(type="base64", length=32)
    assert len(base64.b64decode(generate_value(spec))) == 32


def test_dispatch_uuid() -> None:
    spec = _secret(type="uuid")
    assert uuid_module.UUID(generate_value(spec)).version == 4


def test_dispatch_cmd() -> None:
    spec = _secret(type="cmd", command="python -c \"print('x' * 7)\"")
    assert generate_value(spec) == "x" * 7


# --- manifest-level validation ----------------------------------------------


def test_cmd_without_command_rejected() -> None:
    with pytest.raises(Exception, match="type=cmd requires"):
        Secret.model_validate({"id": "s", "type": "cmd"})


def test_command_without_cmd_type_rejected() -> None:
    with pytest.raises(Exception, match="only valid with type=cmd"):
        Secret.model_validate({"id": "s", "type": "password", "command": "echo x"})


def test_unknown_type_rejected() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Secret.model_validate({"id": "s", "type": "weird"})


def test_zero_length_rejected_for_password() -> None:
    with pytest.raises(Exception, match="length must be > 0"):
        Secret.model_validate({"id": "s", "type": "password", "length": 0})


# --- end-to-end materialize_secrets with mixed types -------------------------


class _FakeVault:
    def __init__(self) -> None:
        self.store: dict[str, dict[str, str]] = {}
        self.address = "http://fake"

    def kv_put(self, path: str, data: dict[str, str]) -> None:
        self.store[path] = dict(data)

    def kv_get(self, path: str) -> dict[str, str] | None:
        return dict(self.store[path]) if path in self.store else None


def test_materialize_dispatches_per_type(tmp_path: Path) -> None:
    base = FIXTURE.read_text(encoding="utf-8")
    extras = (
        "\n  - id: gossip_key\n    type: base64\n    length: 32\n"
        "\n  - id: session_id\n    type: uuid\n"
        "\n  - id: hex_token\n    type: hex\n    length: 16\n"
        "\n  - id: from_cmd\n    type: cmd\n"
        "    command: \"python -c \\\"print('generated')\\\"\"\n"
    )
    manifest_path = tmp_path / "components.yaml"
    manifest_path.write_text(base + extras, encoding="utf-8")
    manifest = load_manifest(manifest_path)

    vault = _FakeVault()
    result = materialize_secrets(manifest, vault)  # type: ignore[arg-type]

    # gossip_key: base64 of 32 random bytes
    assert len(base64.b64decode(result["gossip_key"].value)) == 32
    # session_id: valid UUID4
    assert uuid_module.UUID(result["session_id"].value).version == 4
    # hex_token: 32 chars (16 bytes)
    assert len(result["hex_token"].value) == 32
    assert re.fullmatch(r"[0-9a-f]+", result["hex_token"].value)
    # from_cmd: literal stdout
    assert result["from_cmd"].value == "generated"
