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
            state_str = f"upgrade->{component.version}"
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
@click.argument("domain", required=False, default=None)
def init_env(
    manifest_path: Path,
    state_dir: Path,  # noqa: ARG001 — unused but shared across subcommands
    output_path: Path | None,
    force: bool,
    domain: str | None,
) -> None:
    """Generate a commented `.stackwiz.env` scaffold from the manifest.

    Reads every `config:` field + the top-level `domain:`, applies any
    existing overrides, and writes a YAML file with one entry per field
    plus a `# help:` comment line.

    Optional positional DOMAIN argument — when supplied, the generated
    file's top-level `domain:` is set to that value (and every
    `${domain}`-derived field in the comments renders against it):

    \b
      wizinstall init-env                 # uses manifest's default domain
      wizinstall init-env mycompany.lan   # overrides with mycompany.lan

    Operators edit the generated file once per deployment, then run
    `wizinstall run` — the TUI pre-fills every field from the file, and
    `run --auto` uses it as the source of truth for required values.
    """
    from stackwiz.scaffold import scaffold_env_files

    manifest = _load(manifest_path)
    manifest_dir = manifest_path.parent.resolve()
    target = (output_path or manifest_dir / ".stackwiz.env").resolve()
    try:
        result = scaffold_env_files(
            manifest, manifest_dir, target, force, domain=domain,
        )
    except FileExistsError as exc:
        click.echo(
            f"error: {exc} already exists. Pass --force to overwrite.",
            err=True,
        )
        sys.exit(2)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)

    click.echo(f"wrote {result.env_file}")
    if result.secrets_file is not None:
        click.echo(f"wrote {result.secrets_file}")
    if result.dot_env_file is not None:
        click.echo(f"wrote {result.dot_env_file}")
    if result.gitignore_added:
        click.echo(
            f"updated {manifest_dir / '.gitignore'}: added "
            f"{', '.join(result.gitignore_added)}"
        )
    click.echo("Edit it and re-run `wizinstall run` to pick up the overrides.")


@main.command("extract-bootstrap")
@click.option("--output", "output_dir", type=click.Path(file_okay=False, path_type=Path),
              default=None,
              help="Destination directory (default: stdout for library + templates).")
@click.option("--launcher-name", "launcher_name", default="bootstrap.sh", show_default=True,
              help="Filename for the framework-managed launcher when --output is set.")
@click.option("--config-name", "config_name", default="bootstrap.conf.sh", show_default=True,
              help="Filename for the consumer-owned config when --output is set.")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite the framework-managed library + launcher. "
                   "Does NOT overwrite the consumer config.")
def extract_bootstrap(
    output_dir: Path | None,
    launcher_name: str,
    config_name: str,
    force: bool,
) -> None:
    """Emit the bootstrap triplet for a consumer.

    Three files are written, with different overwrite semantics:

    \b
      stackwiz-bootstrap.sh   framework-managed library (do not edit)
      bootstrap.sh            framework-managed launcher (do not edit)
      bootstrap.conf.sh       consumer-owned config (EDIT ME)

    Re-running --force refreshes the framework pair without touching
    bootstrap.conf.sh, so vendored consumer repos can safely pull in
    library/launcher fixes without losing per-project customization.

    With no --output: writes the library to stdout and prints the
    launcher + config templates to stderr for inspection.
    """
    from stackwiz.scaffold import (
        BOOTSTRAP_CONFIG_TEMPLATE,
        read_bootstrap_launcher_text,
        read_bootstrap_library_text,
        write_bootstrap,
    )

    if output_dir is None:
        click.echo(read_bootstrap_library_text(), nl=False)
        click.echo("\n# ---- launcher (bootstrap.sh) ----", err=True)
        click.echo(read_bootstrap_launcher_text(), err=True, nl=False)
        click.echo("\n# ---- config template (bootstrap.conf.sh) ----", err=True)
        click.echo(BOOTSTRAP_CONFIG_TEMPLATE, err=True, nl=False)
        return

    try:
        result = write_bootstrap(
            output_dir,
            launcher_name=launcher_name,
            config_name=config_name,
            force=force,
        )
    except FileExistsError as exc:
        click.echo(f"error: {exc} exists (pass --force to overwrite)", err=True)
        sys.exit(2)
    click.echo(f"wrote {result.lib_path}")
    click.echo(f"wrote {result.launcher_path}")
    if result.config_created:
        click.echo(f"wrote {result.config_path}")
    else:
        click.echo(f"preserved existing {result.config_path}")
    click.echo("Edit the SW_* arrays in bootstrap.conf.sh, then: ./bootstrap.sh validate")


@main.command("vault-shred-init")
@_shared_opts
@click.option("--yes", "confirmed", is_flag=True, default=False,
              help="Skip interactive confirmation. Use in automation.")
def vault_shred_init(
    manifest_path: Path,
    state_dir: Path,
    confirmed: bool,
) -> None:
    """Securely delete <state_dir>/vault-init.json.

    vault-init.json contains the Vault root token and unseal keys in
    cleartext. It must be backed up off-host and then deleted. This
    subcommand overwrites the on-disk bytes with zeros + fsyncs + unlinks.
    Best-effort against forensic recovery on COW / flash; the real
    rotation path is "off-host backup + re-init".
    """
    from stackwiz.vault_client import shred_vault_init

    manifest = _load(manifest_path)
    state_dir = _resolve_state_dir(state_dir, manifest)
    target = state_dir / "vault-init.json"
    if not target.exists():
        click.echo(f"ok: {target} does not exist", err=True)
        return
    if not confirmed:
        click.echo(
            "This will PERMANENTLY delete the Vault root token and unseal keys.\n"
            "You MUST have copied vault-init.json off this host already."
        )
        click.confirm("Proceed with shred?", abort=True)
    removed = shred_vault_init(state_dir)
    if removed is None:
        click.echo("error: shred failed (see stderr)", err=True)
        sys.exit(1)
    click.echo(f"shredded {removed}")


@main.command("backup-cert")
@click.argument("out_dir", type=click.Path(file_okay=False, path_type=Path),
                required=False)
def backup_cert(out_dir: Path | None) -> None:
    """Save TLS cert material to a tarball (default: current dir).

    Captures /etc/stackwiz/tls (self-signed CA + leaves + BYOC
    overrides) and /etc/letsencrypt (LE state). Derived copies under
    /opt/stackwiz/ are regenerated from these sources by install
    scripts on the next `./bootstrap.sh run`, so they're deliberately
    out of scope.
    """
    from stackwiz.cert_backup import CertBackupError, backup, encrypt_hint

    target = out_dir or Path.cwd()
    try:
        tarball = backup(target)
    except CertBackupError as exc:
        click.echo(f"backup-cert: {exc}", err=True)
        sys.exit(1)
    click.echo(f"wrote {tarball} ({tarball.stat().st_size // 1024} KiB)")
    click.echo("")
    click.echo("The tarball contains CA + Let's Encrypt private keys.")
    click.echo("Encrypt before moving off-host:")
    click.echo(f"  {encrypt_hint(tarball)}")


@main.command("restore-cert")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite existing cert dirs (moved aside first).")
@click.argument("tarball", type=click.Path(exists=True, dir_okay=False,
                                           path_type=Path))
def restore_cert(tarball: Path, force: bool) -> None:
    """Restore TLS cert material from a tarball created by backup-cert."""
    from stackwiz.cert_backup import CertBackupError, restore

    try:
        restored = restore(tarball, force=force)
    except CertBackupError as exc:
        click.echo(f"restore-cert: {exc}", err=True)
        sys.exit(1)
    if not restored:
        click.echo(
            "no paths restored — either nothing in the tarball applies, "
            "or every target already exists (use --force to overwrite)",
        )
        return
    click.echo("")
    click.echo("Next: ./bootstrap.sh run --auto")
    click.echo("  stackwiz-tls.sh's 30-day freshness check reuses the")
    click.echo("  restored certs, install scripts re-populate derived")
    click.echo("  copies (/opt/stackwiz/{vault,nginx}/tls, /var/lib/")
    click.echo("  stackwiz/shared/vault-ca.crt).")


@main.command("inspect-cert")
@click.argument("tarball", type=click.Path(exists=True, dir_okay=False,
                                           path_type=Path))
def inspect_cert(tarball: Path) -> None:
    """Show the manifest + file list of a cert backup tarball."""
    from stackwiz.cert_backup import CertBackupError, inspect

    try:
        click.echo(inspect(tarball), nl=False)
    except CertBackupError as exc:
        click.echo(f"inspect-cert: {exc}", err=True)
        sys.exit(1)


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
    manifest_dir = manifest_path.parent.resolve()
    from stackwiz.info import render_info

    rc = render_info(
        manifest=manifest,
        state_dir=state_dir,
        show_secrets=show_secrets,
        output_format=output_format,
        manifest_dir=manifest_dir,
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
