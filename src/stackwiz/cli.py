"""Entry point: `wizinstall`.

Subcommands:
  run         Launch the TUI installer (default if no subcommand given).
  uninstall   Launch the TUI in uninstall mode.
  validate    Validate manifest and exit.
  info        Show installed components, service URLs, and secret paths.

All subcommands share --manifest and --state. `run` and `uninstall` accept
`--auto` for headless execution.
"""
from __future__ import annotations

import sys
from pathlib import Path

import click

from stackwiz import __version__
from stackwiz.manifest import Manifest, load_manifest


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
def run(manifest_path: Path, state_dir: Path, auto_mode: bool) -> None:
    """Install components from the manifest."""
    manifest = _load(manifest_path)
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = manifest_path.parent.resolve()
    _dispatch("install", manifest, state_dir, manifest_dir, auto_mode)


@main.command()
@_shared_opts
@click.option("--auto", "auto_mode", is_flag=True, default=False,
              help="Run headless (no TUI).")
def uninstall(manifest_path: Path, state_dir: Path, auto_mode: bool) -> None:
    """Remove components in reverse order."""
    manifest = _load(manifest_path)
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir = manifest_path.parent.resolve()
    _dispatch("uninstall", manifest, state_dir, manifest_dir, auto_mode)


@main.command()
@_shared_opts
def validate(manifest_path: Path, state_dir: Path) -> None:  # noqa: ARG001
    """Validate manifest and print the install order."""
    manifest = _load(manifest_path)
    click.echo(f"ok: {manifest.display_name} v{manifest.version}")
    click.echo(f"  domain: {manifest.domain}")
    click.echo(f"  components: {len(manifest.components)}")
    click.echo(f"  order: {' -> '.join(c.id for c in manifest.topo_order())}")


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
        ))

    from stackwiz.app import InstallerApp
    app = InstallerApp(
        manifest=manifest,
        state_dir=state_dir,
        mode=mode,  # type: ignore[arg-type]
        manifest_dir=manifest_dir,
    )
    sys.exit(app.run() or 0)
