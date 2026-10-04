"""MCP server for pg-tune — write-capable companion to pg-explain-mcp.

Refuses to start unless PG_TUNE_ALLOW_WRITES=yes is set. See
DISCLAIMER.md before running.
"""

import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from pg_tune.backup import BackupError, make_backup
from pg_tune.config import BACKUP_DIR
from pg_tune.restore import RestoreError, do_restore, inspect_restore

mcp = FastMCP("pg-tune")


def _check_startup_safety() -> None:
    if os.getenv("PG_TUNE_ALLOW_WRITES") != "yes":
        print(
            "pg-tune refuses to start: PG_TUNE_ALLOW_WRITES is not "
            "set to 'yes'.\n"
            "This is a write-capable tool. Set PG_TUNE_ALLOW_WRITES=yes "
            "only on dev or test databases. See DISCLAIMER.md.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _human_bytes(n: int | None) -> str:
    if not n:
        return "0 B"
    value = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def _format_backup(ref: dict[str, Any]) -> str:
    return (
        f"Backup #{ref['id']} created.\n"
        f"  database: {ref['database']}\n"
        f"  file:     {ref['path']}\n"
        f"  size:     {_human_bytes(ref['size_bytes'])}\n"
        f"  sha256:   {ref['sha256']}\n"
        f"  pg:       {ref['pg_version']}"
    )


def _format_restore_dry_run(info: dict[str, Any]) -> str:
    b = info["backup"]
    lines = [
        "Dry run. Nothing has been changed.",
        "",
        f"Backup #{b['id']}",
        f"  database: {b['database']}",
        f"  taken:    {b['ts']}",
        f"  size:     {_human_bytes(b['size_bytes'])}",
        f"  sha256:   {b['sha256']}",
        f"  file:     {b['path']}",
        f"  file present: {info['file_present']}",
        f"  sha256 match: {info['sha256_match']}",
        "",
    ]
    conns = info["active_connections"]
    if conns:
        lines.append(f"Active connections to '{b['database']}': {len(conns)}")
        for c in conns[:10]:
            age = c.get("query_age_s")
            age_s = f"{float(age):.1f}s" if age is not None else "—"
            lines.append(
                f"  pid {c['pid']:<7} {c['usename'] or '?':<10} "
                f"{c['state'] or '?':<20} {age_s:>8}  "
                f"{(c.get('query') or '')[:60]}"
            )
        if len(conns) > 10:
            lines.append(f"  … and {len(conns) - 10} more")
    else:
        lines.append(f"No active connections to '{b['database']}'.")

    lines += [
        "",
        f"restore action enabled: {info['allowed']}",
        f"PG_TUNE_ALLOW_WRITES:   {info['writes_env']}",
        "",
        f"⚠️  Restore will DROP and recreate '{b['database']}'. "
        "All connections will be terminated.",
        "    Re-run with confirm=True to proceed.",
    ]
    return "\n".join(lines)


@mcp.tool()
def backup(database: str | None = None) -> str:
    """Snapshot a PostgreSQL database to a zstd-compressed pg_dump file.

    Read-only with respect to the target database — only `pg_dump`
    is run. The file lands in `PG_TUNE_BACKUP_DIR` (default:
    `config/backups/` next to tune.db). The result is recorded in
    `tune_backups`.

    Args:
        database: Database name. Defaults to PGDATABASE.
    """
    try:
        ref = make_backup(database)
        return _format_backup(ref)
    except BackupError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Unexpected error: {e}"


@mcp.tool()
def restore(backup_id: int, confirm: bool = False) -> str:
    """Restore a database from a pg-tune backup.

    ⚠️  DESTRUCTIVE. Drops and recreates the target database. All
    active connections are terminated. Requires:

    - PG_TUNE_ALLOW_WRITES=yes;
    - `tune_allows['restore'] = 1`;
    - confirm=True (this argument).

    Without confirm=True, returns a dry-run summary of what would be
    destroyed — active connections, backup metadata, sha256 check.

    Args:
        backup_id: Row id from `tune_backups`.
        confirm:   Must be True to actually run the restore.
    """
    try:
        if not confirm:
            info = inspect_restore(backup_id)
            return _format_restore_dry_run(info)

        result = do_restore(backup_id)
        return (
            f"Restore complete.\n"
            f"  backup_id: {result['backup_id']}\n"
            f"  database:  {result['database']}\n"
            f"  file:      {result['path']}\n"
            f"  size:      {_human_bytes(result['size_bytes'])}\n"
            f"  sha256:    {result['sha256']}\n"
            f"  duration:  {result['elapsed_s']:.1f} s"
        )
    except RestoreError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Unexpected error: {e}"


def main() -> None:
    _check_startup_safety()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    mcp.run()


if __name__ == "__main__":
    main()

