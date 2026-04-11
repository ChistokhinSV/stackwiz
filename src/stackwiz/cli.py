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

import sys
from pathlib import Path

import click

from stackwiz import __version__
from stackwiz.manifest import Component, Manifest, load_manifest


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
@click.argument("components", nargs=-1)
def run(
    manifest_path: Path,
    state_dir: Path,
    auto_mode: bool,
    components: tuple[str, ...],
) -> None:
    """Install components from the manifest.

    With no COMPONENTS args, installs everything required+default (headless)
    or prompts via the TUI. With COMPONENTS args, installs only the listed
    components (ids or 1-based indices from `wizinstall list`).
    """
    manifest = _load(manifest_path)
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = manifest_path.parent.resolve()
    selected_override: set[str] | None = None
    if components:
        selected_override = resolve_selection(manifest, components)
    _dispatch(
        "install", manifest, state_dir, manifest_dir, auto_mode, selected_override
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
    sys.exit(app.run() or 0)
