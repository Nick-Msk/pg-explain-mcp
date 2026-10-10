"""pg_dump | zstd snapshot for pg-tune."""

import hashlib
import os
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from pg_tune.config import BACKUP_DIR, _ensure_db


class BackupError(RuntimeError):
    pass

def _pg_dump_once(
    db: str,
    out_path: Path,
    compress_arg: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    """Run pg_dump once with the given compression flag.

    Caller inspects returncode. ``pg_dump`` validates the
    compression method before opening a connection, so a
    build-without-zstd failure is immediate and cheap.
    """
    cmd = ["pg_dump", "-Fc", compress_arg, "-f", str(out_path), db]
    try:
        return subprocess.run(
            cmd, env=env, capture_output=True, text=True,
        )
    except FileNotFoundError as e:
        raise BackupError(f"pg_dump not found on PATH: {e}") from e
    except OSError as e:
        raise BackupError(f"cannot run pg_dump: {e}") from e

def _is_zstd_unsupported(stderr: str) -> bool:
    """True if pg_dump specifically rejected zstd for this build.

    Message (PG 16 source): "compression with zstd is not
    supported in this build". Anything else — connection failure,
    permissions, disk full — must not be silently retried as gzip.
    """
    low = stderr.lower()
    if "zstd" not in low:
        return False
    return "not supported" in low or "unsupported" in low

def _pg_server_version(database: str) -> str:
    """Return `SHOW server_version` for the target database.

    Uses psycopg with an explicit dbname so it does not depend on
    PGDATABASE or the OS user name.
    """
    import psycopg
    try:
        with psycopg.connect(dbname=database, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("show server_version")
                row = cur.fetchone()
    except psycopg.Error as e:
        raise BackupError(f"cannot query server version: {e}") from e
    return row[0] if row else "unknown"

def _pg_explain_version() -> str:
    try:
        return version("pg-explain-mcp")
    except PackageNotFoundError:
        return "unknown"


def _sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _target_database(database: str | None) -> str:
    db = database or os.getenv("PGDATABASE")
    if not db:
        raise BackupError(
            "no database specified and PGDATABASE is not set"
        )
    return db

def make_backup(
    database: str | None = None,
    backup_dir: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Run pg_dump -Fc and register the result.

    Compression is chosen by trying zstd first and falling back to
    gzip if the pg_dump binary was built without zstd. The help
    text does not advertise available methods, so the only
    reliable check is an actual invocation.
    """
    from pg_tune.config import DEFAULT_DB, record_backup

    db_path = db_path or DEFAULT_DB
    backup_dir = backup_dir or BACKUP_DIR
    _ensure_db(db_path)

    if shutil.which("pg_dump") is None:
        raise BackupError("pg_dump not found on PATH")

    db = _target_database(database)
    env = os.environ.copy()
    env["PGDATABASE"] = db

    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(backup_dir, 0o700)
    except OSError:
        pass

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(2)
    base = f"{db}_{ts}_{suffix}"

    pg_version, _ = _pg_server_version(db)

    # --- try zstd ---
    out_path = backup_dir / f"{base}.dump.zstd"
    result = _pg_dump_once(db, out_path, "--compress=zstd:19", env)

    if result.returncode != 0:
        if not _is_zstd_unsupported(result.stderr):
            if out_path.exists():
                out_path.unlink()
            raise BackupError(
                f"pg_dump failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        # zstd is not in this build — clean up and retry with gzip.
        if out_path.exists():
            out_path.unlink()
        out_path = backup_dir / f"{base}.dump.gz"
        result = _pg_dump_once(db, out_path, "--compress=gzip:9", env)

    if result.returncode != 0:
        if out_path.exists():
            out_path.unlink()
        raise BackupError(
            f"pg_dump failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    size = out_path.stat().st_size
    digest = _sha256_of(out_path)

    backup_id = record_backup(
        database=db,
        path=out_path,
        size_bytes=size,
        sha256=digest,
        pg_version=pg_version,
        pg_explain_version=_pg_explain_version(),
        db_path=db_path,
    )

    return {
        "id": backup_id,
        "database": db,
        "path": str(out_path),
        "size_bytes": size,
        "sha256": digest,
        "pg_version": pg_version,
    }

