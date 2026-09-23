"""MCP server for PostgreSQL query plan analysis."""

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from pg_explain_mcp.analyzer import analyze_plan, summarize_plan_node
from pg_explain_mcp.config import (
    DEFAULT_DB,
    CheckRegistry,
)
from pg_explain_mcp.config import (
    reset_param as reset_param_impl,
)
from pg_explain_mcp.config import (
    set_param as set_param_impl,
)
from pg_explain_mcp.config import (
    show_params as show_params_impl,
)
from pg_explain_mcp.db import explain_query, get_indexes, get_params, get_schema

_registry = CheckRegistry(DEFAULT_DB)

mcp = FastMCP("pg-explain")

def _format_indexes(rows: list[dict[str, any]]) -> str:
    """Format index rows into a human-readable string."""
    if not rows:
        return "No indexes found."

    lines = []
    for row in rows:
        cols = ", ".join(row["columns"])
        if row["is_primary"]:
            kind = "PRIMARY KEY"
        elif row["is_unique"]:
            kind = "UNIQUE"
        else:
            kind = "INDEX"
        lines.append(
            f"{row['schema_name']}.{row['table_name']} → {row['index_name']} [{kind}] ({cols})"
        )
    return "\n".join(lines)


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
def list_indexes(table_name: str | None = None) -> str:
    """Return a list of indexes for user tables.

    Args:
        table_name: Optional table name to filter by. If omitted,
                    returns indexes for all user tables.
    """
    rows = get_indexes(table_name)
    return _format_indexes(rows)

def _format_params(rows: list[dict[str, any]]) -> str:
    """Format parameter rows into a human-readable string."""
    if not rows:
        return "No parameters found."

    lines = []
    for row in rows:
        unit = row.get("unit") or ""
        setting = row["setting"]
        value = f"{setting} {unit}".strip() if unit else setting
        lines.append(f"{row['name']} = {value} ({row['source']})")
    return "\n".join(lines)


@mcp.tool()
def list_parameters(names: str | None = None) -> str:
    """Return PostgreSQL parameters relevant to plan analysis.

    Args:
        names: Optional comma-separated list of parameter names. If omitted,
            returns a curated set: work_mem, hash_mem_multiplier,
            shared_buffers, effective_cache_size, random_page_cost,
            seq_page_cost, and parallel worker limits.
    """
    if names:
        parsed = tuple(n.strip() for n in names.split(",") if n.strip())
    else:
        parsed = None
    rows = get_params(parsed)
    return _format_params(rows)

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

        checks = _registry.load()   # fresh on every call
        fields = _registry.load_fields() # fresh on every call

        report = analyze_plan(plan_json, checks=checks)
        report["plan_nodes"] = summarize_plan_node(plan_tree, fields)

        return json.dumps(report, indent=2, ensure_ascii=False)
    except ValueError as e:
        return f"Validation error: {e}"
    except Exception as e:
        return f"Execution error: {e}"

def _format_params_table(rows: list[dict[str, Any]]) -> str:
    """Format check params into an aligned text table.

    A leading `*` marks params whose current value differs from the
    default.
    """
    if not rows:
        return "No params found."

    lines = []
    for r in rows:
        marker = "*" if r["changed"] else " "
        line = f"{marker} {r['checker']}.{r['param']} = {r['value']}"
        if r["changed"]:
            line += f"  (default: {r['default_value']})"
        lines.append(line)
    return "\n".join(lines)

@mcp.tool()
def show_params(checker: str | None = None) -> str:
    """Show check parameters for the configured target database.

    Args:
        checker: Optional check name. If omitted, returns params for
                 all checks.
    """
    try:
        rows = show_params_impl(checker, database=_registry.target)
        return _format_params_table(rows)
    except KeyError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"

@mcp.tool()
def set_checker_value(
    checker: str,
    param: str,
    value: str | int | float,
) -> str:
    """Set a check parameter's current value.

    Args:
        checker: Check name (e.g. ``SeqScanCheck``).
        param:   Parameter name (e.g. ``threshold_rows``).
        value:   New value. Accepts string, int, or float; coerced to
                 string internally before validation.
    """
    try:
        result = set_param_impl(
            checker, param, str(value), database=_registry.target
        )
        return (
            f"{result['checker']}.{result['param']}: "
            f"{result['old']} → {result['new']}"
        )
    except (KeyError, ValueError) as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"

@mcp.tool()
def reset_checker_value(checker: str, param: str | None = None) -> str:
    """Reset a check parameter (or all params of a check) to defaults.

    Args:
        checker: Check name.
        param:   Optional parameter name. If omitted, resets every
                 param of the check.
    """
    try:
        changes = reset_param_impl(
            checker, param, database=_registry.target
        )
        if not changes:
            return "Already at defaults — nothing to reset."
        return "\n".join(
            f"{c['checker']}.{c['param']}: {c['old']} → {c['new']}"
            for c in changes
        )
    except KeyError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"

def main() -> None:
    """Entry point for the `pg-explain-mcp` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
