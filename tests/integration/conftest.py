"""Integration fixtures: docker-compose-backed Vault + Consul.

The session fixture ``backends`` brings up the stack once per pytest run,
waits for healthchecks, and tears down at the end. Tests that need the
stack take the fixture by name; tests that don't are cheap and unaffected.

Skips the entire integration suite when docker isn't available or the
user passed ``-m 'not integration'`` (the default).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

_COMPOSE_FILE = Path(__file__).parent / "docker-compose.yml"
_VAULT_URL = "http://127.0.0.1:18200"
_CONSUL_URL = "http://127.0.0.1:18500"
_VAULT_ROOT_TOKEN = "stackwiz-it-root"
_BOOT_TIMEOUT_S = 60.0


def _docker_available() -> bool:
    """Lightweight check: the ``docker`` CLI responds to --version.

    Intentionally does NOT call ``docker info`` — that fetches full engine
    state and takes 20+ seconds on Docker Desktop, and occasionally returns
    a non-zero exit code even when the daemon is perfectly usable (e.g.
    cli-plugin noise). Any real daemon-side problem surfaces when
    ``docker compose up`` runs below and raises ``CalledProcessError`` with
    the real error message in stderr — which pytest will show verbatim.
    """
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            timeout=10,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return True


def _wait_ready(url: str, deadline: float) -> None:
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code < 500:
                return
        except httpx.HTTPError as exc:
            last_exc = exc
        time.sleep(0.5)
    raise RuntimeError(
        f"timeout waiting for {url}: last exception was {last_exc!r}"
    )


@pytest.fixture(scope="session")
def backends() -> Iterator[dict[str, str]]:
    """Bring up Vault + Consul via docker-compose, yield their URLs + root token.

    Brought up once per session; torn down at end. Skips the test if docker
    isn't runnable on this host.
    """
    if not _docker_available():
        pytest.skip("docker not available — skipping integration tests")

    env = os.environ.copy()
    # `docker compose up` is the v2 syntax; fall back to `docker-compose`
    # for hosts that still ship the legacy binary.
    compose_cmd: list[str]
    if subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True, env=env,
    ).returncode == 0:
        compose_cmd = ["docker", "compose"]
    else:
        compose_cmd = ["docker-compose"]

    base_args = ["-f", str(_COMPOSE_FILE), "-p", "stackwiz-it"]

    try:
        subprocess.run(
            [*compose_cmd, *base_args, "up", "-d", "--wait"],
            check=True, env=env, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        # Docker CLI is installed but the daemon refused — common in dev
        # when Docker Desktop is in a degraded state. Skip with the real
        # stderr so the operator can diagnose without cryptic tracebacks.
        pytest.skip(
            f"docker compose up failed; skipping integration suite.\n"
            f"stderr: {exc.stderr.strip() if exc.stderr else '<empty>'}"
        )
    try:
        deadline = time.monotonic() + _BOOT_TIMEOUT_S
        _wait_ready(f"{_VAULT_URL}/v1/sys/health?standbyok=true", deadline)
        _wait_ready(f"{_CONSUL_URL}/v1/status/leader", deadline)
        # Enable the stackwiz KV mount. Vault dev mode starts with kv-v2 at
        # 'secret/' but not at 'stackwiz/'; the engine's ensure_kv_mount would
        # normally do this on adoption.
        httpx.post(
            f"{_VAULT_URL}/v1/sys/mounts/stackwiz",
            headers={"X-Vault-Token": _VAULT_ROOT_TOKEN},
            json={"type": "kv", "options": {"version": "2"}},
            timeout=5.0,
        )
        yield {
            "vault_url": _VAULT_URL,
            "consul_url": _CONSUL_URL,
            "vault_token": _VAULT_ROOT_TOKEN,
        }
    finally:
        # Keep container output attached to the pytest run for postmortem.
        subprocess.run(
            [*compose_cmd, *base_args, "down", "-v"],
            check=False, env=env,
        )


@pytest.fixture
def it_env(
    backends: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    """Wire the current test's env vars to the integration backends.

    Engine + client code picks these up via os.environ (VAULT_ADDR etc.).
    """
    monkeypatch.setenv("VAULT_ADDR", backends["vault_url"])
    monkeypatch.setenv("VAULT_TOKEN", backends["vault_token"])
    monkeypatch.setenv("CONSUL_HTTP_ADDR", backends["consul_url"])
    # Dev mode → HTTP only → no TLS verify concerns.
    monkeypatch.setenv("STACKWIZ_VAULT_VERIFY", "false")
    # Use direct executor so no nsenter is attempted (test host has no host
    # PID 1 namespace to target).
    monkeypatch.setenv("STACKWIZ_EXECUTOR_MODE", "direct")
    return backends
