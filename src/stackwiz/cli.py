"""Entry point: `wizinstall`.

Subcommands:
  run         Launch the TUI installer (default if no subcommand given).
  uninstall   Launch the TUI in uninstall mode.
  validate    Validate manifest and exit.
  list        Print components in install order with current state.
  info        Show installed components, service URLs, and secret paths.

All subcommands share --manifest and --state. `run` and `uninstall` accept
`--auto` for headless execution and take optional positional args (component
ids or 1-based indices — same UX as 061's `./deploy.sh 19 20`) to run only
the specified components.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from stackwiz import __version__
from stackwiz.manifest import Component, Manifest, load_manifest


def _resolve_state_dir(base: Path, manifest: Manifest) -> Path:
    """Namespace state by manifest name to prevent multi-stack collisions.

    Returns ``base / manifest.name`` so each consumer gets its own
    ``installed.yaml``, ``install.log``, ``summary.md``, etc.

    Backward compat: if a flat ``installed.yaml`` already sits at *base*
    (pre-namespace layout) and the namespaced dir has none yet, keep using
    the flat path so existing single-stack installs aren't broken.
    """
    namespaced = base / manifest.name
    if (base / "installed.yaml").exists() and not (namespaced / "installed.yaml").exists():
        return base
    host_base = os.environ.get("STACKWIZ_HOST_STATE_DIR")
    if host_base:
        os.environ["STACKWIZ_HOST_STATE_DIR"] = str(Path(host_base) / manifest.name)
    return namespaced


def _shared_opts(func):
    func = click.option(
        "--state",
        "state_dir",
        type=click.Path(file_okay=False, path_type=Path),
        default=Path("/state"),
        show_default=True,
        help="Directory for persistent installer state.",
    )(func)
    func = click.option(
        "--manifest",
        "manifest_path",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        default=Path("/manifest/components.yaml"),
        show_default=True,
        help="Path to the components.yaml manifest.",
    )(func)
    return func


def _load(manifest_path: Path) -> Manifest:
    try:
        return load_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"manifest error: {exc}", err=True)
        sys.exit(2)


def resolve_selection(
    manifest: Manifest,
    tokens: tuple[str, ...],
) -> set[str]:
    """Resolve positional tokens (ids or 1-based indices) to component ids.

    Unknown tokens cause a clean error and exit(2). Matches 061's UX: no
    automatic dependency inclusion — the user is responsible for running
    prerequisites first.
    """
    order = manifest.topo_order()
    by_id = {c.id: c for c in order}
    selected: set[str] = set()
    for tok in tokens:
        if tok.isdigit():
            idx = int(tok) - 1
            if idx < 0 or idx >= len(order):
                click.echo(
                    f"error: index {tok} out of range (1-{len(order)}). "
                    f"Run `wizinstall list` to see valid indices.",
                    err=True,
                )
                sys.exit(2)
            selected.add(order[idx].id)
        elif tok in by_id:
            selected.add(tok)
        else:
            click.echo(
                f"error: unknown component {tok!r}. "
                f"Run `wizinstall list` to see valid ids.",
                err=True,
            )
            sys.exit(2)
    return selected


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.version_option(__version__, "-V", "--version")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Interactive TUI installer for declarative component manifests."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(run)


@main.command()
@_shared_opts
@click.option("--auto", "auto_mode", is_flag=True, default=False,
              help="Run headless (no TUI).")
@click.option("--force", "force_refresh", is_flag=True, default=False,
              help="Re-run selected components with Action.REFRESH even if "
                   "nothing changed. Alias for `wizinstall refresh` with the "
                   "same selection.")
@click.argument("components", nargs=-1)
def run(
    manifest_path: Path,
    state_dir: Path,
    auto_mode: bool,
    force_refresh: bool,
    components: tuple[str, ...],
) -> None:
    """Install components from the manifest.

    With no COMPONENTS args, installs everything required+default (headless)
    or prompts via the TUI. With COMPONENTS args, installs only the listed
    components (ids or 1-based indices from `wizinstall list`).

    Use `--force` to re-run even when the engine would otherwise NOOP — the
    specified components run with Action.REFRESH and `WIZ_ACTION=refresh`
    set in the script env.
    """
    manifest = _load(manifest_path)
    state_dir = _resolve_state_dir(state_dir, manifest)
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = manifest_path.parent.resolve()
    selected_override: set[str] | None = None
    forced_refresh: set[str] | None = None
    if components:
        selected_override = resolve_selection(manifest, components)
    if force_refresh:
        forced_refresh = selected_override or {
            c.id for c in manifest.components if c.required or c.default
        }
    _dispatch(
        "install", manifest, state_dir, manifest_dir, auto_mode,
        selected_override, forced_refresh,
    )


@main.command()
@_shared_opts
@click.option("--auto", "auto_mode", is_flag=True, default=False,
              help="Run headless (no TUI).")
@click.argument("components", nargs=-1)
def refresh(
    manifest_path: Path,
    state_dir: Path,
    auto_mode: bool,
    components: tuple[str, ...],
) -> None:
    """Force-re-run installed components without a config change.

    Every selected component is run with Action.REFRESH and
    `WIZ_ACTION=refresh` set in the script env, regardless of whether
    anything in the manifest or state changed. Use this for steps that
    pull from upstream on every run — git-synced ansible playbooks,
    template provisioning, kubernetes manifest re-application.

    With no COMPONENTS args, refreshes every currently-installed component.
    With COMPONENTS args, refreshes only the listed ones (ids or 1-based
    indices from `wizinstall list`).

    Equivalent to `wizinstall run --force [COMPONENTS...]`.
    """
    manifest = _load(manifest_path)
    state_dir = _resolve_state_dir(state_dir, manifest)
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = manifest_path.parent.resolve()

    from stackwiz.state import State
    state = State(state_dir)
    if components:
        selected = resolve_selection(manifest, components)
        # Restrict to things that are actually installed; trying to refresh
        # something that was never installed makes no sense.
        installed_ids = set(state.installed().keys())
        missing = selected - installed_ids
        if missing:
            click.echo(
                f"note: not installed (will install from scratch): "
                f"{', '.join(sorted(missing))}"
            )
    else:
        selected = set(state.installed().keys())
        if not selected:
            click.echo(
                "error: nothing installed yet; run `wizinstall run` first",
                err=True,
            )
            sys.exit(2)

    _dispatch(
        "install", manifest, state_dir, manifest_dir, auto_mode,
        selected, forced_refresh=selected,
    )


@main.command()
@_shared_opts
@click.option("--auto", "auto_mode", is_flag=True, default=False,
              help="Run headless (no TUI).")
@click.argument("components", nargs=-1)
def uninstall(
    manifest_path: Path,
    state_dir: Path,
    auto_mode: bool,
    components: tuple[str, ...],
) -> None:
    """Remove components in reverse order.

    With no COMPONENTS args, removes everything currently installed. With
    COMPONENTS args, removes only the listed components (reverse topological
    order within the selection).
    """
    manifest = _load(manifest_path)
    state_dir = _resolve_state_dir(state_dir, manifest)
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = manifest_path.parent.resolve()
    selected_override: set[str] | None = None
    if components:
        selected_override = resolve_selection(manifest, components)
    _dispatch(
        "uninstall", manifest, state_dir, manifest_dir, auto_mode, selected_override
    )


@main.command()
@_shared_opts
def validate(manifest_path: Path, state_dir: Path) -> None:  # noqa: ARG001
    """Validate manifest and print the install order."""
    manifest = _load(manifest_path)
    click.echo(f"ok: {manifest.display_name} v{manifest.version}")
    click.echo(f"  domain: {manifest.domain}")
    click.echo(f"  components: {len(manifest.components)}")
    click.echo(f"  order: {' -> '.join(c.id for c in manifest.topo_order())}")


@main.command("list")
@_shared_opts
def list_cmd(manifest_path: Path, state_dir: Path) -> None:
    """Print components in install order with their current state.

    Output format is designed for `wizinstall run` positional args: both the
    index (left column) and the id (second column) are accepted by `run` and
    `uninstall`. Same pattern as 061's `./deploy.sh --list`.
    """
    from stackwiz.state import State
    manifest = _load(manifest_path)
    state_dir = _resolve_state_dir(state_dir, manifest)
    state_dir.mkdir(parents=True, exist_ok=True)
    state = State(state_dir)
    installed = state.installed()

    click.echo(f"{manifest.display_name}  v{manifest.version}")
    click.echo(f"domain: {manifest.domain}  state: {state_dir}")
    click.echo("")
    header = f"  {'#':>3}  {'id':<20}  {'version':<12}  {'state':<12}  description"
    click.echo(header)
    click.echo("  " + "-" * (len(header) - 2))
    for idx, component in enumerate(manifest.topo_order(), start=1):
        entry = installed.get(component.id)
        if entry is None:
            state_str = "pending"
        elif entry.version != component.version:
            state_str = f"upgrade→{component.version}"
        else:
            state_str = "installed"
        desc = _describe(component)
        click.echo(
            f"  {idx:>3}  {component.id:<20}  {component.version:<12}  "
            f"{state_str:<12}  {desc}"
        )
    click.echo("")
    click.echo(
        "Run `wizinstall run ID ...` or `wizinstall run N ...` to install a subset."
    )


def _describe(component: Component) -> str:
    services = component.all_consul_services()
    if services:
        parts = ", ".join(f"{s.name}:{s.port}" for s in services)
        return f"{component.name} [{parts}]"
    return component.name


@main.command("init-env")
@_shared_opts
@click.option("--output", "output_path", type=click.Path(path_type=Path),
              default=None,
              help="Destination file (default: <manifest_dir>/.stackwiz.env).")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite the destination file if it already exists.")
def init_env(
    manifest_path: Path,
    state_dir: Path,  # noqa: ARG001 — unused but shared across subcommands
    output_path: Path | None,
    force: bool,
) -> None:
    """Generate a commented `.stackwiz.env` scaffold from the manifest.

    Reads every `config:` field + the top-level `domain:`, applies any
    existing overrides, and writes a YAML file with one entry per field
    plus a `# help:` comment line.

    Operators edit the generated file once per deployment, then run
    `wizinstall run` — the TUI pre-fills every field from the file, and
    `run --auto` uses it as the source of truth for required values.
    """
    manifest = _load(manifest_path)
    manifest_dir = manifest_path.parent.resolve()
    target = (output_path or manifest_dir / ".stackwiz.env").resolve()

    if target.exists() and not force:
        click.echo(
            f"error: {target} already exists. Pass --force to overwrite.",
            err=True,
        )
        sys.exit(2)

    from stackwiz.config_overrides import effective_config
    existing = _try_read_existing(target)
    resolved, domain = effective_config(manifest, existing, None)

    header = (
        f"# stackwiz consumer config overrides for "
        f"{manifest.display_name} v{manifest.version}"
    )
    lines: list[str] = []
    lines.append(header)
    lines.append("#")
    lines.append("# EDIT ONLY WHAT YOU NEED TO OVERRIDE.")
    lines.append("# Fields below are commented out — uncomment to override the manifest default.")
    lines.append("# Change `domain:` and every ${domain}-derived field auto-updates at run time.")
    lines.append("# Precedence (highest wins):")
    lines.append("#   <state_dir>/config.yaml > this file > manifest `default:` fields")
    lines.append("")
    lines.append("# ---- Deployment domain ----")
    lines.append("# Drives Consul/Vault service discovery (consul.<domain> / vault.<domain>)")
    lines.append("# and is referenced via ${domain} by other fields in the manifest.")
    lines.append(_yaml_line("domain", domain))
    lines.append("")
    lines.append("# ---- Component configuration ----")
    lines.append("# Values shown in comments are what the current `domain:` resolves to —")
    lines.append("# they're HINTS, not defaults baked into this file. Uncomment a line to")
    lines.append("# force that specific value regardless of the cascade.")
    lines.append("")
    for field in manifest.config:
        if field.help:
            lines.append(f"# {field.label}: {field.help}")
        else:
            lines.append(f"# {field.label}")
        if field.type == "select" and field.choices:
            lines.append(f"#   choices: {', '.join(field.choices)}")
        if field.required:
            lines.append("#   required")
        val = resolved.get(field.id, field.default)
        yaml_line = _yaml_line(field.id, val)
        # If the user explicitly set this field in the current .stackwiz.env,
        # preserve it as an uncommented override. Otherwise emit a commented
        # hint so the cascade stays in effect.
        if field.id in existing and field.id != "domain":
            lines.append(yaml_line)
        else:
            lines.append(f"# {yaml_line}")
        lines.append("")

    # If the manifest uses TLS, hint at the DNS API credentials that
    # bootstrap.sh passes through as env vars (not config fields).
    tls_ids = {f.id for f in manifest.config}
    if tls_ids & {"tls_mode", "certbot_email"}:
        lines.append("# ---- TLS / Let's Encrypt credentials (environment variables) ----")
        lines.append("# These are NOT stackwiz config fields. Set them in your shell or")
        lines.append("# source a .env file BEFORE running bootstrap.sh.")
        lines.append("#")
        lines.append("# Cloudflare DNS-01 (fastest, recommended):")
        lines.append('#   export CF_DNS_API_TOKEN="<your cloudflare api token>"')
        lines.append("#")
        lines.append("# AWS Route53 DNS-01:")
        lines.append('#   export AWS_DNS_ACCESS_KEY_ID="<key>"')
        lines.append('#   export AWS_DNS_SECRET_ACCESS_KEY="<secret>"')
        lines.append("#")
        lines.append("# certbot_email (above) is used as the LE registration email.")
        lines.append("# If no DNS credentials are set and tls_mode is 'auto', the helper")
        lines.append("# falls back to self-signed certs.")
        lines.append("")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    click.echo(f"wrote {target}")

    from stackwiz.secrets_env import (
        SECRETS_ENV_FILENAME,
        load_secrets_env,
        user_secret_specs,
        write_secrets_env_scaffold,
    )
    user_specs = user_secret_specs(manifest)
    if user_specs:
        secrets_target = (manifest_dir / SECRETS_ENV_FILENAME).resolve()
        existing_secrets = load_secrets_env(secrets_target)
        write_secrets_env_scaffold(secrets_target, manifest, existing_secrets, user_specs)
        click.echo(f"wrote {secrets_target}")

    # Idempotently ensure both env files are gitignored. Consumers keep secrets
    # in `.stackwiz.secrets.env` so it MUST never hit git — belt and braces.
    entries = [".stackwiz.env", SECRETS_ENV_FILENAME]
    added = _ensure_gitignore_entries(manifest_dir / ".gitignore", entries)
    if added:
        click.echo(
            f"updated {manifest_dir / '.gitignore'}: added {', '.join(added)}"
        )

    click.echo("Edit it and re-run `wizinstall run` to pick up the overrides.")


def _ensure_gitignore_entries(path: Path, entries: list[str]) -> list[str]:
    """Append missing `entries` to the gitignore at `path`. Returns what was added.

    Creates the file if absent. Idempotent — existing entries (any form of
    leading `./` or trailing whitespace) are recognized and skipped.
    """
    existing_lines: list[str] = []
    existing_set: set[str] = set()
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()
        for line in existing_lines:
            stripped = line.strip()
            if stripped.startswith("./"):
                stripped = stripped[2:]
            elif stripped.startswith("/"):
                stripped = stripped[1:]
            if stripped:
                existing_set.add(stripped)
    missing = [e for e in entries if e not in existing_set]
    if not missing:
        return []
    out = list(existing_lines)
    if out and out[-1] != "":
        out.append("")
    out.extend(missing)
    out.append("")
    path.write_text("\n".join(out), encoding="utf-8")
    return missing


def _try_read_existing(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _yaml_line(key: str, value: object) -> str:
    """Emit `key: value` with appropriate YAML quoting."""
    if value is None:
        return f"{key}: null"
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}"
    if isinstance(value, (int, float)):
        return f"{key}: {value}"
    s = str(value)
    # Always quote strings for safety (avoids YAML parsing surprises for
    # values that look numeric, like IP addresses).
    escaped = s.replace('"', '\\"')
    return f'{key}: "{escaped}"'


@main.command()
@_shared_opts
@click.option("--show-secrets", is_flag=True, default=False,
              help="Include unmasked secret values (reads Vault).")
@click.option("--format", "output_format",
              type=click.Choice(["text", "markdown", "json"]),
              default="text", show_default=True,
              help="Output format.")
def info(
    manifest_path: Path,
    state_dir: Path,
    show_secrets: bool,
    output_format: str,
) -> None:
    """Show installed components, URLs, and secret paths."""
    manifest = _load(manifest_path)
    state_dir = _resolve_state_dir(state_dir, manifest)
    state_dir.mkdir(parents=True, exist_ok=True)
    from stackwiz.info import render_info

    rc = render_info(
        manifest=manifest,
        state_dir=state_dir,
        show_secrets=show_secrets,
        output_format=output_format,
    )
    sys.exit(rc)


def _dispatch(
    mode: str,
    manifest: Manifest,
    state_dir: Path,
    manifest_dir: Path,
    auto_mode: bool,
    selected_override: set[str] | None = None,
    forced_refresh: set[str] | None = None,
) -> None:
    if auto_mode:
        from stackwiz.headless import run_headless
        env_file = manifest_dir / ".stackwiz.env"
        sys.exit(run_headless(
            manifest=manifest,
            state_dir=state_dir,
            manifest_dir=manifest_dir,
            mode=mode,
            config_env_file=env_file,
            selected_override=selected_override,
            forced_refresh=forced_refresh,
        ))

    from stackwiz.app import InstallerApp
    app = InstallerApp(
        manifest=manifest,
        state_dir=state_dir,
        mode=mode,  # type: ignore[arg-type]
        manifest_dir=manifest_dir,
    )
    if selected_override is not None:
        app.selected_component_ids = selected_override
        app.selection_locked = True
    if forced_refresh is not None:
        app.forced_refresh = forced_refresh
    sys.exit(app.run() or 0)
