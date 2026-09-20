"""MCP server for PostgreSQL query plan analysis."""

import json

from mcp.server.fastmcp import FastMCP

from pg_explain_mcp.analyzer import analyze_plan, summarize_plan_node
from pg_explain_mcp.db import explain_query, get_schema

mcp = FastMCP("pg-explain")


@mcp.tool()
def ping() -> str:
    """Health check — returns 'pong' if the server is running."""
    return "pong"


@mcp.tool()
def list_tables() -> str:
    """Return a list of all user tables and their columns."""
    rows = get_schema()
    tables: dict[str, list[str]] = {}
    for row in rows:
        key = f"{row['table_schema']}.{row['table_name']}"
        tables.setdefault(key, []).append(f"{row['column_name']} {row['data_type']}")
    result = "\n".join(f"{name}: {', '.join(cols)}" for name, cols in tables.items())
    return result or "No tables found."


@mcp.tool()
def explain(sql: str) -> str:
    """Run EXPLAIN ANALYZE on a SELECT query and return a structured report.

    The report contains timing, a list of detected issues, and a compact
    summary of the execution plan tree.

    Args:
        sql: A SQL query. Only SELECT and WITH statements are allowed.
    """

    try:
        raw = explain_query(sql, analyze=True, buffers=True)
        plan_json = raw["QUERY PLAN"]
        root = plan_json[0]
        plan_tree = root.get("Plan", {})

        report = analyze_plan(plan_json)
        report["plan_nodes"] = summarize_plan_node(plan_tree)

        return json.dumps(report, indent=2, ensure_ascii=False)
    except ValueError as e:
        return f"Validation error: {e}"
    except Exception as e:
        return f"Execution error: {e}"


def main() -> None:
    """Entry point for the `pg-explain-mcp` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
