"""stackwiz-hub entrypoint.

Loops forever:
  1. Consul blocking-query wakes on KV changes (or after
     safety_interval_s — whichever comes first).
  2. Reconciler dispatches per-entry work.
  3. Log the summary; go again.

SIGTERM/SIGINT cleanly close httpx clients and exit.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime

from stackwiz_hub.config import Settings, get_settings
from stackwiz_hub.consul_watch import ConsulWatcher
from stackwiz_hub.kb_source import KBSourceClient
from stackwiz_hub.mcpjungle import MCPJungleClient
from stackwiz_hub.reconciler import Reconciler
from stackwiz_hub.vault_client import VaultReader
from stackwiz_hub.write_back import WriteBackClient

log = logging.getLogger("stackwiz-hub")


class _Formatter(logging.Formatter):
    """[HH:MM:SS dd.mm.YYYY] LEVEL logger: message."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S %d.%m.%Y")
        return f"[{ts}] {record.levelname:<7} {record.name}: {record.getMessage()}"


def _setup_logging(level: str) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter())
    root.addHandler(handler)
    root.setLevel(level.upper())


def _build_components(
    s: Settings,
) -> tuple[
    ConsulWatcher,
    VaultReader | None,
    MCPJungleClient | None,
    KBSourceClient,
    WriteBackClient,
]:
    consul = ConsulWatcher(
        addr=s.consul_http_addr,
        token=s.consul_token,
        prefix=s.registry_prefix,
        wait_s=s.reconcile_safety_interval_s,
    )
    vault: VaultReader | None = None
    if s.vault_addr and s.vault_token:
        vault = VaultReader(
            addr=s.vault_addr,
            token=s.vault_token,
            kv_mount=s.vault_kv_mount,
            verify=s.vault_verify,
            timeout_s=s.http_timeout_s,
        )
    else:
        log.warning("VAULT_ADDR/VAULT_TOKEN unset; entries skipped (hub is read-only to Vault)")
    mcpjungle: MCPJungleClient | None = None
    if s.mcpjungle_url:
        mcpjungle = MCPJungleClient(
            base_url=s.mcpjungle_url, timeout_s=s.http_timeout_s,
        )
    else:
        log.info("MCPJUNGLE_URL unset; MCP entries will be ignored")

    kb_repo = s.kb_repo_path
    kb_repo.mkdir(parents=True, exist_ok=True)
    kb_source = KBSourceClient(kb_repo=kb_repo, timeout_s=s.http_timeout_s)
    write_back = WriteBackClient(kb_repo=kb_repo, timeout_s=s.http_timeout_s)
    return consul, vault, mcpjungle, kb_source, write_back


def main() -> int:
    s = get_settings()
    _setup_logging(s.log_level)
    log.info(
        "stackwiz-hub starting (consul=%s vault=%s mcpjungle=%s kb_repo=%s)",
        s.consul_http_addr,
        s.vault_addr or "<disabled>",
        s.mcpjungle_url or "<disabled>",
        s.kb_repo_path,
    )

    consul, vault, mcpjungle, kb_source, write_back = _build_components(s)
    reconciler = Reconciler(
        consul=consul, vault=vault, mcpjungle=mcpjungle,
        kb_source=kb_source, write_back=write_back,
        kb_repo=s.kb_repo_path,
        author_name=s.kb_commit_author_name,
        author_email=s.kb_commit_author_email,
    )

    # Clean shutdown on signals.
    stop = False

    def _on_signal(signum: int, _frame: object) -> None:
        nonlocal stop
        log.info("caught signal %d; shutting down", signum)
        stop = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    index = 0
    backoff_s = 1.0
    while not stop:
        try:
            index, report = reconciler.reconcile_once(since_index=index)
            log.info("reconcile idx=%d: %s", index, report.summary())
            backoff_s = 1.0
        except Exception as exc:  # noqa: BLE001
            # Exponential backoff so a dead Consul doesn't hammer
            # logs at tens of thousands of lines per second. Cap at
            # the safety-poll interval — past that, waiting longer
            # helps nothing.
            log.warning("reconcile failed: %s (sleep %.0fs)", exc, backoff_s)
            time.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, float(s.reconcile_safety_interval_s))

    for c in (consul, vault, mcpjungle, kb_source, write_back):
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    log.info("stackwiz-hub exited")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
