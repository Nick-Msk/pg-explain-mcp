"""SQLite-backed configuration for the PlanCheck registry."""

import sqlite3
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
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(
            f"Config database not found: {db_path}. "
        )

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
    """Create or rebuild the config database from schema.sql and seed.sql."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema = (_CONFIG_DIR / "schema.sql").read_text()
    seed = (_CONFIG_DIR / "seed.sql").read_text()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema)
        conn.executescript(seed)

    print(f"Initialized {db_path}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--init":
        init_db()
    else:
        print("Usage: python -m pg_explain_mcp.config --init")

