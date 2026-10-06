"""MCP server for pg-tune — write-capable companion to pg-explain-mcp.

Refuses to start unless PG_TUNE_ALLOW_WRITES=yes is set. See
DISCLAIMER.md before running.
"""

import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from pg_tune.backup import BackupError, make_backup
from pg_tune.config import BACKUP_DIR, set_setting
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

    conns = info["active_connections"]
    if conns:
        lines += [
            "",
            f"⚠️  {len(conns)} active connection(s) to '{b['database']}'.",
            "",
            "    If any of them belongs to another MCP server",
            "    (postgres-test1, universal-db-mcp, etc.), you MUST",
            "    call that server's disconnect tool BEFORE restore:",
            "",
            "        postgres-test1.disconnect_database()",
            "",
            "    Otherwise that server will hang after DROP DATABASE",
            "    and will need a manual process restart that you",
            "    cannot perform yourself.",
            "",
            f"    Restore will DROP and recreate '{b['database']}'.",
            "    After disconnecting, re-run with confirm=True.",
        ]
    else:
        lines += [
            "",
            f"⚠️  Restore will DROP and recreate '{b['database']}'.",
            "    No active connections — safe to proceed.",
            "    Re-run with confirm=True.",
        ]

    lines += [
        "",
        f"restore action enabled: {info['allowed']}",
        f"PG_TUNE_ALLOW_WRITES:   {info['writes_env']}",
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

    DESTRUCTIVE. Drops and recreates the target database via
    DROP DATABASE ... WITH (FORCE). Every connection to the target
    is terminated at the PostgreSQL level.

    Other MCP servers or clients connected to the target database
    will be forcibly disconnected. A client that does not handle
    the disconnect cleanly (universal-db-mcp / postgres-test1 is
    one such client) will hang and require a process restart,
    which you cannot perform yourself.

    REQUIRED WORKFLOW:

      1. Call with confirm=False (dry-run). Read the list of
         active connections carefully.
      2. If any active connection belongs to another MCP server,
         disconnect it first using that server's disconnect tool.
      3. Only then call with confirm=True.

    Do not skip step 1. Do not skip step 2 if connections exist.

    Requires:

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

@mcp.tool()
def list_backups(database: str = "", limit: int = 20) -> str:
    """List registered pg-tune backups, most recent first.

    Reads from the tune SQLite config (tune_backups). Does not
    touch the target PostgreSQL database.

    Args:
        database: Filter by database name. Empty string returns
            backups for all databases.
        limit:    Return at most this many rows. Default 20.
    """
    from pg_tune.config import DEFAULT_DB, _connect, _ensure_db

    _ensure_db(DEFAULT_DB)

    sql = (
        "select id, ts, database, size_bytes, pg_version, "
        "       restored_ts, path "
        "from tune_backups"
    )
    params: list = []
    if database:
        sql += " where database = ?"
        params.append(database)
    sql += " order by ts desc limit ?"
    params.append(limit)

    with _connect(DEFAULT_DB) as conn:
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        return f"No backups for {database!r}." if database else "No backups."

    lines = [
        f"{'id':>4}  {'ts':<24}  {'database':<20}  "
        f"{'size':>10}  {'pg':<8}  state"
    ]
    for r in rows:
        state = "restored" if r["restored_ts"] else "available"
        pg = (r["pg_version"] or "").splitlines()[0].strip() or "?"
        lines.append(
            f"{r['id']:>4}  {r['ts']:<24}  {r['database']:<20}  "
            f"{_human_bytes(r['size_bytes']):>10}  {pg:<8}  {state}"
        )
    return "\n".join(lines)

@mcp.tool()
def list_allows() -> str:
    """Show every registered write action and its current state.

    Reads from the tune SQLite config (tune_allows). This is the
    allow-list that gates every write operation pg-tune can perform:
    restore, ANALYZE, CREATE INDEX, and so on.

    A fresh install has only `backup` enabled. Everything else must
    be turned on explicitly with `set_allow` (or the CLI).
    """
    from pg_tune.config import DEFAULT_DB

    rows = list_settings("ALLOWS", DEFAULT_DB)
    if not rows:
        return "No actions registered."

    lines = [
        f"{'action':<26}  {'state':<10}  {'default':<10}  description"
    ]
    for r in rows:
        state = "enabled" if r["enabled"] else "disabled"
        default = "enabled" if r["default_enabled"] else "disabled"
        lines.append(
            f"{r['action']:<26}  {state:<10}  {default:<10}  "
            f"{r['description']}"
        )
    return "\n".join(lines)


@mcp.tool()
def set_allow(action: str, enabled: bool) -> str:
    """Enable or disable a write action.

    This is a security-relevant operation: turning `restore` on
    means an LLM can then drop and recreate the target database.
    Ask the user before flipping a switch, especially for
    `restore`, `create_index`, and `drop_own_objects`.

    Args:
        action:  Action name, e.g. ``restore``, ``analyze``,
                 ``create_index``. Case-sensitive.
        enabled: True to allow, False to deny.

    Use ``list_allows()`` to see the full list of action names.
    """
    from pg_tune.config import DEFAULT_DB

    try:
        r = set_setting(
            "ALLOWS", action, "1" if enabled else "0", DEFAULT_DB
        )
    except KeyError as e:
        return f"Error: {e}. Call list_allows() to see valid names."
    old = "enabled" if r["old"] == "1" else "disabled"
    new = "enabled" if r["new"] == "1" else "disabled"
    return f"{r['name']}: {old} → {new}\n  {r['desc']}"

@mcp.tool()
def list_settings(category: str = "") -> str:
    """Show settings by category (ALLOWS or SETTING).

    Empty category returns both. Values are shown with their
    defaults so drift is visible.
    """
    from pg_tune.config import DEFAULT_DB
    from pg_tune.config import list_settings as _list

    rows = _list(category, DEFAULT_DB)
    if not rows:
        return "No settings registered."

    lines = []
    for r in rows:
        mark = " *" if r["value"] != r["default_value"] else ""
        lines.append(
            f"{r['category']:<8} {r['name']:<26} "
            f"= {r['value']}{mark}  ({r['desc']})"
        )
    return "\n".join(lines)

@mcp.tool()
def status() -> str:
    """Show the current state of the target database.

    Connects to PGDATABASE (the same one pg-tune backs up) and
    lists user tables with row counts. Read-only.
    """
    import os

    import psycopg

    db = os.getenv("PGDATABASE")
    if not db:
        return "Error: PGDATABASE is not set"

    with psycopg.connect(dbname=db, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select schemaname, relname, n_live_tup
                from pg_stat_user_tables
                order by schemaname, relname
                """
            )
            rows = cur.fetchall()

    if not rows:
        return f"Database '{db}' has no user tables."

    lines = [f"Database: {db}", "", f"{'schema':<12}  {'table':<24}  rows"]
    for schema, table, n in rows:
        lines.append(f"{schema:<12}  {table:<24}  {n:,}")
    return "\n".join(lines)

def main() -> None:
    _check_startup_safety()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    mcp.run()


if __name__ == "__main__":
    main()

