"""Command-line entry point for running EXPLAIN and parsing the plan.

Usage:

    # Run EXPLAIN on a query
    pg-explain-parse "select * from t limit 10"

    # Plan only — do not execute the query
    pg-explain-parse --no-analyze "select * from t"

    # JSON output
    pg-explain-parse --json "select * from t"

    # Parse a saved JSON plan instead of running a query
    pg-explain-parse -f plan.json
    psql -tA -c "explain (format json) select 1" | pg-explain-parse -f -

Connection parameters are read from the same environment variables
as the MCP server: PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DATABASE.
"""

import argparse
import json
import sys

from pg_explain_mcp.analyzer import (
    filtered_parse_plan,
    format_plan_tree,
    parse_plan,
)
from pg_explain_mcp.db import explain_query


def _read_json_file(path: str) -> str:
    """Read plan JSON from a file, or from stdin if ``path`` is ``-``."""
    if path == "-":
        return sys.stdin.read()
    with open(path) as f:
        return f.read()


def _emit(nodes: list[dict], as_json: bool) -> None:
    """Print the parsed nodes as JSON or indented text."""
    if as_json:
        print(json.dumps(nodes, indent=2, ensure_ascii=False))
    else:
        print(format_plan_tree(nodes))


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pg-explain-parse",
        description=(
            "Run EXPLAIN on a SQL query (or parse a saved JSON plan) "
            "and print the tree."
        ),
    )
    parser.add_argument(
        "sql",
        nargs="?",
        help="SQL query to EXPLAIN. Mutually exclusive with --file.",
    )
    parser.add_argument(
        "-f", "--file",
        metavar="PATH",
        help=(
            "Read plan JSON from a file instead of running a query. "
            "Use '-' to read from stdin."
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
    args = parser.parse_args()

    if args.sql and args.file:
        print(
            "error: provide either a SQL query or --file, not both",
            file=sys.stderr,
        )
        return 2
    if not args.sql and not args.file:
        print(
            "error: provide a SQL query or --file PATH",
            file=sys.stderr,
        )
        return 2

    # --- mode 1: parse a saved JSON plan ----------------------------
    if args.file:
        raw = _read_json_file(args.file).strip()
        if not raw:
            print("error: empty input", file=sys.stderr)
            return 2
        try:
            plan_json = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"error: input is not valid JSON: {e}", file=sys.stderr)
            return 1

        if args.all_fields:
            nodes = parse_plan(plan_json)
        else:
            nodes = filtered_parse_plan(plan_json)

        _emit(nodes, args.json)
        return 0

    # --- mode 2: run EXPLAIN on the given SQL -----------------------
    try:
        raw = explain_query(
            args.sql,
            analyze=not args.no_analyze,
            buffers=not args.no_buffers,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        print(
            "hint: check PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, "
            "PG_DATABASE",
            file=sys.stderr,
        )
        return 1

    if args.all_fields:
        nodes = parse_plan(raw["QUERY PLAN"])
    else:
        nodes = filtered_parse_plan(raw["QUERY PLAN"])

    _emit(nodes, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())

