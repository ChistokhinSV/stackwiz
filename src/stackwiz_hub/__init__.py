"""stackwiz-hub — one-per-host reconciler for the cross-stack registry.

Reads `stackwiz/registry/<kind>/<name>` entries (written by the engine's
`_publish_registry` step) via Consul blocking queries and drives:

* KB content sync — GET /.kb/snapshot from sources, merge into a
  shared kb-repo on the host filesystem.
* MCP server registration — POST to MCPJungle's /api/v0/servers so
  LibreChat picks up every registered MCP behind one gateway.
* KB write-back — POST /.kb/push to sources when central-side
  commits have edits destined for them.

Replaces the separate kb-source-sync + kb-mcp-registrar sidecars that
each reinvented discovery, auth, and reconcile. One reconcile loop,
one Vault policy, one docker network attachment.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
