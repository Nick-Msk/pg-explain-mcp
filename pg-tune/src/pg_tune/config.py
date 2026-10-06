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
        "drop table if exists tune_settings",
        "drop table if exists tune_backups",
    )

    with sqlite3.connect(db_path) as conn:
        for stmt in drop:
            conn.execute(stmt)
        conn.executescript(schema)
        conn.executescript(seed)

    print(f"Initialized {db_path}", file=sys.stderr)


def list_settings(
    category: str = "",
    db_path: Path | str = DEFAULT_DB,
) -> list[dict[str, Any]]:
    """Return rows of tune_settings, optionally filtered by category."""
    _ensure_db(db_path)
    sql = (
        "select category, name, value, default_value, desc "
        "from tune_settings"
    )
    params: list = []
    if category:
        sql += " where category = ?"
        params.append(category)
    sql += " order by category, name"
    with _connect(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_setting(
    category: str,
    name: str,
    db_path: Path | str = DEFAULT_DB,
) -> str | None:
    _ensure_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "select value from tune_settings "
            "where category = ? and name = ?",
            (category, name),
        ).fetchone()
    return row["value"] if row else None


def set_setting(
    category: str,
    name: str,
    value: str,
    db_path: Path | str = DEFAULT_DB,
) -> dict[str, Any]:
    _ensure_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "select value, default_value, desc from tune_settings "
            "where category = ? and name = ?",
            (category, name),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown setting: {category}.{name}")
        conn.execute(
            "update tune_settings set value = ? "
            "where category = ? and name = ?",
            (value, category, name),
        )
        conn.commit()
    return {
        "category": category,
        "name": name,
        "old": row["value"],
        "new": value,
        "default": row["default_value"],
        "desc": row["desc"],
    }


def reset_setting(
    category: str = "",
    name: str = "",
    db_path: Path | str = DEFAULT_DB,
) -> list[dict[str, Any]]:
    """Reset by category, by (category, name), or everything.

    Empty category and name resets every row. Raising for a
    specific (category, name) that does not exist is intentional —
    a typo should fail, not silently no-op.
    """
    _ensure_db(db_path)
    where: list[str] = []
    params: list = []
    if category:
        where.append("category = ?")
        params.append(category)
    if name:
        where.append("name = ?")
        params.append(name)

    sql = (
        "select category, name, value, default_value, desc "
        "from tune_settings"
    )
    if where:
        sql += " where " + " and ".join(where)

    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        if (category or name) and not rows:
            raise KeyError(
                f"unknown setting: {category}.{name}".rstrip(".")
            )
        changes = []
        for r in rows:
            if r["value"] == r["default_value"]:
                continue
            conn.execute(
                "update tune_settings set value = ? "
                "where category = ? and name = ?",
                (r["default_value"], r["category"], r["name"]),
            )
            changes.append({
                "category": r["category"],
                "name": r["name"],
                "old": r["value"],
                "new": r["default_value"],
            })
        conn.commit()
    return changes


def is_allowed(
    action: str,
    db_path: Path | str = DEFAULT_DB,
) -> bool:
    """True if ALLOWS.<action> is '1'."""
    return get_setting("ALLOWS", action, db_path) == "1"

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

