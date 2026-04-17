"""End-to-end smoke test: install -> NOOP re-run -> uninstall.

The test drives the engine the same way ``wizinstall run --auto`` does
(via ``run_headless``) so the whole CLI-side invocation path gets
exercised, not just the engine in isolation.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from stackwiz.headless import run_headless
from stackwiz.manifest import load_manifest
from stackwiz.state import State

MANIFEST_DIR = Path(__file__).parent / "manifests" / "minimal"
MANIFEST_FILE = MANIFEST_DIR / "components.yaml"


@pytest.mark.integration
def test_install_is_idempotent_and_uninstall_cleans_up(
    it_env: dict[str, str],
    tmp_path: Path,
) -> None:
    # run_headless takes the state_dir it is handed as-is. Namespacing by
    # manifest.name is a `wizinstall` CLI concern (_resolve_state_dir) —
    # the test exercises run_headless directly, so we pass a per-test dir.
    state_dir = tmp_path / "state"
    manifest = load_manifest(MANIFEST_FILE)

    # 1. Install. Exit code 0, component marked installed, Consul picks up
    #    the registration, Vault KV has the smoke value the script wrote.
    rc = run_headless(
        manifest=manifest,
        state_dir=state_dir,
        manifest_dir=MANIFEST_DIR,
        mode="install",
    )
    assert rc == 0, "first install should succeed"

    installed_yaml = state_dir / "installed.yaml"
    assert installed_yaml.exists(), "state file must be written after install"
    state = State(state_dir)
    assert "hello" in state.installed()

    # Consul should have the service.
    r = httpx.get(f"{it_env['consul_url']}/v1/catalog/services", timeout=5.0)
    # No consul_service block on the component so the catalog has only 'consul'
    # — but the point is the Consul agent is reachable and the engine talked
    # to it. The richer "service registered" assertion belongs on a manifest
    # that declares a consul_service.
    assert r.status_code == 200

    # Vault should have the smoke value the install script wrote.
    r = httpx.get(
        f"{it_env['vault_url']}/v1/stackwiz/data/it/hello/smoke",
        headers={"X-Vault-Token": it_env["vault_token"]},
        timeout=5.0,
    )
    assert r.status_code == 200, "install script should have written the smoke value"
    assert r.json()["data"]["data"]["smoke"] == "ok"

    # 2. Re-run. Nothing changed → NOOP. Verified via state unchanged.
    rc = run_headless(
        manifest=manifest,
        state_dir=state_dir,
        manifest_dir=MANIFEST_DIR,
        mode="install",
    )
    assert rc == 0, "re-run should succeed"
    state = State(state_dir)
    entry = state.installed()["hello"]
    assert entry.version == "0.0.1"

    # 3. Uninstall. Exit 0, state clean.
    rc = run_headless(
        manifest=manifest,
        state_dir=state_dir,
        manifest_dir=MANIFEST_DIR,
        mode="uninstall",
    )
    assert rc == 0, "uninstall should succeed"
    state = State(state_dir)
    assert "hello" not in state.installed(), (
        f"component 'hello' should be removed from state, got {state.installed()}"
    )


@pytest.mark.integration
def test_backends_probes_resolve_localhost(
    it_env: dict[str, str],
) -> None:
    """Sanity check on the discovery + tokens wiring before the full smoke."""
    import asyncio

    from stackwiz.discovery import probe_consul, probe_vault

    probe_v = asyncio.run(probe_vault("it.stackwiz.local", "127.0.0.1:18200"))
    assert probe_v.reachable, f"vault probe should succeed: {probe_v}"
    probe_c = asyncio.run(probe_consul("it.stackwiz.local", "127.0.0.1:18500"))
    assert probe_c.reachable, f"consul probe should succeed: {probe_c}"
