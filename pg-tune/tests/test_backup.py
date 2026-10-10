"""Tests for pg_tune.backup.make_backup.

pg_dump never runs for real — subprocess.run is replaced with a
stub that writes a small payload to the -f path. The stub lets
each test decide what the binary "replies": zstd accepted,
zstd rejected for this build, or a non-compression error.
"""

import hashlib
import subprocess
from pathlib import Path

import pytest
from pg_tune.backup import BackupError, make_backup

TEST_BACKUP_DIR = Path(__file__).resolve().parent / "test_backups"

# ---------------------------------------------------------------------------
# Fixtures — one per pg_dump "personality"
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_pg_dump_zstd_ok(monkeypatch):
    """Modern pg_dump: zstd accepted, one call, done."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        f_idx = cmd.index("-f")
        Path(cmd[f_idx + 1]).write_bytes(b"fake dump payload")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("pg_tune.backup.subprocess.run", fake_run)
    return calls


@pytest.fixture
def fake_pg_dump_zstd_missing(monkeypatch):
    """Pre-16 pg_dump: rejects zstd, accepts gzip on retry."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        if "--compress=zstd:19" in cmd:
            return subprocess.CompletedProcess(
                cmd, 1, "",
                "pg_dump: error: compression with zstd is not "
                "supported in this build\n",
            )
        f_idx = cmd.index("-f")
        Path(cmd[f_idx + 1]).write_bytes(b"fake dump payload")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("pg_tune.backup.subprocess.run", fake_run)
    return calls


@pytest.fixture
def fake_pg_dump_conn_error(monkeypatch):
    """pg_dump fails for a reason unrelated to compression."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 1, "",
            "pg_dump: error: connection to server failed\n",
        )

    monkeypatch.setattr("pg_tune.backup.subprocess.run", fake_run)
    return calls


@pytest.fixture
def fake_server_version(monkeypatch):
    """Server reports 18.6. Used only for tune_backups metadata —
    never for the compression decision."""
    monkeypatch.setattr(
        "pg_tune.backup._pg_server_version",
        lambda db: ("18.6", 180006),
    )


@pytest.fixture
def fake_which(monkeypatch):
    """Pretend pg_dump is on PATH."""
    monkeypatch.setattr(
        "pg_tune.backup.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )


# ---------------------------------------------------------------------------
# Happy path — modern pg_dump
# ---------------------------------------------------------------------------


class TestMakeBackupSuccess:
    def test_creates_file_with_zstd_extension(
        self, tune_db, fake_pg_dump_zstd_ok,
        fake_server_version, fake_which,
    ):
        ref = make_backup(database="testdb", db_path=tune_db)
        path = Path(ref["path"])
        assert path.exists()
        assert path.suffix == ".zstd"
        assert path.name.startswith("testdb_")
        assert path.parent == TEST_BACKUP_DIR

    def test_returns_expected_shape(
        self, tune_db, fake_pg_dump_zstd_ok,
        fake_server_version, fake_which,
    ):
        ref = make_backup(database="testdb", db_path=tune_db)
        assert set(ref.keys()) == {
            "id", "database", "path", "size_bytes",
            "sha256", "pg_version",
        }
        assert ref["database"] == "testdb"
        assert ref["pg_version"] == "18.6"

    def test_sha256_matches_file_contents(
        self, tune_db, fake_pg_dump_zstd_ok,
        fake_server_version, fake_which,
    ):
        ref = make_backup(database="testdb", db_path=tune_db)
        expected = hashlib.sha256(b"fake dump payload").hexdigest()
        assert ref["sha256"] == expected

    def test_size_matches_file(
        self, tune_db, fake_pg_dump_zstd_ok,
        fake_server_version, fake_which,
    ):
        ref = make_backup(database="testdb", db_path=tune_db)
        assert ref["size_bytes"] == len(b"fake dump payload")

    def test_persisted_in_tune_backups(
        self, tune_db, fake_pg_dump_zstd_ok,
        fake_server_version, fake_which,
    ):
        import sqlite3
        ref = make_backup(database="testdb", db_path=tune_db)
        with sqlite3.connect(tune_db) as conn:
            row = conn.execute(
                "select database, path, size_bytes, sha256, pg_version "
                "from tune_backups where id = ?",
                (ref["id"],),
            ).fetchone()
        assert row is not None
        assert row[0] == "testdb"
        assert row[1] == ref["path"]
        assert row[2] == ref["size_bytes"]
        assert row[3] == ref["sha256"]
        assert row[4] == "18.6"

    def test_pg_dump_command_shape(
        self, tune_db, fake_pg_dump_zstd_ok,
        fake_server_version, fake_which,
    ):
        make_backup(database="testdb", db_path=tune_db)
        cmd = fake_pg_dump_zstd_ok[0]
        assert cmd[0] == "pg_dump"
        assert "-Fc" in cmd
        assert "--compress=zstd:19" in cmd
        assert cmd[-1] == "testdb"
        f_idx = cmd.index("-f")
        assert cmd[f_idx + 1].endswith(".zstd")

    def test_second_backup_gets_new_id(
        self, tune_db, fake_pg_dump_zstd_ok,
        fake_server_version, fake_which,
    ):
        a = make_backup(database="testdb", db_path=tune_db)
        b = make_backup(database="testdb", db_path=tune_db)
        assert b["id"] > a["id"]
        assert a["path"] != b["path"]


# ---------------------------------------------------------------------------
# Compression selection
# ---------------------------------------------------------------------------


class TestCompressionSelection:
    def test_zstd_used_on_capable_client(
        self, tune_db, fake_pg_dump_zstd_ok,
        fake_server_version, fake_which,
    ):
        ref = make_backup(database="testdb", db_path=tune_db)
        assert Path(ref["path"]).suffix == ".zstd"
        assert len(fake_pg_dump_zstd_ok) == 1

    def test_gzip_fallback_on_incapable_client(
        self, tune_db, fake_pg_dump_zstd_missing,
        fake_server_version, fake_which,
    ):
        ref = make_backup(database="testdb", db_path=tune_db)
        assert Path(ref["path"]).suffix == ".gz"
        assert len(fake_pg_dump_zstd_missing) == 2
        assert "--compress=zstd:19" in fake_pg_dump_zstd_missing[0]
        assert "--compress=gzip:9" in fake_pg_dump_zstd_missing[1]

    def test_fallback_records_gzip_file_in_db(
        self, tune_db, fake_pg_dump_zstd_missing,
        fake_server_version, fake_which,
    ):
        import sqlite3
        ref = make_backup(database="testdb", db_path=tune_db)
        with sqlite3.connect(tune_db) as conn:
            row = conn.execute(
                "select path from tune_backups where id = ?",
                (ref["id"],),
            ).fetchone()
        assert row[0].endswith(".gz")

    def test_no_partial_zstd_left_after_fallback(
        self, tune_db, fake_pg_dump_zstd_missing,
        fake_server_version, fake_which,
    ):
        make_backup(database="testdb", db_path=tune_db)
        assert list(TEST_BACKUP_DIR.glob("*.zstd")) == []
        assert list(TEST_BACKUP_DIR.glob("*.gz")) != []

    def test_non_compression_error_is_not_retried(
        self, tune_db, fake_pg_dump_conn_error,
        fake_server_version, fake_which,
    ):
        with pytest.raises(BackupError, match="connection"):
            make_backup(database="testdb", db_path=tune_db)
        # Exactly one attempt — no gzip retry for unrelated errors.
        assert len(fake_pg_dump_conn_error) == 1

    def test_non_compression_error_leaves_no_file(
        self, tune_db, fake_pg_dump_conn_error,
        fake_server_version, fake_which,
    ):
        with pytest.raises(BackupError):
            make_backup(database="testdb", db_path=tune_db)
        assert list(TEST_BACKUP_DIR.glob("*.zstd")) == []
        assert list(TEST_BACKUP_DIR.glob("*.gz")) == []


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestMakeBackupErrors:
    def test_no_database_and_no_env(
        self, tune_db, fake_which, fake_server_version, monkeypatch,
    ):
        monkeypatch.delenv("PGDATABASE", raising=False)
        with pytest.raises(BackupError, match="no database"):
            make_backup(database=None, db_path=tune_db)

    def test_pg_dump_missing(
        self, tune_db, monkeypatch,
    ):
        monkeypatch.setattr(
            "pg_tune.backup.shutil.which", lambda name: None
        )
        with pytest.raises(BackupError, match="pg_dump not found"):
            make_backup(database="testdb", db_path=tune_db)

    def test_generic_pg_dump_failure_records_nothing(
        self, tune_db, fake_server_version, fake_which, monkeypatch,
    ):
        import sqlite3

        def failing_run(cmd, *args, **kwargs):
            f_idx = cmd.index("-f")
            Path(cmd[f_idx + 1]).write_bytes(b"partial")
            return subprocess.CompletedProcess(
                cmd, 1, "",
                "pg_dump: error: permission denied for database",
            )

        monkeypatch.setattr("pg_tune.backup.subprocess.run", failing_run)

        with pytest.raises(BackupError, match="pg_dump failed"):
            make_backup(database="testdb", db_path=tune_db)

        with sqlite3.connect(tune_db) as conn:
            n = conn.execute(
                "select count(*) from tune_backups"
            ).fetchone()[0]
        assert n == 0

    def test_generic_pg_dump_failure_cleans_up_file(
        self, tune_db, fake_server_version, fake_which, monkeypatch,
    ):
        def failing_run(cmd, *args, **kwargs):
            f_idx = cmd.index("-f")
            Path(cmd[f_idx + 1]).write_bytes(b"partial")
            return subprocess.CompletedProcess(
                cmd, 1, "", "pg_dump: error: out of disk space",
            )

        monkeypatch.setattr("pg_tune.backup.subprocess.run", failing_run)

        with pytest.raises(BackupError):
            make_backup(database="testdb", db_path=tune_db)

        assert list(TEST_BACKUP_DIR.glob("*")) == []

    def test_pg_dump_not_installed_at_runtime(
        self, tune_db, fake_server_version, fake_which, monkeypatch,
    ):
        # which() says it is there, but the actual call fails with
        # FileNotFoundError — race, or PATH changed under us.
        def missing_run(cmd, *args, **kwargs):
            raise FileNotFoundError("pg_dump")

        monkeypatch.setattr("pg_tune.backup.subprocess.run", missing_run)

        with pytest.raises(BackupError):
            make_backup(database="testdb", db_path=tune_db)

    def test_missing_server_version_recorded_as_error(
        self, tune_db, fake_which, fake_pg_dump_zstd_ok, monkeypatch,
    ):
        def boom(db):
            raise BackupError("cannot query server version: nope")

        monkeypatch.setattr("pg_tune.backup._pg_server_version", boom)

        with pytest.raises(BackupError, match="server version"):
            make_backup(database="testdb", db_path=tune_db)

