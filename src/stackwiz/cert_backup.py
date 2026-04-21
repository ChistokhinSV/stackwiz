"""TLS certificate backup / restore.

Narrow scope: captures only crypto material whose loss triggers a full
cert re-issue cycle on a fresh VM. Vault data, Consul KV, Authentik DB,
and docker named volumes are NOT in scope — a full-backup tool will
cover them separately.

Paths we care about (all host-side bind-mounted into the installer
container by the bootstrap's cert-command fast-path):

  /etc/stackwiz/tls    self-signed CA root + per-host leaves + BYOC
                       overrides (custom/<host>/...)
  /etc/letsencrypt     LE account + live/archive + renewal-hooks +
                       credential files (.stackwiz-cloudflare.ini,
                       .stackwiz-route53.env)

Tarball layout::

  manifest.txt                       # plain-text summary
  files/etc/stackwiz/tls/...         # verbatim tree
  files/etc/letsencrypt/...

The restore path refuses to clobber existing dirs unless ``force=True``;
when force is set, existing content is moved aside to a timestamped
sibling so the operator can still recover if they ran restore by
mistake. After restore, install scripts (``stackwiz-tls.sh``'s 30-day
freshness check) reuse the restored certs and re-populate derived
copies (/opt/stackwiz/{vault,nginx}/tls, /var/lib/stackwiz/shared
/vault-ca.crt) on the next ``./bootstrap.sh`` run.
"""
from __future__ import annotations

import os
import shutil
import socket
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Primary cert dirs. Order matters only for readability; restore treats
# each independently. Paths reflect what install/stackwiz-tls.sh writes.
CERT_PATHS: tuple[Path, ...] = (
    Path("/etc/stackwiz/tls"),
    Path("/etc/letsencrypt"),
)


class CertBackupError(RuntimeError):
    """Raised for operator-facing failures (missing dirs, bad tarball)."""


def _strip_anchor(p: Path) -> Path:
    """Return path minus its root/anchor (leading '/', drive letter, …).

    ``Path.relative_to('/')`` works on POSIX but blows up on Windows when
    the source path carries a drive letter. Using ``parts[1:]`` drops
    whatever the first anchor element is (``/``, ``C:\\``, etc.) and
    preserves the rest unchanged. Production always runs under Linux
    inside the installer container where this is a no-op; it only
    matters for dev-host pytest runs on Windows.
    """
    parts = p.parts
    return Path(*parts[1:]) if parts else p


# --- backup ----------------------------------------------------------------


def _host_tag() -> str:
    """Short hostname sanitised for filenames.

    Reads ``SW_HOST_HOSTNAME`` first so the bootstrap can pass the real
    VM hostname — inside a docker container, ``socket.gethostname()``
    returns the container id unless --uts=host is set, which the
    installer container doesn't use.
    """
    forced = os.environ.get("SW_HOST_HOSTNAME", "").strip()
    raw = forced or socket.gethostname()
    tag = "".join(c if c.isalnum() or c == "-" else "_" for c in raw.split(".")[0])
    while "__" in tag:
        tag = tag.replace("__", "_")
    return tag.strip("_") or "host"


def backup(out_dir: Path) -> Path:
    """Write a gzipped tarball of cert material under ``out_dir``.

    Returns the absolute path of the created tarball. Raises
    ``CertBackupError`` if no cert directory exists on the host
    (nothing to back up).
    """
    out_dir = out_dir.resolve()
    if not out_dir.is_dir():
        raise CertBackupError(f"output dir {out_dir} does not exist")

    present: list[Path] = [p for p in CERT_PATHS if p.exists()]
    if not present:
        raise CertBackupError(
            "no cert paths on this host — /etc/stackwiz/tls and "
            "/etc/letsencrypt are both absent",
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_tar = out_dir / f"stackwiz-certs-{_host_tag()}-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory() as work_str:
        work = Path(work_str)
        files_root = work / "files"
        files_root.mkdir()

        # Preserve symlinks (LE's live/<host>/ → ../archive/<host>/N) and
        # modes (0600 on private keys). shutil.copytree + copy_function
        # copy2 preserves mtime; symlinks=True keeps LE's layout intact.
        for p in present:
            dst = files_root / _strip_anchor(p)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(p, dst, symlinks=True)

        _write_manifest(work / "manifest.txt", present, stamp)

        with tarfile.open(out_tar, "w:gz") as tar:
            tar.add(work / "manifest.txt", arcname="manifest.txt")
            tar.add(files_root, arcname="files")

    out_tar.chmod(0o600)
    return out_tar


def _write_manifest(path: Path, included: list[Path], stamp: str) -> None:
    lines = [
        "# stackwiz certificate backup",
        f"source_host: {_fqdn()}",
        f"created_at_utc: {stamp}",
        "tool: wizinstall backup-cert",
        "",
        "# included paths",
    ]
    for p in CERT_PATHS:
        lines.append(f"  {'included' if p in included else 'skipped '}: {p}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fqdn() -> str:
    forced = os.environ.get("SW_HOST_HOSTNAME", "").strip()
    if forced:
        return forced
    try:
        return socket.getfqdn()
    except OSError:
        return socket.gethostname()


# --- inspect ---------------------------------------------------------------


def inspect(tarball: Path) -> str:
    """Return a human-readable summary of ``tarball``'s contents."""
    tarball = _resolve_tarball(tarball)
    chunks = ["# source:"]
    with tarfile.open(tarball, "r:gz") as tar:
        try:
            mf = tar.extractfile("manifest.txt")
        except KeyError:
            mf = None
        if mf is not None:
            chunks.append(mf.read().decode("utf-8", errors="replace").rstrip())
        chunks.append("")
        chunks.append("# archive tree")
        for member in tar.getmembers():
            if member.name in ("manifest.txt", "files"):
                continue
            if member.name.startswith("files/"):
                chunks.append(f"  {member.name[len('files/'):]}")
    return "\n".join(chunks) + "\n"


# --- restore ---------------------------------------------------------------


def restore(tarball: Path, force: bool = False) -> list[Path]:
    """Expand ``tarball`` back into place. Returns the restored host paths.

    Existing directories are preserved unless ``force=True``, in which
    case they're moved to ``<path>.before-restore-<utc>`` first. Private
    keys are re-chmodded to 0600 post-restore regardless of what the
    tarball carried.
    """
    tarball = _resolve_tarball(tarball)

    with tempfile.TemporaryDirectory() as work_str:
        work = Path(work_str)
        with tarfile.open(tarball, "r:gz") as tar:
            _safe_extract(tar, work)

        restored: list[Path] = []
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        files_root = work / "files"
        if not files_root.is_dir():
            raise CertBackupError(
                f"{tarball.name} is missing the top-level files/ tree",
            )

        for host_path in CERT_PATHS:
            src = files_root / _strip_anchor(host_path)
            if not src.exists():
                continue
            if host_path.exists():
                if not force:
                    print(f"  ! {host_path} already exists (pass --force to overwrite)")
                    continue
                backup_path = Path(f"{host_path}.before-restore-{stamp}")
                host_path.rename(backup_path)
                print(f"  ~ moved existing {host_path} -> {backup_path}")
            host_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, host_path, symlinks=True)
            restored.append(host_path)
            print(f"  + restored {host_path}")

        _reassert_private_key_perms()
    return restored


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Python 3.12+ extractfilter='data' blocks absolute paths + ..

    The tarballs we produce carry only ``files/...`` and ``manifest.txt``
    so filter='data' is strict enough; use it to avoid CVE-style
    traversal on malicious tarballs.
    """
    try:
        tar.extractall(dest, filter="data")  # type: ignore[arg-type]
    except TypeError:
        # Python <3.12 — best-effort sanity check.
        for m in tar.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts:
                raise CertBackupError(f"unsafe tar member: {m.name!r}")
        tar.extractall(dest)


def _reassert_private_key_perms() -> None:
    """Flip *.key under /etc/stackwiz/tls back to 0600 + *.crt to 0644.

    Cert restore may land files with whatever mode the tarball carried.
    cp -a preserved mode at backup time, but defence-in-depth: explicit
    re-assert avoids leaking a private key if the tarball was ever
    processed by a tool that changed modes.
    """
    tls_dir = Path("/etc/stackwiz/tls")
    if not tls_dir.is_dir():
        return
    for p in tls_dir.rglob("*.key"):
        if p.is_file():
            try:
                p.chmod(0o600)
            except OSError:
                pass
    for p in tls_dir.rglob("*.crt"):
        if p.is_file():
            try:
                p.chmod(0o644)
            except OSError:
                pass


def _resolve_tarball(tarball: Path) -> Path:
    tarball = tarball.resolve()
    if not tarball.is_file():
        raise CertBackupError(f"tarball {tarball} not found")
    return tarball


# --- encryption helpers ----------------------------------------------------
#
# gpg is left to the operator (interactive passphrase prompt doesn't
# belong inside an install pipeline). These helpers exist so CLI output
# can guide them without duplicating the command string.


def encrypt_hint(tarball: Path) -> str:
    return (
        f"gpg --symmetric --cipher-algo AES256 '{tarball}'   # encrypt before off-host move"
    )


def decrypt_hint(tarball_gpg: Path) -> str:
    return (
        f"gpg -d '{tarball_gpg}' > '{tarball_gpg.with_suffix('')}'   # restore plaintext"
    )
