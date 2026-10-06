"""Console scripts for pg-tune: backup, restore."""

import argparse
import json
import os
import sys

from pg_tune.backup import BackupError, make_backup
from pg_tune.config import reset_setting, set_setting
from pg_tune.restore import RestoreError, do_restore, inspect_restore


def _human_bytes(n: int | None) -> str:
    if not n:
        return "0 B"
    value = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"

def _print_allows(db_path) -> None:
    from pg_tune.config import list_allows
    rows = list_allows(db_path)
    if not rows:
        print("No actions registered.")
        return
    print(f"{'action':<26}  {'state':<10}  {'default':<10}  description")
    for r in rows:
        state = "enabled" if r["enabled"] else "disabled"
        default = "enabled" if r["default_enabled"] else "disabled"
        print(
            f"{r['action']:<26}  {state:<10}  {default:<10}  "
            f"{r['description']}"
        )

def _print_settings(category: str, db_path) -> None:
    from itertools import groupby

    from pg_tune.config import list_settings

    rows = list_settings(category, db_path)
    if not rows:
        print("No settings registered.")
        return

    rows.sort(key=lambda r: (r["category"], r["name"]))
    for cat, group in groupby(rows, key=lambda r: r["category"]):
        print(f"{cat}:")

        if cat == "ALLOWS":
            print(
                f"  {'action':<26}  {'state':<10}  "
                f"{'default':<10}  description"
            )
            for r in group:
                state = "enabled" if r["value"] == "1" else "disabled"
                default = (
                    "enabled" if r["default_value"] == "1"
                    else "disabled"
                )
                print(
                    f"  {r['name']:<26}  {state:<10}  "
                    f"{default:<10}  {r['desc']}"
                )
        else:
            print(
                f"  {'name':<26}  {'value':<8}  "
                f"{'default':<8}  description"
            )
            for r in group:
                mark = " *" if r["value"] != r["default_value"] else ""
                print(
                    f"  {r['name']:<26}  {r['value'] + mark:<8}  "
                    f"{r['default_value']:<8}  {r['desc']}"
                )
        print()

def _print_vec_params(db_path) -> None:
    from pg_tune.config import list_vec_params
    rows = list_vec_params(db_path)
    if not rows:
        print("No vector parameters registered.")
        return
    print(
        f"{'name':<24}  {'scope':<10}  {'raw_key':<22}  "
        f"{'measure':<8}  description"
    )
    for r in rows:
        print(
            f"{r['name']:<24}  {r['scope']:<10}  {r['raw_key']:<22}  "
            f"{r['measure']:<8}  {r['desc']}"
        )

def backup_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pg-tune-backup",
        description=(
            "Snapshot a database with pg_dump and register the "
            "result in tune_backups."
        ),
    )
    p.add_argument(
        "-d", "--database",
        default=None,
        help="Database name. Defaults to PGDATABASE.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print the backup reference as JSON.",
    )
    args = p.parse_args(argv)

    try:
        ref = make_backup(args.database)
    except BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(ref, indent=2))
    else:
        print(f"backup #{ref['id']} -> {ref['path']}")
        print(f"  database: {ref['database']}")
        print(f"  size:     {_human_bytes(ref['size_bytes'])}")
        print(f"  sha256:   {ref['sha256']}")
        print(f"  pg:       {ref['pg_version']}")
    return 0

def _resolve_backup_ref(ref: str, database: str | None) -> int:
    """Accept a numeric id or '@latest'.

    '@latest' resolves to the most recent backup — filtered by
    ``database`` if given, otherwise across all databases.
    """
    if ref.isdigit():
        return int(ref)
    if ref != "@latest":
        raise SystemExit(f"error: invalid backup reference {ref!r}")

    from pg_tune.config import DEFAULT_DB, _connect, _ensure_db
    _ensure_db(DEFAULT_DB)

    with _connect(DEFAULT_DB) as conn:
        if database:
            row = conn.execute(
                "select id from tune_backups where database = ? "
                "order by ts desc limit 1",
                (database,),
            ).fetchone()
            if row is None:
                raise SystemExit(
                    f"error: no backups for database {database!r}"
                )
        else:
            row = conn.execute(
                "select id from tune_backups order by ts desc limit 1"
            ).fetchone()
            if row is None:
                raise SystemExit("error: no backups registered")

    return row["id"]

def restore_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pg-tune-restore",
        description=(
            "Restore a database from a pg-tune backup. DESTRUCTIVE — "
            "drops and recreates the target database."
        ),
    )
    p.add_argument(
        "backup",
        help=(
            "Backup id, or '@latest' for the most recent backup of "
            "the PGDATABASE target."
        ),
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Actually run the restore. Without this flag, only a "
            "dry-run summary is printed."
        ),
    )
    args = p.parse_args(argv)

    try:
        backup_id = _resolve_backup_ref(args.backup, os.getenv("PGDATABASE"))

        if not args.confirm:
            info = inspect_restore(backup_id)
            b = info["backup"]
            print(f"Dry run. Backup #{b['id']} — {b['database']}")
            print(f"  file:        {b['path']}")
            print(f"  size:        {_human_bytes(b['size_bytes'])}")
            print(f"  sha256:      {b['sha256']}")
            print(f"  present:     {info['file_present']}")
            print(f"  sha match:   {info['sha256_match']}")
            print(f"  conns:       {len(info['active_connections'])}")
            print(f"  allowed:     {info['allowed']}")
            print(f"  writes env:  {info['writes_env']}")
            print()
            print(
                f"⚠️  restore will DROP and recreate '{b['database']}'. "
                "Re-run with --confirm."
            )
            return 0

        result = do_restore(backup_id)
        print(
            f"restore complete: {result['database']} "
            f"({_human_bytes(result['size_bytes'])}, "
            f"{result['elapsed_s']:.1f} s)"
        )
        return 0
    except RestoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

def config_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pg-tune-config",
        description="Manage the pg-tune SQLite configuration.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--init",
        action="store_true",
        help="Rebuild tune.db from schema + seed.",
    )
    g.add_argument(
        "--show",
        nargs="?",
        const="",
        metavar="CATEGORY",
        help=(
            "Print settings. Optionally filter by category "
            "(ALLOWS or SETTING)."
        ),
    )
    g.add_argument(
        "--show-params",
        action="store_true",
        help="Print tune_vec_params only.",
    )
    g.add_argument(
        "--set",
        nargs=3,
        metavar=("CATEGORY", "NAME", "VALUE"),
        help="Set one value: --set SETTING default_cold_run 5",
    )
    g.add_argument(
        "--allow",
        metavar="ACTION",
        help="Sugar for --set ALLOWS <ACTION> 1.",
    )
    g.add_argument(
        "--deny",
        metavar="ACTION",
        help="Sugar for --set ALLOWS <ACTION> 0.",
    )
    g.add_argument(
        "--reset",
        nargs="+",
        metavar="CATEGORY [NAME]",
        help=(
            "Reset one category, or one row. "
            "Category is required; use --reset-all for everything."
        ),
    )
    g.add_argument(
        "--reset-all",
        action="store_true",
        help="Reset every setting to its seed default.",
    )
    args = p.parse_args(argv)

    from pg_tune.config import (
        DEFAULT_DB,
        init_db,
    )

    if args.init:
        init_db(DEFAULT_DB)
        return 0

    if args.show is not None:
        _print_settings(args.show, DEFAULT_DB)
        return 0

    if args.show_params:
        _print_vec_params(DEFAULT_DB)
        return 0

    if args.set:
        category, name, value = args.set
        try:
            r = set_setting(category, name, value, DEFAULT_DB)
        except KeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"{r['category']}.{r['name']}: {r['old']} → {r['new']}")
        return 0

    if args.allow:
        try:
            set_setting("ALLOWS", args.allow, "1", DEFAULT_DB)
        except KeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"{args.allow}: enabled")
        return 0

    if args.deny:
        try:
            set_setting("ALLOWS", args.deny, "0", DEFAULT_DB)
        except KeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"{args.deny}: disabled")
        return 0

    if args.reset:
        if len(args.reset) > 2:
            print(
                "error: --reset takes CATEGORY and optional NAME",
                file=sys.stderr,
            )
            return 1
        category = args.reset[0]
        name = args.reset[1] if len(args.reset) == 2 else ""
        try:
            changes = reset_setting(category, name, DEFAULT_DB)
        except KeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not changes:
            print("Already at defaults.")
            return 0
        for c in changes:
            print(f"{c['category']}.{c['name']}: {c['old']} → {c['new']}")
        return 0

    if args.reset_all:
        changes = reset_setting("", "", DEFAULT_DB)
        if not changes:
            print("Already at defaults.")
            return 0
        for c in changes:
            print(f"{c['category']}.{c['name']}: {c['old']} → {c['new']}")
        return 0

    return 0

def list_backups_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pg-tune-list_backups",
        description="List pg-tune backups registered in tune.db.",
    )
    p.add_argument(
        "-d", "--database",
        default=None,
        help="Filter by database name.",
    )
    p.add_argument(
        "-n", "--limit",
        type=int,
        default=20,
        help="Show at most N rows (default: 20).",
    )
    args = p.parse_args(argv)

    from pg_tune.config import DEFAULT_DB, _connect, _ensure_db
    _ensure_db(DEFAULT_DB)

    sql = (
        "select id, ts, database, size_bytes, pg_version, "
        "       restored_ts, path "
        "from tune_backups"
    )
    params: list = []
    if args.database:
        sql += " where database = ?"
        params.append(args.database)
    sql += " order by ts desc limit ?"
    params.append(args.limit)

    with _connect(DEFAULT_DB) as conn:
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        print("No backups.")
        return 0

    # header
        print(
        f"{'id':>4}  {'ts':<24}  {'database':<20}  "
        f"{'size':>10}  {'pg':<8}  state"
    )
    for r in rows:
        if r["restored_ts"]:
            state = f"restored {r['restored_ts'][:19]}"
        else:
            state = "available"
        pg = (r["pg_version"] or "").splitlines()[0].strip() or "?"
        print(
            f"{r['id']:>4}  {r['ts']:<24}  {r['database']:<20}  "
            f"{_human_bytes(r['size_bytes']):>10}  "
            f"{pg:<8}  {state}"
        )
    return 0
