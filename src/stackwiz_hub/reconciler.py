"""Single reconcile loop — the heart of the hub.

Two reconcile triggers:
  * Consul KV change under stackwiz/registry/  (blocking query wakes us)
  * Safety timer (reconcile_safety_interval_s) — catches silent
    Vault-side changes the blocking query doesn't see (e.g. bearer
    rotation without a pointer rewrite).

For each registry entry the loop:
  1. Fetches the full config from Vault.
  2. Resolves the bearer (if any).
  3. Dispatches on entry.kind:
       kb-source   -> pull via HTTP, merge into kb-repo
       mcp-server  -> upsert with MCPJungle
  4. On kb-source entries, also runs the write-back scan.

All errors log-warn and continue — one broken source must not block
the rest. A full reconcile iteration always succeeds (possibly with
zero effective work) so the loop never wedges.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from stackwiz_hub.consul_watch import ConsulWatcher
from stackwiz_hub.kb_source import KBSourceClient
from stackwiz_hub.mcpjungle import MCPJungleClient
from stackwiz_hub.models import RegistryDoc, RegistryPointer
from stackwiz_hub.vault_client import VaultReader
from stackwiz_hub.write_back import WriteBackClient

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    kb_synced: list[str] = field(default_factory=list)
    kb_pushed_back: list[str] = field(default_factory=list)
    mcp_registered: list[str] = field(default_factory=list)
    mcp_removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts: list[str] = []
        if self.mcp_registered:
            parts.append(f"mcp+={len(self.mcp_registered)}")
        if self.mcp_removed:
            parts.append(f"mcp-={len(self.mcp_removed)}")
        if self.kb_synced:
            parts.append(f"kb-pull={len(self.kb_synced)}")
        if self.kb_pushed_back:
            parts.append(f"kb-push={len(self.kb_pushed_back)}")
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts) if parts else "no-op"


class Reconciler:
    def __init__(
        self,
        *,
        consul: ConsulWatcher,
        vault: VaultReader | None,
        mcpjungle: MCPJungleClient | None,
        kb_source: KBSourceClient,
        write_back: WriteBackClient,
        kb_repo: Path,
        author_name: str,
        author_email: str,
    ) -> None:
        self.consul = consul
        self.vault = vault
        self.mcpjungle = mcpjungle
        self.kb_source = kb_source
        self.write_back = write_back
        self.kb_repo = kb_repo
        self.author_name = author_name
        self.author_email = author_email
        self._known_mcp_names: set[str] = set()

    def reconcile_once(self, since_index: int = 0) -> tuple[int, ReconcileReport]:
        """Block on Consul; returns new-index + work summary."""
        report = ReconcileReport()
        changeset = self.consul.fetch(since_index=since_index)

        # Resolve full config docs via Vault (one RTT per entry).
        resolved: list[tuple[RegistryPointer, RegistryDoc, str | None]] = []
        for pointer in changeset.pointers:
            if self.vault is None:
                report.errors.append(
                    f"{pointer.name}: no Vault client; entry skipped",
                )
                continue
            doc = self.vault.read_registry_config(pointer.config_vault_path)
            if doc is None:
                report.errors.append(f"{pointer.name}: vault config read failed")
                continue
            bearer = self.vault.read_token(doc.auth.token_ref)
            resolved.append((pointer, doc, bearer))

        # Reconcile MCP servers — track + prune deletes by diffing
        # the pointer list against the names we last registered.
        # Only names whose upsert SUCCEEDED enter _known_mcp_names,
        # so a failed registration retries on the next reconcile
        # (otherwise a transient DNS / MCPJungle blip would leave
        # the server permanently unregistered until KV changes).
        if self.mcpjungle is not None:
            desired = {doc.name for _, doc, _ in resolved if doc.kind == "mcp-server"}
            stale = self._known_mcp_names - desired
            for name in stale:
                if self.mcpjungle.delete_server(name):
                    report.mcp_removed.append(name)
            new_known = self._known_mcp_names - stale
            for _, doc, bearer in resolved:
                if doc.kind != "mcp-server":
                    continue
                if self.mcpjungle.upsert_server(doc, bearer):
                    report.mcp_registered.append(doc.name)
                    new_known.add(doc.name)
                else:
                    report.errors.append(f"mcp upsert failed: {doc.name}")
            self._known_mcp_names = new_known

        # Reconcile KB sources — pull + commit.
        kb_changed = False
        for _, doc, bearer in resolved:
            if doc.kind != "kb-source":
                continue
            if self.kb_source.pull_if_changed(doc, bearer):
                report.kb_synced.append(doc.name)
                kb_changed = True
        if kb_changed:
            self._commit_kb_changes(report)

        # Write-back (central → source) for every kb-source every cycle.
        for _, doc, bearer in resolved:
            if doc.kind != "kb-source":
                continue
            if self.write_back.maybe_push(doc, bearer):
                report.kb_pushed_back.append(doc.name)

        return changeset.index, report

    # ---- kb-repo git housekeeping -----------------------------------------

    def _commit_kb_changes(self, report: ReconcileReport) -> None:
        """git-commit any pending changes in kb-repo.

        Pulls land as file writes under _sources/<name>/ and need to
        be recorded so subsequent reads (kb-mcp indexing, git log) see
        a coherent history.
        """
        try:
            self._git("add", "-A")
            # Only commit if there's actually something staged — avoids
            # empty commits on reconcile-no-op cycles.
            diff = subprocess.run(
                ["git", "-C", str(self.kb_repo), "diff", "--staged", "--quiet"],
                check=False,
            )
            if diff.returncode == 0:
                return
            self._git(
                "-c", f"user.name={self.author_name}",
                "-c", f"user.email={self.author_email}",
                "commit", "-q", "-m", "hub: sync KB sources",
            )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"git commit failed: {exc}")

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.kb_repo), *args],
            check=True, capture_output=True,
        )
