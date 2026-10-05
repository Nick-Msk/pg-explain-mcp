"""Integration tests: real pg_dump + pg_restore against a live PG.

Opt-in — the rest of the test suite is offline, this file is not.

    PG_TEST_INTEGRATION=1 pytest tests/test_integration_backup_restore.py -v

The test creates a throwaway database named
``pg_tune_bkp_test_<random>``, does a full cycle
(seed → backup → mutate → restore → verify) against it, then drops
it. Nothing outside that database is touched.

Requires a PostgreSQL where the current user can CREATE/DROP
DATABASE. Uses the PG* environment variables for connection info,
the same ones pg_dump/pg_restore consume.
"""

import os
import secrets
from pathlib import Path

import psycopg
import pytest
from pg_tune.backup import make_backup
from pg_tune.config import set_allow
from pg_tune.restore import RestoreError, do_restore, inspect_restore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PG_TEST_INTEGRATION"),
        reason="set PG_TEST_INTEGRATION=1 to run integration tests",
    ),
]


# ---------------------------------------------------------------------------
# Helpers — each opens a short-lived connection and closes it
# ---------------------------------------------------------------------------


def _create_database(name: str) -> None:
    with psycopg.connect(dbname="postgres", autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')


def _drop_database(name: str) -> None:
    with psycopg.connect(dbname="postgres", autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _seed(dbname: str) -> None:
    """Three users, four orders — enough to tell apart from an empty db."""
    with psycopg.connect(dbname=dbname, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "create table users ("
                "  id serial primary key,"
                "  email text not null)"
            )
            cur.execute(
                "create table orders ("
                "  id serial primary key,"
                "  user_id int references users(id),"
                "  amt numeric)"
            )
            cur.executemany(
                "insert into users (email) values (%s)",
                [("a@x.com",), ("b@x.com",), ("c@x.com",)],
            )
            cur.executemany(
                "insert into orders (user_id, amt) values (%s, %s)",
                [(1, 10), (2, 20), (2, 30), (3, 40)],
            )


def _mutate(dbname: str) -> None:
    """Something destructive enough that restore has to undo it."""
    with psycopg.connect(dbname=dbname, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("drop table orders")
            cur.execute("delete from users where id > 1")
            cur.execute("create table junk (x int)")


def _counts(dbname: str) -> dict[str, int]:
    """Return row counts; -1 means the table does not exist."""
    with psycopg.connect(dbname=dbname) as conn:
        with conn.cursor() as cur:
            result: dict[str, int] = {}
            for table in ("users", "orders", "junk"):
                cur.execute(
                    "select exists ("
                    "  select 1 from information_schema.tables"
                    "  where table_schema = 'public'"
                    "    and table_name = %s)",
                    (table,),
                )
                if not cur.fetchone()[0]:
                    result[table] = -1
                    continue
                cur.execute(f"select count(*) from {table}")
                result[table] = cur.fetchone()[0]
            return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_db_name():
    """A unique name; drops the db at teardown if it still exists."""
    name = f"pg_tune_bkp_test_{secrets.token_hex(3)}"
    yield name
    _drop_database(name)


@pytest.fixture
def allow_writes(monkeypatch):
    """PG_TUNE_ALLOW_WRITES=yes for the duration of the test."""
    monkeypatch.setenv("PG_TUNE_ALLOW_WRITES", "yes")


@pytest.fixture
def allow_restore(tune_db):
    """Enable the `restore` action in tune_allows."""
    set_allow("restore", True, tune_db)
    return tune_db


# ---------------------------------------------------------------------------
# The full cycle
# ---------------------------------------------------------------------------


class TestFullCycle:
    def test_backup_mutate_restore_verify(
        self,
        tune_db,
        test_db_name,
        allow_writes,
        allow_restore,
    ):
        # -- 0. seed ------------------------------------------------
        _create_database(test_db_name)
        _seed(test_db_name)

        assert _counts(test_db_name) == {
            "users": 3, "orders": 4, "junk": -1,
        }

        # -- 1. backup ----------------------------------------------
        ref = make_backup(database=test_db_name, db_path=tune_db)
        assert ref["database"] == test_db_name
        assert Path(ref["path"]).exists()
        assert ref["size_bytes"] > 0

        # -- 2. mutate ----------------------------------------------
        _mutate(test_db_name)
        assert _counts(test_db_name) == {
            "users": 1, "orders": -1, "junk": 0,
        }

        # -- 3. dry run ---------------------------------------------
        info = inspect_restore(ref["id"], tune_db)
        assert info["file_present"] is True
        assert info["sha256_match"] is True
        assert info["allowed"] is True
        assert info["writes_env"] is True

        # -- 4. restore ---------------------------------------------
        result = do_restore(ref["id"], tune_db)
        assert result["backup_id"] == ref["id"]
        assert result["database"] == test_db_name

        # -- 5. verify ----------------------------------------------
        assert _counts(test_db_name) == {
            "users": 3,
            "orders": 4,
            "junk": -1,   # junk was never in the backup
        }

    def test_restore_is_idempotent(
        self,
        tune_db,
        test_db_name,
        allow_writes,
        allow_restore,
    ):
        _create_database(test_db_name)
        _seed(test_db_name)
        ref = make_backup(database=test_db_name, db_path=tune_db)

        do_restore(ref["id"], tune_db)
        first = _counts(test_db_name)

        do_restore(ref["id"], tune_db)
        second = _counts(test_db_name)

        assert first == second
        assert first == {"users": 3, "orders": 4, "junk": -1}


# ---------------------------------------------------------------------------
# Gate checks — the write path refuses to run when it should
# ---------------------------------------------------------------------------


class TestGates:
    def test_refused_without_writes_env(
        self,
        tune_db,
        test_db_name,
        allow_restore,
        monkeypatch,
    ):
        monkeypatch.delenv("PG_TUNE_ALLOW_WRITES", raising=False)

        _create_database(test_db_name)
        _seed(test_db_name)
        ref = make_backup(database=test_db_name, db_path=tune_db)

        with pytest.raises(RestoreError, match="PG_TUNE_ALLOW_WRITES"):
            do_restore(ref["id"], tune_db)

    def test_refused_when_allow_disabled(
        self,
        tune_db,
        test_db_name,
        allow_writes,
    ):
        # NOT calling allow_restore — tune_allows['restore'] stays 0.
        _create_database(test_db_name)
        _seed(test_db_name)
        ref = make_backup(database=test_db_name, db_path=tune_db)

        with pytest.raises(RestoreError, match="not enabled"):
            do_restore(ref["id"], tune_db)

    def test_refused_when_backup_file_missing(
        self,
        tune_db,
        test_db_name,
        allow_writes,
        allow_restore,
    ):
        _create_database(test_db_name)
        _seed(test_db_name)
        ref = make_backup(database=test_db_name, db_path=tune_db)

        # Simulate an external delete of the dump file.
        Path(ref["path"]).unlink()

        with pytest.raises(RestoreError, match="missing"):
            do_restore(ref["id"], tune_db)

    def test_refused_when_sha256_mismatch(
        self,
        tune_db,
        test_db_name,
        allow_writes,
        allow_restore,
    ):
        _create_database(test_db_name)
        _seed(test_db_name)
        ref = make_backup(database=test_db_name, db_path=tune_db)

        # Corrupt the file — append a byte.
        with Path(ref["path"]).open("ab") as f:
            f.write(b"x")

        with pytest.raises(RestoreError, match="sha256"):
            do_restore(ref["id"], tune_db)

