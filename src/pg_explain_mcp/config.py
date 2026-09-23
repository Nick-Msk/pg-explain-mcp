"""SQLite-backed configuration for the PlanCheck registry."""

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
    if not db_path.exists():
        init_db(db_path)

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

    def __init__(self, db_path: Path | str = DEFAULT_DB, database: str = "postgres"):
        self._db_path = Path(db_path)
        self._database = database

    def load(self) -> tuple[PlanCheck, ...]:
        return load_checks(self._database, self._db_path)

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

    # stderr — MCP uses stdout for JSON-RPC framing.
    print(f"Initialized {db_path}", file=sys.stderr)

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
            rows = conn.execute(
                "select num, database, name, enabled from checks order by num"
            ).fetchall()
            for r in rows:
                mark = "on " if r["enabled"] else "off"
                print(f"{r['num']:>2}  [{mark}]  {r['name']}")
    else:
        parser.print_help()

