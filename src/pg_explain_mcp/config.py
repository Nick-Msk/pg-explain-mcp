"""SQLite-backed configuration for the PlanCheck registry."""

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

from pg_explain_mcp.analyzer import (
    BitmapHeapScanCheck,
    DiskSpillHashCheck,
    DiskSpillSortCheck,
    EstimateMismatchCheck,
    IndexScanCheck,
    NestedLoopCheck,
    PlanCheck,
    SeqScanCheck,
)

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
DEFAULT_DB = _CONFIG_DIR / "checks.db"
TARGET_DB_TYPE = os.getenv("TARGET_DB_TYPE", "postgres")

# class name → (class, {param_name: type})
_REGISTRY: dict[str, tuple[type, dict[str, Callable[[str], Any]]]] = {
    "SeqScanCheck": (SeqScanCheck, {
        "threshold_rows":   int,
        "min_filter_ratio": float,
    }),
    "EstimateMismatchCheck": (EstimateMismatchCheck, {
        "threshold_ratio": float,
        "min_rows":        int,
    }),
    "DiskSpillSortCheck": (DiskSpillSortCheck, {
        "min_spill_kb": int,
    }),
    "DiskSpillHashCheck": (DiskSpillHashCheck, {
        "min_batches": int,
    }),
    "NestedLoopCheck": (NestedLoopCheck, {
        "threshold_loops": int,
        "threshold_rows":  int,
    }),
    "BitmapHeapScanCheck": (BitmapHeapScanCheck, {
        "threshold_rows": int,
    }),
    "IndexScanCheck": (IndexScanCheck, {
        "min_rows":         int,
        "heap_fetch_ratio": float,
        "min_disk_blocks":  int,
    }),
}

# ---------------------------------------------------------------------------
# Runtime management (read / write from MCP tools)
# ---------------------------------------------------------------------------


def _validate_value(checker: str, param: str, value: str) -> None:
    """Ensure the value parses as the type declared in ``_REGISTRY``.

    Raises ``KeyError`` if the check or param is unknown, ``ValueError``
    if the value cannot be coerced.
    """
    if checker not in _REGISTRY:
        raise KeyError(f"Unknown checker: {checker}")
    _, param_types = _REGISTRY[checker]
    if param not in param_types:
        raise KeyError(f"Unknown param '{param}' for {checker}")
    try:
        param_types[param](value)
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
    database: str = "postgres",
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
    "databases":    {"database"},
    "checks":       {"num", "database", "name", "description", "enabled"},
    "check_params": {"num", "database", "param", "value", "default_value"},
    "plan_fields":  {"database", "raw", "key", "enabled"},
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
    database: str = "postgres",
    db_path: Path | str = DEFAULT_DB,
) -> tuple[PlanCheck, ...]:
    """Load enabled checks for ``database`` from the SQLite config.

    Checks are returned ordered by ``num`` — the order in the seed file.
    Params are coerced to the Python type declared in ``_REGISTRY``.

    If the config database does not exist, it is created from
    ``config/schema.sql`` and ``config/seed.sql`` before loading.
    """

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

        checks: list[PlanCheck] = []
        for row in rows:
            num, name = row["num"], row["name"]
            if name not in _REGISTRY:
                raise KeyError(
                    f"Check {num} '{name}' in config has no class in _REGISTRY"
                )
            cls, param_types = _REGISTRY[name]

            param_rows = conn.execute(
                "select param, value from check_params "
                "where database = ? and num = ?",
                (database, num),
            ).fetchall()

            params: dict[str, Any] = {}
            for p in param_rows:
                pname, pval = p["param"], p["value"]
                if pname not in param_types:
                    raise KeyError(f"Unknown param '{pname}' for {name}")
                params[pname] = param_types[pname](pval)

            checks.append(cls(**params))

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

    def load(self) -> tuple[PlanCheck, ...]:
        return load_checks(TARGET_DB_TYPE, self._db_path)

    def load_fields(self) -> dict[str, str]:
        return load_plan_fields(TARGET_DB_TYPE, self._db_path)

def init_db(db_path: Path | str = DEFAULT_DB) -> None:
    """Create or rebuild the config database from schema.sql and seed.sql.

    If the database file already exists, it is removed and rebuilt from
    scratch — the seed file contains plain ``insert`` statements, so
    re-running against an existing schema would violate constraints.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Remove the old file, if any. Safe on macOS/Linux even if a running
    # process still has the file open — the old inode stays alive until
    # that process closes it, and the next `load()` will pick up the new
    # file by path.
    if db_path.exists():
        db_path.unlink()

    schema = (_CONFIG_DIR / "schema.sql").read_text()
    seed = (_CONFIG_DIR / "seed.sql").read_text()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema)
        conn.executescript(seed)

        # Sanity check: schema and seed must produce a non-empty registry.
        n_checks = conn.execute("select count(*) from checks").fetchone()[0]
        n_fields = conn.execute("select count(*) from plan_fields").fetchone()[0]
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

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m pg_explain_mcp.config",
        description="Manage the SQLite check registry for pg-explain-mcp.",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Rebuild config/checks.db from schema.sql and seed.sql.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print the current registry as a table and exit.",
    )
    args = parser.parse_args()

    if args.init:
        init_db()
    elif args.show:
        with sqlite3.connect(DEFAULT_DB) as conn:
            conn.row_factory = sqlite3.Row
            print("databases:")
            for r in conn.execute("select database from databases order by database"):
                print(f"  {r['database']}")
            print("checks:")
            for r in conn.execute(
                "select num, database, name, enabled from checks "
                "order by database, num"
            ):
                mark = "on " if r["enabled"] else "off"
                print(f"  {r['database']}  {r['num']:>2}  [{mark}]  {r['name']}")
            print("plan fields:")
            for r in conn.execute(
                "select database, raw, key, enabled from plan_fields "
                "order by database, raw"
            ):
                mark = "on " if r["enabled"] else "off"
                print(f"  {r['database']}  [{mark}]  {r['raw']!r} → {r['key']!r}")
    else:
        parser.print_help()

