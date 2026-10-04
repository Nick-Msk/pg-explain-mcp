"""pg_restore-based restore for pg-tune.

Destructive: drops and recreates the target database. Callers must
pass confirm=True; the dry-run path only inspects state.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import psycopg

from pg_tune.backup import _sha256_of
from pg_tune.config import (
    _ensure_db,
    get_backup,
    is_allowed,
    mark_backup_restored,
)


class RestoreError(RuntimeError):
    pass


def _require_writes_allowed() -> None:
    if os.getenv("PG_TUNE_ALLOW_WRITES") != "yes":
        raise RestoreError(
            "PG_TUNE_ALLOW_WRITES is not set to 'yes' — refusing to "
            "restore. This is a write operation."
        )


def _active_connections(database: str) -> list[dict[str, Any]]:
    """Return active connections to `database`, excluding ourselves."""
    with psycopg.connect(dbname="postgres") as conn:
        conn.row_factory = psycopg.rows.dict_row
        with conn.cursor() as cur:
            cur.execute(
                """
                select pid, usename, state,
                       extract(epoch from (now() - query_start))::numeric
                              as query_age_s,
                       left(query, 80) as query
                from pg_stat_activity
                where datname = %s and pid <> pg_backend_pid()
                order by query_start nulls last
                """,
                (database,),
            )
            return cur.fetchall()


def inspect_restore(
    backup_id: int,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Dry-run: gather everything a restore would destroy.

    Does not touch the target database. Returns a dict with backup
    metadata, file presence, sha256 match, and active connections.
    """
    from pg_tune.config import DEFAULT_DB

    db_path = db_path or DEFAULT_DB
    _ensure_db(db_path)

    backup = get_backup(backup_id, db_path)
    if backup is None:
        raise RestoreError(f"no backup with id {backup_id}")

    path = Path(backup["path"])
    file_ok = path.is_file()
    sha_ok = False
    if file_ok:
        sha_ok = _sha256_of(path) == backup["sha256"]

    connections: list[dict[str, Any]] = []
    try:
        connections = _active_connections(backup["database"])
    except psycopg.OperationalError:
        # Can't reach the maintenance DB — still return file info.
        pass

    return {
        "backup": backup,
        "file_present": file_ok,
        "sha256_match": sha_ok,
        "active_connections": connections,
        "allowed": is_allowed("restore", db_path),
        "writes_env": os.getenv("PG_TUNE_ALLOW_WRITES") == "yes",
    }


def do_restore(
    backup_id: int,
    db_path: Path | None = None,
    who: str = "system",
) -> dict[str, Any]:
    """Drop and recreate the target database from a backup.

    Callers must have already run inspect_restore and passed the
    result to a human for confirmation.
    """
    from pg_tune.config import DEFAULT_DB

    db_path = db_path or DEFAULT_DB

    _require_writes_allowed()
    if not is_allowed("restore", db_path):
        raise RestoreError(
            "action 'restore' is not enabled in tune_allows. "
            "Run `pg-tune-config --allow restore` first."
        )

    info = inspect_restore(backup_id, db_path)
    backup = info["backup"]
    database = backup["database"]
    path = Path(backup["path"])

    if not info["file_present"]:
        raise RestoreError(f"backup file missing: {path}")
    if not info["sha256_match"]:
        raise RestoreError(
            f"sha256 mismatch on {path} — file is corrupted or was "
            "modified after the backup was taken"
        )
    if shutil.which("pg_restore") is None:
        raise RestoreError("pg_restore not found on PATH")

    t0 = time.monotonic()

    with psycopg.connect(dbname="postgres", autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'
            )
            cur.execute(f'CREATE DATABASE "{database}"')

    result = subprocess.run(
        ["pg_restore", "-d", database, str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RestoreError(
            f"pg_restore failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}\n"
            f"Database '{database}' is left partially restored.\n"
            f"Backup file: {path}\n"
            f"To recover manually:\n"
            f"  dropdb {database}\n"
            f"  createdb {database} --template=template0\n"
            f"  pg_restore -d {database} {path}"
        )

    elapsed = time.monotonic() - t0
    mark_backup_restored(backup_id, who=who, db_path=db_path)

    return {
        "backup_id": backup_id,
        "database": database,
        "path": str(path),
        "elapsed_s": elapsed,
        "size_bytes": backup["size_bytes"],
        "sha256": backup["sha256"],
    }

