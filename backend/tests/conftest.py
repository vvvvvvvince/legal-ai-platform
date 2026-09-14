"""Cross-platform pytest configuration for the backend suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def pytest_configure(config) -> None:
    """Avoid Windows' shared temp directory and stale test-run locks.

    Some managed Windows environments deny access to the shared
    ``%LOCALAPPDATA%\\Temp\\pytest-*`` directory.  A fixed in-repository
    ``--basetemp`` also fails after an interrupted run because an indexer or a
    running process can retain a handle.  A project-local, process-unique
    directory is both writable and isolated from earlier runs.
    """

    runtime_root = Path(config.rootpath) / ".pytest-runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    if not config.option.basetemp:
        config.option.basetemp = runtime_root / f"run-{os.getpid()}"


def pytest_sessionstart(session) -> None:
    """Undo pytest's Windows 0700 ACL before tmp_path fixtures enumerate it."""
    base_temp = session.config._tmp_path_factory.getbasetemp()
    os.chmod(base_temp, 0o777)


@pytest.fixture(autouse=True)
def isolate_auth_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never let endpoint tests inherit the developer's local login database."""
    monkeypatch.setenv("AUTH_DB", str(tmp_path / "auth.sqlite3"))
