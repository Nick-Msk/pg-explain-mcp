"""Shared fixtures for pg-tune tests.

Every fixture redirects ``DEFAULT_DB`` and ``BACKUP_DIR`` to a
sandbox under ``tests/``, so the developer's real
``config/tune.db`` and ``config/backups/`` are never touched.
"""

from pathlib import Path

import pytest
from pg_tune.config import init_db

TESTS_DIR = Path(__file__).resolve().parent
TEST_DB = TESTS_DIR / "test_tune.db"
TEST_BACKUP_DIR = TESTS_DIR / "test_backups"


@pytest.fixture
def tune_db(monkeypatch):
    """Fresh test_tune.db for each test.

    Redirects both ``config.DEFAULT_DB`` (used by config functions)
    and ``backup.BACKUP_DIR`` (used by make_backup), so no path
    escapes into the developer's real config.
    """
    monkeypatch.setattr("pg_tune.config.DEFAULT_DB", TEST_DB)
    monkeypatch.setattr("pg_tune.backup.BACKUP_DIR", TEST_BACKUP_DIR)

    init_db(TEST_DB)
    yield TEST_DB

    # Cleanup after each test — remove the db file and backup dir.
    if TEST_DB.exists():
        TEST_DB.unlink()
    if TEST_BACKUP_DIR.exists():
        for p in TEST_BACKUP_DIR.iterdir():
            p.unlink()
        TEST_BACKUP_DIR.rmdir()


@pytest.fixture
def cli_db(monkeypatch):
    """Like ``tune_db``, for CLI tests.

    Does not call ``init_db`` — the CLI itself does that via
    ``--init``, and tests want to exercise that path. Just
    redirects paths and wipes stale state.
    """
    monkeypatch.setattr("pg_tune.config.DEFAULT_DB", TEST_DB)
    monkeypatch.setattr("pg_tune.backup.BACKUP_DIR", TEST_BACKUP_DIR)

    if TEST_DB.exists():
        TEST_DB.unlink()
    yield TEST_DB

    if TEST_DB.exists():
        TEST_DB.unlink()
    if TEST_BACKUP_DIR.exists():
        for p in TEST_BACKUP_DIR.iterdir():
            p.unlink()
        TEST_BACKUP_DIR.rmdir()

