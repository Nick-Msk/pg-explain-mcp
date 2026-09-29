"""Command-line entry point for running EXPLAIN and parsing the plan.

Usage:

    # SQL as argument
    pg-explain-parse "select * from t limit 10"

    # SQL from a file — if the argument names an existing file, it is
    # read as SQL. Otherwise the string itself is treated as SQL.
    pg-explain-parse query.sql

    # SQL from stdin — pipe anything in
    cat query.sql | pg-explain-parse

    # Parse a saved plan JSON instead
    pg-explain-parse --plan plan.json
    cat plan.json | pg-explain-parse --plan -

Connection parameters are read from the same environment variables
as the MCP server: PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DATABASE.
"""

import argparse
import json
import sys
from pathlib import Path

from pg_explain_mcp.analyzer import (
    filtered_parse_plan,
    format_plan_tree,
    parse_plan,
)
from pg_explain_mcp.config import DEFAULT_DB, CheckRegistry
from pg_explain_mcp.db import explain_query

_registry = CheckRegistry(DEFAULT_DB)

def _read_file(path: str) -> str:
    """Read a file, or stdin if ``path`` is ``-``."""
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text()


def _resolve_sql(arg: str | None) -> str | None:
    """Resolve the SQL query from argument, file, or stdin.

    Order of preference:

    1. If ``arg`` is given and points to an existing file, read it.
    2. If ``arg`` is given (and is not a file), treat it as SQL text.
    3. If nothing is passed and stdin is not a TTY, read from stdin.
    4. Otherwise return ``None`` — caller reports an error.
    """
    if arg:
        p = Path(arg)
        if p.is_file():
            return p.read_text()
        return arg
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return None


def _emit(nodes: list[dict], as_json: bool, marker_tabs: int = 5) -> None:
    if as_json:
        print(json.dumps(nodes, indent=2, ensure_ascii=False))
    else:
        print(format_plan_tree(nodes, marker_tabs=marker_tabs))


def _parse_with_policy(plan_json: list, all_fields: bool) -> list:
    if all_fields:
        return parse_plan(plan_json)
    policy = _registry.load_field_policy()
    return filtered_parse_plan(plan_json, field_policy=policy)

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pg-explain-parse",
        description=(
            "Run EXPLAIN on a SQL query (or parse a saved JSON plan) "
            "and print the tree."
        ),
    )
    parser.add_argument(
        "sql_or_file",
        nargs="?",
        help=(
            "SQL text, or path to a file containing SQL. If omitted, "
            "SQL is read from stdin."
        ),
    )
    parser.add_argument(
        "--plan",
        metavar="PATH",
        help=(
            "Parse a saved EXPLAIN (FORMAT JSON) plan instead of "
            "running a query. Use '-' to read from stdin."
        ),
    )
    parser.add_argument(
        "-j", "--json",
        action="store_true",
        help="Print the flat node list as JSON (default: indented text).",
    )
    parser.add_argument(
        "--no-analyze",
        action="store_true",
        help=(
            "Skip ANALYZE — plan only, do not execute the query. "
            "Useful for potentially expensive queries."
        ),
    )
    parser.add_argument(
        "--no-buffers",
        action="store_true",
        help="Skip BUFFERS — do not report buffer usage.",
    )
    parser.add_argument(
        "--all-fields",
        action="store_true",
        help=(
            "Keep zero-valued numeric fields. By default they are "
            "dropped for readability."
        ),
    )
    parser.add_argument(
        "--marker-tabs",
        type=int,
        default=5,
        metavar="N",
        help=(
            "Tab characters between the node type and the [depth:sibling] "
            "marker. Default: 5."
        ),
    )
    args = parser.parse_args()

    if args.sql_or_file and args.plan:
        print(
            "error: provide either SQL (positional) or --plan, not both",
            file=sys.stderr,
        )
        return 2

    # --- mode 1: parse a saved plan JSON ----------------------------
    if args.plan:
        try:
            raw = _read_file(args.plan).strip()
        except OSError as e:
            print(f"error: cannot read {args.plan}: {e}", file=sys.stderr)
            return 1
        if not raw:
            print("error: empty input", file=sys.stderr)
            return 2
        try:
            plan_json = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"error: input is not valid JSON: {e}", file=sys.stderr)
            return 1

        nodes = _parse_with_policy(plan_json, args.all_fields)
        _emit(nodes, args.json, args.marker_tabs)

        return 0

    # --- mode 2: run EXPLAIN on SQL ---------------------------------
    sql = _resolve_sql(args.sql_or_file)
    if sql is None:
        print(
            "error: no SQL provided — pass it as an argument, a file "
            "path, or on stdin",
            file=sys.stderr,
        )
        return 2
    sql = sql.strip()
    if not sql:
        print("error: empty SQL", file=sys.stderr)
        return 2

    try:
        raw = explain_query(
            sql,
            analyze=not args.no_analyze,
            buffers=not args.no_buffers,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        print(
            "hint: set the connection environment variables, e.g.\n"
            "    PG_HOST=127.0.0.1 PG_USER=your_user "
            "PG_PASSWORD=your_password PG_DATABASE=your_db",
            file=sys.stderr,
        )
        return 1

    nodes = _parse_with_policy(raw["QUERY PLAN"], args.all_fields)
    _emit(nodes, args.json, args.marker_tabs)

    return 0


if __name__ == "__main__":
    sys.exit(main())

