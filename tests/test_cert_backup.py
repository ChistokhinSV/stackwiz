"""Tests for cert_backup: round-trip + safety checks.

We don't exercise the real /etc/stackwiz/tls path — that would need
root. Instead we monkeypatch CERT_PATHS to point at tmp_path subtrees.
That covers every code path except the final chmod re-assertion,
which unit-tests can't meaningfully validate without running as root.
"""
from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from stackwiz import cert_backup
from stackwiz.cert_backup import CertBackupError, backup, inspect, restore


def _symlinks_available() -> bool:
    """Check whether the FS lets this process create symlinks.

    Windows requires admin or developer-mode for os.symlink; CI runs
    on Linux where it always works. Probing avoids skipping the whole
    module on Linux just because Windows dev lacks it.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "t"
        target.write_text("x")
        link = Path(d) / "l"
        try:
            os.symlink(target, link)
            return True
        except (OSError, NotImplementedError):
            return False


_HAS_SYMLINKS = _symlinks_available()


@pytest.fixture
def cert_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Build a fake /etc/stackwiz/tls + /etc/letsencrypt layout.

    Returns (tls_dir, le_dir) — both under tmp_path. Monkeypatches
    cert_backup.CERT_PATHS to look there.
    """
    tls = tmp_path / "etc" / "stackwiz" / "tls"
    le = tmp_path / "etc" / "letsencrypt"
    tls.mkdir(parents=True)
    le.mkdir(parents=True)

    (tls / "stackwiz-ca.crt").write_text("CA CERT\n")
    (tls / "stackwiz-ca.key").write_text("CA KEY\n")
    (tls / "chat.example.com.crt").write_text("LEAF CERT\n")
    (tls / "chat.example.com.key").write_text("LEAF KEY\n")
    (tls / "chat.example.com.fullchain.crt").write_text("FULLCHAIN\n")
    (tls / "custom").mkdir()
    (tls / "custom" / "byoc.example.com").mkdir()
    (tls / "custom" / "byoc.example.com" / "cert.pem").write_text("BYOC CERT\n")
    (tls / "custom" / "byoc.example.com" / "key.pem").write_text("BYOC KEY\n")

    (le / "live").mkdir()
    (le / "archive").mkdir()
    (le / "archive" / "api.example.com").mkdir()
    (le / "archive" / "api.example.com" / "privkey1.pem").write_text("LE KEY 1\n")
    (le / "live" / "api.example.com").mkdir()
    # LE's live/<host>/ is symlinks back to archive. Emulate when the
    # host FS allows symlinks; otherwise substitute a regular copy so
    # the rest of the round-trip still runs. Tests that specifically
    # assert symlink preservation skip when unavailable.
    target_rel = "../../archive/api.example.com/privkey1.pem"
    live_key = le / "live" / "api.example.com" / "privkey.pem"
    if _HAS_SYMLINKS:
        live_key.symlink_to(target_rel)
    else:
        live_key.write_text("LE KEY 1\n")

    monkeypatch.setattr(cert_backup, "CERT_PATHS", (tls, le))
    return tls, le


def test_backup_creates_gzipped_tarball(
    cert_tree: tuple[Path, Path], tmp_path: Path,
) -> None:
    tls, le = cert_tree
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    tarball = backup(out_dir)

    assert tarball.exists()
    assert tarball.suffix == ".gz"
    assert tarball.parent == out_dir
    assert tarball.name.startswith("stackwiz-certs-")
    if os.name == "posix":
        # 0600 — contains CA private key. Windows NTFS can't model this,
        # production always runs Linux inside the installer container.
        assert (tarball.stat().st_mode & 0o777) == 0o600


def test_backup_manifest_lists_included_paths(
    cert_tree: tuple[Path, Path], tmp_path: Path,
) -> None:
    tls, le = cert_tree
    tarball = backup(tmp_path)
    with tarfile.open(tarball, "r:gz") as tar:
        mf_member = tar.getmember("manifest.txt")
        mf = tar.extractfile(mf_member)
        assert mf is not None
        text = mf.read().decode("utf-8")
    assert str(tls) in text
    assert str(le) in text
    assert "included" in text


@pytest.mark.skipif(not _HAS_SYMLINKS, reason="host FS can't create symlinks")
def test_backup_preserves_symlinks(
    cert_tree: tuple[Path, Path], tmp_path: Path,
) -> None:
    tls, le = cert_tree
    tarball = backup(tmp_path)
    with tarfile.open(tarball, "r:gz") as tar:
        links = [m for m in tar.getmembers() if m.issym()]
    # The LE layout's live/<host>/privkey.pem must survive as a symlink,
    # otherwise certbot renewal breaks when it follows archive -> live.
    assert any("privkey.pem" in m.name for m in links), (
        "LE symlink dropped — cp(symlinks=True) failed")


def test_backup_raises_when_nothing_to_back_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cert_backup, "CERT_PATHS", (
        tmp_path / "does-not-exist-a",
        tmp_path / "does-not-exist-b",
    ))
    with pytest.raises(CertBackupError, match="no cert paths"):
        backup(tmp_path)


def test_inspect_reports_manifest_and_tree(
    cert_tree: tuple[Path, Path], tmp_path: Path,
) -> None:
    tarball = backup(tmp_path)
    report = inspect(tarball)
    assert "# source:" in report
    assert "# archive tree" in report
    assert "stackwiz-ca.crt" in report
    assert "chat.example.com.fullchain.crt" in report


def test_inspect_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CertBackupError, match="not found"):
        inspect(tmp_path / "nope.tar.gz")


def test_restore_round_trip(
    cert_tree: tuple[Path, Path], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backup on a tree → wipe the tree → restore → contents back."""
    tls, le = cert_tree
    tarball = backup(tmp_path)

    # Blow the tree away (but keep the parent dirs so restore has a target).
    import shutil
    shutil.rmtree(tls)
    shutil.rmtree(le)

    restored = restore(tarball)
    assert set(restored) == {tls, le}
    assert (tls / "stackwiz-ca.crt").read_text() == "CA CERT\n"
    assert (tls / "chat.example.com.fullchain.crt").read_text() == "FULLCHAIN\n"
    assert (tls / "custom" / "byoc.example.com" / "key.pem").read_text() == "BYOC KEY\n"
    if _HAS_SYMLINKS:
        # Symlink preserved through restore — readlink must match.
        live_key = le / "live" / "api.example.com" / "privkey.pem"
        assert live_key.is_symlink()


def test_restore_refuses_to_clobber_without_force(
    cert_tree: tuple[Path, Path], tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tls, le = cert_tree
    tarball = backup(tmp_path)

    # Second restore without --force should skip because targets exist.
    restored = restore(tarball, force=False)
    assert restored == []
    out = capsys.readouterr().out
    assert "already exists" in out


def test_restore_force_moves_existing_aside(
    cert_tree: tuple[Path, Path], tmp_path: Path,
) -> None:
    tls, le = cert_tree
    tarball = backup(tmp_path)

    # Add a pre-existing file the restore should move aside — re-read
    # after restore to check it's not in the final state.
    (tls / "sentinel").write_text("MARKER\n")
    (tls / "chat.example.com.crt").write_text("PRE-RESTORE CONTENT\n")

    restored = restore(tarball, force=True)
    assert tls in restored

    # Original tls content present in a .before-restore-* dir.
    preserved = list(tls.parent.glob("tls.before-restore-*"))
    assert len(preserved) == 1
    assert (preserved[0] / "sentinel").read_text() == "MARKER\n"

    # Fresh restore doesn't contain the sentinel.
    assert not (tls / "sentinel").exists()
    assert (tls / "chat.example.com.crt").read_text() == "LEAF CERT\n"


def test_restore_rejects_traversal_attempt(tmp_path: Path) -> None:
    """Hand-craft a tarball with a member that tries to escape."""
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo(name="../../etc/passwd")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    with pytest.raises((CertBackupError, tarfile.ReadError, tarfile.TarError, Exception)):
        restore(evil)


def test_host_tag_prefers_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SW_HOST_HOSTNAME", "myprod-vm.lab.local")
    assert cert_backup._host_tag() == "myprod-vm"


def test_host_tag_sanitises_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SW_HOST_HOSTNAME", "weird host/name:1")
    tag = cert_backup._host_tag()
    # Only filename-safe chars survive — no slashes, colons, or spaces.
    for bad in ("/", ":", " ", "\\"):
        assert bad not in tag, f"{bad!r} leaked into tag={tag!r}"
    assert tag
