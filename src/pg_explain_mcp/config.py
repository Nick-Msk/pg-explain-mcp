"""SQLite-backed configuration for the PlanCheck registry."""

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from pg_explain_mcp.analyzer import (
    BitmapHeapScanCheck,
    CheckBase,
    DiskSpillHashCheck,
    DiskSpillSortCheck,
    EstimateMismatchCheck,
    IndexOnlyScanCheck,
    IndexRegularScanCheck,
    NestedLoopCheck,
    NonSargableCheck,
    PartitionPruningCheck,
    SeqScanCheck,
)

from contextlib import contextmanager

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
DEFAULT_DB = _CONFIG_DIR / "checks.db"
TARGET_DB_TYPE = os.getenv("TARGET_DB_TYPE", "postgres")

@contextmanager
def audit_writer(who: str, db_path: Path = DEFAULT_DB):
    """Tag every config write inside the block with ``who``."""
    _ensure_db(db_path)
    with sqlite3.connect(db_path) as conn:
        prev = conn.execute(
            "select who from _audit_session where id = 1"
        ).fetchone()[0]
        conn.execute(
            "update _audit_session set who = ? where id = 1", (who,)
        )
        conn.commit()
    try:
        yield
    finally:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "update _audit_session set who = ? where id = 1", (prev,)
            )
            conn.commit()

def set_audit_writer(
    who: str = "system",
    db_path: Path | str = DEFAULT_DB,
) -> None:
    """Tag subsequent config writes with ``who``.

    Writes go to ``_audit_session.who``; triggers read it via a
    scalar subquery when they insert into ``config_audit``.
    """
    db_path = Path(db_path)
    _ensure_db(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "update _audit_session set who = ? where id = 1",
            (who,),
        )
        conn.commit()

def history(
    table_name: str = "",
    column_name: str = "",
    optype: str = "",
    count: int = 0,
    include_seed: bool = False,
    db_path: Path | str = DEFAULT_DB,
) -> list[dict[str, Any]]:
    """Return ``config_audit`` rows, most recent first.

    - empty ``table_name`` / ``column_name`` / ``optype`` are wildcards;
    - ``optype`` is one of ``'I'`` (insert), ``'U'`` (update),
      ``'D'`` (delete);
    - ``count = 0`` means "no limit";
    - ``count > 0`` limits to the newest N rows.
    """
    db_path = Path(db_path)
    _ensure_db(db_path)

    where: list[str] = []
    params: list[Any] = []
    if table_name:
        where.append("table_name = ?")
        params.append(table_name)
    if column_name:
        where.append("column_name = ?")
        params.append(column_name)
    if optype:
        where.append("optype = ?")
        params.append(optype)
    if not include_seed:
        where.append("who <> 'seed'")

    sql = (
        "select ts, table_name, column_name, optype, "
        "       old_value, new_value, who "
        "from config_audit"
    )
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by ts desc, id desc"
    if count > 0:
        sql += " limit ?"
        params.append(count)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

# class name → class. Each class declares its own PARAMS.
_REGISTRY: dict[str, type[CheckBase]] = {
    "SeqScanCheck":          SeqScanCheck,
    "EstimateMismatchCheck": EstimateMismatchCheck,
    "DiskSpillSortCheck":    DiskSpillSortCheck,
    "DiskSpillHashCheck":    DiskSpillHashCheck,
    "NestedLoopCheck":       NestedLoopCheck,
    "BitmapHeapScanCheck":   BitmapHeapScanCheck,
    "IndexRegularScanCheck": IndexRegularScanCheck,
    "IndexOnlyScanCheck":    IndexOnlyScanCheck,
    "PartitionPruningCheck": PartitionPruningCheck,
    "NonSargableCheck":      NonSargableCheck
}

# ---------------------------------------------------------------------------
# Runtime management (read / write from MCP tools)
# ---------------------------------------------------------------------------


def _validate_value(checker: str, param: str, value: str) -> None:
    if checker not in _REGISTRY:
        raise KeyError(f"Unknown checker: {checker}")
    cls = _REGISTRY[checker]
    if param not in cls.PARAMS:
        raise KeyError(f"Unknown param '{param}' for {checker}")
    try:
        cls.PARAMS[param](value)
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"Invalid value {value!r} for {checker}.{param}: {e}"
        ) from e

def show_params(
    checker: str | None = None,
    database: str = "postgres",
    db_path: Path | str = DEFAULT_DB,
) -> list[dict[str, Any]]:
    """Return current and default values for check params.

    If ``checker`` is given, filter to that check. Returns rows with
    keys: ``checker``, ``param``, ``value``, ``default_value``, ``changed``.
    """
    db_path = Path(db_path)
    _ensure_db(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        sql = (
            "select c.name as checker, p.param, p.value, p.default_value "
            "from check_params p "
            "join checks c on c.num = p.num and c.database = p.database "
            "where p.database = ? "
        )
        params: tuple = (database,)
        if checker:
            sql += "and c.name = ? "
            params = (database, checker)
        sql += "order by c.num, p.param"

        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "checker": r["checker"],
                "param": r["param"],
                "value": r["value"],
                "default_value": r["default_value"],
                "changed": r["value"] != r["default_value"],
            }
            for r in rows
        ]


def set_param(
    checker: str,
    param: str,
    value: str,
    database: str = TARGET_DB_TYPE,
    db_path: Path | str = DEFAULT_DB,
) -> dict[str, Any]:
    """Set a param's current value. Validates the type first.

    Returns ``{"checker": ..., "param": ..., "old": ..., "new": ...}``.
    """
    db_path = Path(db_path)
    _ensure_db(db_path)
    _validate_value(checker, param, value)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select p.value from check_params p "
            "join checks c on c.num = p.num and c.database = p.database "
            "where p.database = ? and c.name = ? and p.param = ?",
            (database, checker, param),
        ).fetchone()
        if row is None:
            raise KeyError(f"No param {checker}.{param} in database")

        old = row["value"]
        conn.execute(
            "update check_params set value = ? "
            "where database = ? and param = ? "
            "and num = (select num from checks "
            "           where database = ? and name = ?)",
            (value, database, param, database, checker),
        )
        conn.commit()

    return {"checker": checker, "param": param, "old": old, "new": value}


def reset_param(
    checker: str,
    param: str | None = None,
    database: str = "postgres",
    db_path: Path | str = DEFAULT_DB,
) -> list[dict[str, Any]]:
    """Reset params to their default values.

    If ``param`` is given, reset only that one. Otherwise reset every
    param of the check. Returns the list of changes made.
    """
    db_path = Path(db_path)
    _ensure_db(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        sql = (
            "select c.name as checker, p.param, p.value, p.default_value "
            "from check_params p "
            "join checks c on c.num = p.num and c.database = p.database "
            "where p.database = ? and c.name = ? "
        )
        args: tuple = (database, checker)
        if param:
            sql += "and p.param = ? "
            args = (database, checker, param)

        rows = conn.execute(sql, args).fetchall()
        if not rows:
            raise KeyError(
                f"No params for {checker}"
                + (f".{param}" if param else "")
            )

        changes = []
        for r in rows:
            if r["value"] == r["default_value"]:
                continue
            conn.execute(
                "update check_params set value = ? "
                "where database = ? and param = ? "
                "and num = (select num from checks "
                "           where database = ? and name = ?)",
                (r["default_value"], database, r["param"], database, checker),
            )
            changes.append({
                "checker": checker,
                "param": r["param"],
                "old": r["value"],
                "new": r["default_value"],
            })
        conn.commit()

    return changes

# Columns that must exist. If any is missing, the file is stale and
# gets rebuilt. Keep this list in sync with schema.sql.
_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "databases":      {"database"},
    "checks":         {"num", "database", "name", "description", "enabled"},
    "tags":           {"name"},
    "checks_tags":    {"num", "database", "tag"},
    "check_params":   {"num", "database", "param", "value", "default_value"},
    "plan_fields":    {"database", "raw", "key", "enabled"},
    "config_audit":   {"id", "table_name", "column_name", "optype",
                       "ts", "old_value", "new_value", "who"},
    "_audit_session": {"id", "who"},
}

def _ensure_db(db_path: Path) -> None:
    """Ensure the config DB exists *and* matches the expected schema.

    Checks both that the required tables are present and that each
    table carries the required columns. If anything is missing — the
    file is stale, empty, or corrupted — it is rebuilt from
    ``schema.sql`` and ``seed.sql``.
    """
    if not db_path.exists():
        init_db(db_path)
        return

    try:
        with sqlite3.connect(db_path) as conn:
            for table, required in _REQUIRED_COLUMNS.items():
                rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
                if not rows:
                    raise sqlite3.OperationalError(f"missing table: {table}")
                present = {r[1] for r in rows}
                missing = required - present
                if missing:
                    raise sqlite3.OperationalError(
                        f"table {table} missing columns: {sorted(missing)}"
                    )
    except sqlite3.OperationalError:
        init_db(db_path)

def load_checks(
    database: str = TARGET_DB_TYPE,
    db_path: Path | str = DEFAULT_DB,
) -> tuple[CheckBase, ...]:
    db_path = Path(db_path)
    _ensure_db(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select num, name from checks "
            "where database = ? and enabled = 1 "
            "order by num",
            (database,),
        ).fetchall()

        checks: list[CheckBase] = []
        for row in rows:
            num, name = row["num"], row["name"]
            if name not in _REGISTRY:
                raise KeyError(
                    f"Check {num} '{name}' in config has no class in _REGISTRY"
                )

            param_rows = conn.execute(
                "select param, value from check_params "
                "where database = ? and num = ?",
                (database, num),
            ).fetchall()
            params = {r["param"]: r["value"] for r in param_rows}

            checks.append(_REGISTRY[name](params=params))

        return tuple(checks)

class CheckRegistry:
    """Re-reads the check list from SQLite on every ``load()``.

    Constructed once at server start; ``load()`` is called on each
    ``explain`` so that edits to the config take effect immediately,
    without restarting the MCP server.
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self._db_path = Path(db_path)

    @property
    def target(self) -> str:
        return TARGET_DB_TYPE

    def load(self) -> tuple[CheckBase, ...]:
        return load_checks(TARGET_DB_TYPE, self._db_path)

    def load_fields(self) -> dict[str, str]:
        return load_plan_fields(TARGET_DB_TYPE, self._db_path)

    def load_field_policy(self) -> dict[str, int]:
        return load_field_policy(TARGET_DB_TYPE, self._db_path)

    def load_field_config(self) -> dict[str, tuple[str, int]]:
        return load_field_config(TARGET_DB_TYPE, self._db_path)

    def history(
        self,
        table_name: str = "",
        column_name: str = "",
        optype: str = "",
        count: int = 0,
        include_seed: bool = False
    ) -> list[dict[str, Any]]:
        return history(
            table_name, column_name, optype, count, self._db_path
        )

    def set_audit_writer(self, who: str = "system") -> None:
        set_audit_writer(who, self._db_path)

    @contextmanager
    def audit_writer(self, who: str):
        with audit_writer(who, self._db_path):
            yield

_SEED_OBJECTS_DROP = (
    "drop view  if exists checks_with_tags",
    "drop table if exists check_params",
    "drop table if exists checks_tags",
    "drop table if exists checks",
    "drop table if exists tags",
    "drop table if exists plan_fields",
    "drop table if exists databases",
)

def init_db(db_path: Path | str = DEFAULT_DB) -> None:
    """Create or rebuild the config database from schema.sql and seed.sql.

    Seed tables are dropped and rebuilt. ``config_audit`` and
    ``_audit_session`` are preserved across rebuilds — the audit log
    outlives the seed configuration it records.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema = (_CONFIG_DIR / "schema.sql").read_text()
    seed = (_CONFIG_DIR / "seed.sql").read_text()

    with sqlite3.connect(db_path) as conn:
        for stmt in _SEED_OBJECTS_DROP:
            conn.execute(stmt)
        conn.executescript(schema)
        conn.executescript(seed)

        n_checks = conn.execute(
            "select count(*) from checks"
        ).fetchone()[0]
        n_fields = conn.execute(
            "select count(*) from plan_fields"
        ).fetchone()[0]
        if n_checks == 0 or n_fields == 0:
            raise RuntimeError(
                f"Seed produced {n_checks} checks / {n_fields} plan fields "
                f"— check {_CONFIG_DIR}/seed.sql"
            )

    print(
        f"Initialized {db_path} ({n_checks} checks, {n_fields} fields)",
        file=sys.stderr,
    )

def load_plan_fields(
    database: str = "postgres",
    db_path: Path | str = DEFAULT_DB,
) -> dict[str, str]:
    """Return the raw→key mapping for ``plan_nodes`` output.

    Loaded from ``plan_fields``. If the database is missing, it is
    created from ``schema.sql`` and ``seed.sql`` first.
    """
    db_path = Path(db_path)
    _ensure_db(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "select raw, key from plan_fields "
            "where database = ? and enabled = 1",
            (database,),
        ).fetchall()
        return {raw: key for raw, key in rows}

def load_field_policy(
    database: str = TARGET_DB_TYPE,
    db_path: Path | str = DEFAULT_DB,
) -> dict[str, int]:
    """Return {raw_field_name: mode} for parse output filtering.

    Modes:

    - ``0``   — hide the field entirely.
    - ``1``   — keep the field, including zero values.
    - ``999`` — unknown semantics: keep, but drop numeric zeros.

    Fields not listed in ``plan_fields`` are treated as mode 999.
    """
    db_path = Path(db_path)
    _ensure_db(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "select raw, enabled from plan_fields where database = ?",
            (database,),
        ).fetchall()
        return {raw: mode for raw, mode in rows}

def load_field_config(
    database: str = TARGET_DB_TYPE,
    db_path: Path | str = DEFAULT_DB,
) -> dict[str, tuple[str, int]]:
    """Return {raw: (key, mode)} for plan output.

    ``key`` is the compact field name (``actual_rows``), ``mode`` is
    the filtering policy (0=hide, 1=keep zeros, 999=drop zeros).
    """
    db_path = Path(db_path)
    _ensure_db(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "select raw, key, enabled from plan_fields where database = ?",
            (database,),
        ).fetchall()
        return {raw: (key, mode) for raw, key, mode in rows}

def main(argv: list[str] | None = None) -> int:
    """Entry point for `pg-explain-config` console script."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="pg-explain-config",
        description="Manage the SQLite config for pg-explain-mcp.",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Rebuild config/checks.db from schema.sql and seed.sql.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print the current registry and plan field policy.",
    )
    args = parser.parse_args(argv)

    if args.init:
        init_db()
        return 0

    if args.show:
        _show()
        return 0

    parser.print_help()
    return 0


def _show() -> None:
    """Print databases, checks, and plan field policy."""
    with sqlite3.connect(DEFAULT_DB) as conn:
        conn.row_factory = sqlite3.Row

        print("databases:")
        for r in conn.execute(
            "select database from databases order by database"
        ):
            print(f"  {r['database']}")

        print("checks:")
        for r in conn.execute(
            "select num, database, name, enabled, tags "
            "from checks_with_tags "
            "order by database, num"
        ):
            mark = "on " if r["enabled"] else "off"
            tags = f" [{r['tags']}]" if r["tags"] else ""
            print(
                f"  {r['database']}  {r['num']:>2}  [{mark}]  "
                f"{r['name']}{tags}"
            )

        _FIELD_MODES = {0: "hide", 1: "keep", 999: "auto"}

        print("plan fields:")
        for r in conn.execute(
            "select database, raw, key, enabled from plan_fields "
            "order by database, raw"
        ):
            mode = _FIELD_MODES.get(r["enabled"], f"?{r['enabled']}")
            print(
                f"  {r['database']}  [{mode:4}]  "
                f"{r['raw']!r} → {r['key']!r}"
            )

def set_audit_writer(self, who: str) -> None:
    """Tag subsequent config writes with ``who`` in ``config_audit``.

    Call this before any batch of writes that should be attributed
    to a specific caller. Reset to ``'system'`` afterwards if needed.
    """
    with self._connect() as conn:
        conn.execute(
            "update _audit_session set who = ? where id = 1",
            (who,),
        )
        conn.commit()

if __name__ == "__main__":
    import sys
    sys.exit(main())

