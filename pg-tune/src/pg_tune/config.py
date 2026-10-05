"""SQLite-backed configuration for pg-tune."""

import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
DEFAULT_DB = _CONFIG_DIR / "tune.db"

TARGET_DB_TYPE = os.getenv("TARGET_DB_TYPE", "postgres")
BACKUP_DIR = Path(
    os.getenv(
        "PG_TUNE_BACKUP_DIR",
        str(DEFAULT_DB.parent / "backups"),
    )
)


def _connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_db(db_path: Path | str = DEFAULT_DB) -> None:
    db_path = Path(db_path)
    if not db_path.exists():
        init_db(db_path)
        return
    # Sanity check: required tables present.
    try:
        with sqlite3.connect(db_path) as conn:
            for table in ("tune_backups", "tune_allows", "tune_audit",
                          "tune_vec_params", "tune_audit_vector"):
                row = conn.execute(
                    "select 1 from sqlite_master "
                    "where type='table' and name=?",
                    (table,),
                ).fetchone()
                if row is None:
                    raise sqlite3.OperationalError(f"missing table: {table}")
    except sqlite3.OperationalError:
        init_db(db_path)


def init_db(db_path: Path | str = DEFAULT_DB) -> None:
    """Drop and rebuild the tune config tables from schema + seed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema = (_CONFIG_DIR / "tune_schema.sql").read_text()
    seed = (_CONFIG_DIR / "tune_seed.sql").read_text()

    drop = (
        "drop table if exists tune_audit_vector",
        "drop table if exists tune_vec_params",
        "drop table if exists tune_audit",
        "drop table if exists tune_allows",
        "drop table if exists tune_backups",
    )

    with sqlite3.connect(db_path) as conn:
        for stmt in drop:
            conn.execute(stmt)
        conn.executescript(schema)
        conn.executescript(seed)

    print(f"Initialized {db_path}", file=sys.stderr)


def is_allowed(
    action: str,
    db_path: Path | str = DEFAULT_DB,
) -> bool:
    """True if the given action is enabled in tune_allows."""
    _ensure_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "select enabled from tune_allows where action = ?",
            (action,),
        ).fetchone()
    return bool(row and row["enabled"])

def list_allows(db_path: Path | str = DEFAULT_DB) -> list[dict[str, Any]]:
    """Return every row of tune_allows, ordered by action name."""
    _ensure_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "select action, enabled, default_enabled, description "
            "from tune_allows order by action"
        ).fetchall()
    return [dict(r) for r in rows]

def list_vec_params(
    db_path: Path | str = DEFAULT_DB,
) -> list[dict[str, Any]]:
    """Return every row of tune_vec_params, ordered by name."""
    _ensure_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "select name, desc, measure "
            "from tune_vec_params order by name"
        ).fetchall()
    return [dict(r) for r in rows]

def set_allow(
    action: str,
    enabled: bool | int,
    db_path: Path | str = DEFAULT_DB,
) -> dict[str, Any]:
    """Set the enabled flag for one action.

    ``enabled`` accepts bool or int; anything truthy becomes 1.
    Returns ``{"action", "old", "new", "description"}``.
    Raises ``KeyError`` if the action is not registered.
    """
    _ensure_db(db_path)
    new = 1 if enabled else 0

    with _connect(db_path) as conn:
        row = conn.execute(
            "select enabled, description from tune_allows where action = ?",
            (action,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown action: {action!r}")

        old = row["enabled"]
        conn.execute(
            "update tune_allows set enabled = ? where action = ?",
            (new, action),
        )
        conn.commit()

    return {
        "action": action,
        "old": old,
        "new": new,
        "description": row["description"],
    }

def reset_allow(
    action: str = "",
    db_path: Path | str = DEFAULT_DB,
) -> list[dict[str, Any]]:
    """Reset one action (or all) to default_enabled.

    Returns the list of changes. Empty list when already at defaults.
    Raises ``KeyError`` if a specific ``action`` is not registered.
    """
    _ensure_db(db_path)

    with _connect(db_path) as conn:
        if action:
            rows = conn.execute(
                "select action, enabled, default_enabled, description "
                "from tune_allows where action = ?",
                (action,),
            ).fetchall()
            if not rows:
                raise KeyError(f"unknown action: {action!r}")
        else:
            rows = conn.execute(
                "select action, enabled, default_enabled, description "
                "from tune_allows order by action"
            ).fetchall()

        changes = []
        for r in rows:
            if r["enabled"] == r["default_enabled"]:
                continue
            conn.execute(
                "update tune_allows set enabled = ? where action = ?",
                (r["default_enabled"], r["action"]),
            )
            changes.append({
                "action": r["action"],
                "old": r["enabled"],
                "new": r["default_enabled"],
                "description": r["description"],
            })
        conn.commit()

    return changes

def record_backup(
    database: str,
    path: Path,
    size_bytes: int,
    sha256: str,
    pg_version: str,
    pg_explain_version: str,
    db_path: Path | str = DEFAULT_DB,
) -> int:
    """Insert a tune_backups row, return its id."""
    _ensure_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "insert into tune_backups "
            "(database, path, size_bytes, sha256, pg_version, "
            " pg_explain_version) "
            "values (?, ?, ?, ?, ?, ?)",
            (database, str(path), size_bytes, sha256,
             pg_version, pg_explain_version),
        )
        conn.commit()
        return cur.lastrowid


def get_backup(
    backup_id: int,
    db_path: Path | str = DEFAULT_DB,
) -> dict[str, Any] | None:
    _ensure_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "select * from tune_backups where id = ?",
            (backup_id,),
        ).fetchone()
    return dict(row) if row else None


def mark_backup_restored(
    backup_id: int,
    who: str = "system",
    db_path: Path | str = DEFAULT_DB,
) -> None:
    _ensure_db(db_path)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with _connect(db_path) as conn:
        conn.execute(
            "update tune_backups "
            "set restored_ts = ?, restored_by = ? where id = ?",
            (ts, who, backup_id),
        )
        conn.commit()


def new_session_id() -> str:
    """Short opaque session id for the tune_audit log."""
    return secrets.token_hex(6)

