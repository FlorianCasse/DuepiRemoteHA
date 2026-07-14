"""Safety tests for the legacy Duepi migration utility."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

import migrate


class FileBackedRemoteExecutor(migrate.RemoteExecutor):
    """Remote executor double backed by a temporary local filesystem."""

    def __init__(self, root: Path) -> None:
        super().__init__("test-ha")
        self.root = root

    def _path(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def file_exists(self, path: str) -> bool:
        return self._path(path).is_file()

    def dir_exists(self, path: str) -> bool:
        return self._path(path).is_dir()

    def read_file(self, path: str) -> str | None:
        target = self._path(path)
        return target.read_text() if target.is_file() else None

    def mkdir(self, path: str) -> None:
        self._path(path).mkdir(parents=True, exist_ok=True)

    def copy(self, src: str, dst: str) -> None:
        source = self._path(src)
        target = self._path(dst)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text())

    def remove(self, path: str) -> None:
        self._path(path).unlink()

    def remove_dir(self, path: str) -> None:
        import shutil

        shutil.rmtree(self._path(path))


class FailingCopySshExecutor(FileBackedRemoteExecutor):
    """Simulate an SSH copy failure before an existing backup is changed."""

    def copy(self, src: str, dst: str) -> None:
        if src.endswith("/stoveOnOff.py"):
            raise RuntimeError("Remote command failed: cp: permission denied")
        super().copy(src, dst)


class CorruptingCopySshExecutor(FileBackedRemoteExecutor):
    """Simulate a remote transport that returns corrupted copied content."""

    def copy(self, src: str, dst: str) -> None:
        super().copy(src, dst)
        self._path(dst).write_text("corrupted")


def _write_old_installation(root: Path, *, integration: bool = True) -> None:
    scripts = root / "config" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "stoveOnOff.py").write_text("print('legacy')\n")
    (scripts / ".env").write_text("DUEPI_DEVICE_ID=device-123\n")
    (root / "config" / "configuration.yaml").write_text("homeassistant: {}\n")
    if integration:
        component = root / "config" / "custom_components" / "duepi"
        component.mkdir(parents=True)
        (component / "__init__.py").write_text("DOMAIN = 'duepi'\n")


def test_remote_nonzero_command_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every failed remote shell operation must surface its command and stderr."""
    monkeypatch.setattr(
        migrate.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="permission denied"
        ),
    )

    with pytest.raises(RuntimeError, match=r"Remote command failed.*permission denied"):
        migrate.RemoteExecutor("test-ha").copy("/config/source", "/config/destination")


def test_remote_timeout_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out remote shell operation identifies both host and timeout."""
    monkeypatch.setattr(
        migrate.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="ssh test-ha", timeout=30)
        ),
    )

    with pytest.raises(RuntimeError, match=r"timed out after 30s on test-ha"):
        migrate.RemoteExecutor("test-ha").copy("/config/source", "/config/destination")


def test_connection_check_propagates_remote_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """The connection probe cannot hide an SSH failure from a caller."""
    monkeypatch.setattr(
        migrate.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=255, stdout="", stderr="Connection refused"
        ),
    )

    with pytest.raises(RuntimeError, match="Connection refused"):
        migrate.RemoteExecutor("test-ha").test_connection()


def test_local_copy_failure_is_not_suppressed(tmp_path: Path) -> None:
    """Local operations propagate the same underlying file failure to callers."""
    with pytest.raises(FileNotFoundError):
        migrate.LocalExecutor().copy(str(tmp_path / "missing"), str(tmp_path / "destination"))


def test_migration_aborts_before_backup_when_integration_is_missing(tmp_path: Path) -> None:
    """A missing new integration leaves every legacy file untouched."""
    _write_old_installation(tmp_path, integration=False)
    executor = FileBackedRemoteExecutor(tmp_path)

    migrate.run_migration(executor, "/config", interactive=False)

    assert (tmp_path / "config" / "scripts" / "stoveOnOff.py").is_file()
    assert (tmp_path / "config" / "scripts" / ".env").is_file()
    assert not (tmp_path / "config" / "duepi_migration_backup").exists()


def test_failing_ssh_copy_keeps_sources_and_existing_backup_intact(tmp_path: Path) -> None:
    """A copy failure cannot cause the old installation or prior backup to be lost."""
    _write_old_installation(tmp_path)
    backup = tmp_path / "config" / "duepi_migration_backup"
    backup.mkdir()
    (backup / "stoveOnOff.py").write_text("previous backup\n")
    executor = FailingCopySshExecutor(tmp_path)

    with pytest.raises(RuntimeError, match="permission denied"):
        migrate.run_migration(executor, "/config", interactive=False)

    assert (tmp_path / "config" / "scripts" / "stoveOnOff.py").is_file()
    assert (tmp_path / "config" / "scripts" / ".env").is_file()
    assert (backup / "stoveOnOff.py").read_text() == "previous backup\n"
    assert not (backup / ".env").exists()


def test_migration_removes_sources_only_after_verified_backups(tmp_path: Path) -> None:
    """A successful migration verifies backups before deleting the legacy files."""
    _write_old_installation(tmp_path)
    executor = FileBackedRemoteExecutor(tmp_path)

    migrate.run_migration(executor, "/config", interactive=False)

    scripts = tmp_path / "config" / "scripts"
    backup = tmp_path / "config" / "duepi_migration_backup"
    assert not (scripts / "stoveOnOff.py").exists()
    assert not (scripts / ".env").exists()
    assert (backup / "stoveOnOff.py").read_text() == "print('legacy')\n"
    assert (backup / ".env").read_text() == "DUEPI_DEVICE_ID=device-123\n"


def test_migration_preserves_sources_when_backup_content_is_not_verified(tmp_path: Path) -> None:
    """A corrupt backup aborts migration before any legacy file is deleted."""
    _write_old_installation(tmp_path)
    executor = CorruptingCopySshExecutor(tmp_path)

    migrate.run_migration(executor, "/config", interactive=False)

    scripts = tmp_path / "config" / "scripts"
    assert (scripts / "stoveOnOff.py").is_file()
    assert (scripts / ".env").is_file()


def test_rollback_keeps_integration_and_backup_when_restore_is_not_verified(tmp_path: Path) -> None:
    """Rollback must not delete recovery data after a bad restore."""
    component = tmp_path / "config" / "custom_components" / "duepi"
    component.mkdir(parents=True)
    (component / "__init__.py").write_text("DOMAIN = 'duepi'\n")
    backup = tmp_path / "config" / "duepi_migration_backup"
    backup.mkdir(parents=True)
    (backup / "stoveOnOff.py").write_text("print('legacy')\n")
    executor = CorruptingCopySshExecutor(tmp_path)

    migrate.run_rollback(executor, "/config", interactive=False)

    assert (component / "__init__.py").is_file()
    assert (backup / "stoveOnOff.py").read_text() == "print('legacy')\n"
