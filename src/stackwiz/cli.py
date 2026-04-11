"""Entry point: `wizinstall`."""
from __future__ import annotations

import sys
from pathlib import Path

import click

from stackwiz import __version__
from stackwiz.manifest import load_manifest


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("/manifest/components.yaml"),
    show_default=True,
    help="Path to the components.yaml manifest.",
)
@click.option(
    "--state",
    "state_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("/state"),
    show_default=True,
    help="Directory for persistent installer state.",
)
@click.option(
    "--uninstall",
    "mode_uninstall",
    is_flag=True,
    default=False,
    help="Run in uninstall mode (reverse teardown).",
)
@click.option(
    "--validate",
    "validate_only",
    is_flag=True,
    default=False,
    help="Validate manifest and exit without launching the TUI.",
)
@click.option(
    "--auto",
    "auto_mode",
    is_flag=True,
    default=False,
    help="Run headless (no TUI). Required config values come from "
         "`/manifest/.stackwiz.env` (YAML key: value) or fall back to manifest defaults.",
)
@click.version_option(__version__, "-V", "--version")
def main(
    manifest_path: Path,
    state_dir: Path,
    mode_uninstall: bool,
    validate_only: bool,
    auto_mode: bool,
) -> None:
    """Interactive TUI installer for declarative component manifests."""
    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        click.echo(f"manifest error: {exc}", err=True)
        sys.exit(2)

    if validate_only:
        click.echo(f"ok: {manifest.display_name} v{manifest.version}")
        click.echo(f"  domain: {manifest.domain}")
        click.echo(f"  components: {len(manifest.components)}")
        click.echo(f"  order: {' -> '.join(c.id for c in manifest.topo_order())}")
        sys.exit(0)

    state_dir.mkdir(parents=True, exist_ok=True)

    mode = "uninstall" if mode_uninstall else "install"
    manifest_dir = manifest_path.parent.resolve()

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
        mode=mode,
        manifest_dir=manifest_dir,
    )
    sys.exit(app.run() or 0)
