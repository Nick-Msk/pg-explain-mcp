"""MCP server for PostgreSQL query plan analysis."""

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from pg_explain_mcp.analyzer import (
    analyze_plan,
    filtered_parse_plan,
    format_plan_tree,
    summarize_plan_node,
)
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
from pg_explain_mcp.db import (
    explain_query,
    get_indexes,
    get_params,
    get_relation_info,
    get_relation_stat_info,
    get_schema,
)

_registry = CheckRegistry(DEFAULT_DB)

mcp = FastMCP("pg-explain")

def _extract_index_args(index_def: str) -> str:
    """Extract the argument list from a CREATE INDEX definition.

    Scans for the first top-level ``(`` and returns everything up to
    its matching ``)``. Handles nested parens, casts, and multi-column
    indexes:

        ... USING btree (lower(email))     -> "lower(email)"
        ... USING btree (id, upper(val))   -> "id, upper(val)"
    """
    depth = 0
    start = -1
    for i, c in enumerate(index_def):
        if c == "(":
            if depth == 0:
                start = i
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0 and start >= 0:
                return index_def[start + 1 : i]
    return index_def


def _format_indexes(rows: list[dict[str, Any]]) -> str:
    """Format index rows into a human-readable string."""
    if not rows:
        return "No indexes found."

    lines = []
    for row in rows:
        if row["is_primary"]:
            kind = "PRIMARY KEY"
        elif row["is_functional"]:
            kind = "FUNCTIONAL"
        elif row["is_unique"]:
            kind = "UNIQUE"
        else:
            kind = "INDEX"

        if row["is_functional"]:
            cols_str = _extract_index_args(row["index_def"])
        else:
            plain = row.get("plain_columns") or []
            cols_str = ", ".join(plain)

        lines.append(
            f"{row['schema_name']}.{row['table_name']} → "
            f"{row['index_name']} [{kind}] ({cols_str})"
        )
    return "\n".join(lines)

def _human_bytes(n: int | None) -> str:
    if not n:
        return "0 B"
    value = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def _format_timestamp(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "—"


def _format_relation_info(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No relations found."
    lines: list[str] = []
    for r in rows:
        lines.append(f"{r['schema_name']}.{r['relation_name']} [{r['relkind']}]")
        lines.append(f"  rows (est):     {r['estimated_rows']:,}")
        lines.append(f"  pages:          {r['pages']:,}")
        lines.append(f"  columns:        {r['column_count']}")
        lines.append(f"  indexes:        {r['index_count']}")
        lines.append(f"  heap size:      {_human_bytes(r['heap_size_bytes'])}")
        lines.append(f"  index size:     {_human_bytes(r['index_size_bytes'])}")
        lines.append(f"  total size:     {_human_bytes(r['total_size_bytes'])}")
        lines.append(f"  owner:          {r['owner']}")
        lines.append(f"  access method:  {r['access_method'] or '—'}")
        lines.append(f"  persistence:    {r['persistence']}")
        if r["tablespace"]:
            lines.append(f"  tablespace:     {r['tablespace']}")
        if r["comment"]:
            lines.append(f"  comment:        {r['comment']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_relation_stats(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No statistics found."
    lines: list[str] = []
    for r in rows:
        lines.append(f"{r['schema_name']}.{r['relation_name']}")
        lines.append(f"  rows (est):         {r['estimated_rows']:,}")
        lines.append(
            f"  pages:              {r['pages']:,} "
            f"({r['all_visible_pages']:,} all-visible)"
        )
        lines.append(
            f"  live/dead tuples:   {r['n_live_tup']:,} / {r['n_dead_tup']:,}"
        )
        lines.append(f"  mod since analyze:  {r['n_mod_since_analyze']:,}")
        lines.append(f"  ins since vacuum:   {r['n_ins_since_vacuum']:,}")
        lines.append(
            f"  seq scans:          {r['seq_scan']:,} "
            f"(rows read {r['seq_tup_read']:,})"
        )
        lines.append(
            f"  idx scans:          {r['idx_scan']:,} "
            f"(rows fetched {r['idx_tup_fetch']:,})"
        )
        lines.append(
            f"  tuple changes:      ins={r['n_tup_ins']:,} "
            f"upd={r['n_tup_upd']:,} del={r['n_tup_del']:,} "
            f"hot={r['n_tup_hot_upd']:,}"
        )
        lines.append(f"  last vacuum:        {_format_timestamp(r['last_vacuum'])}")
        lines.append(f"  last autovacuum:    {_format_timestamp(r['last_autovacuum'])}")
        lines.append(f"  last analyze:       {_format_timestamp(r['last_analyze'])}")
        lines.append(f"  last autoanalyze:   {_format_timestamp(r['last_autoanalyze'])}")
        lines.append(
            f"  counters:           vacuum={r['vacuum_count']} "
            f"autovacuum={r['autovacuum_count']} "
            f"analyze={r['analyze_count']} "
            f"autoanalyze={r['autoanalyze_count']}"
        )
        lines.append("")
    return "\n".join(lines).rstrip()

def _collect_relation_names(node: dict[str, Any], out: set[str]) -> None:
    rel = node.get("Relation Name")
    if rel:
        out.add(rel)
    for child in node.get("Plans", []):
        _collect_relation_names(child, out)

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
def list_relation_info(relation: str | None = None) -> str:
    """Return metadata for user relations from pg_class.

    Covers tables, partitioned tables, matviews, views, foreign
    tables. Sizes come from pg_relation_size / pg_indexes_size /
    pg_total_relation_size.

    Args:
        relation: Optional filter — ``table`` or ``schema.table``.
            If omitted, returns every user relation.
    """
    try:
        rows = get_relation_info(relation)
        return _format_relation_info(rows)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def list_relation_stats(relation: str | None = None) -> str:
    """Return runtime statistics for user tables.

    Combines pg_stat_user_tables (scan counters, tuple changes,
    vacuum/analyze timestamps) with pg_class row/page estimates and
    the visibility map size.

    Args:
        relation: Optional filter — ``table`` or ``schema.table``.
    """
    try:
        rows = get_relation_stat_info(relation)
        return _format_relation_stats(rows)
    except Exception as e:
        return f"Error: {e}"

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

        relation_names: set[str] = set()
        _collect_relation_names(plan_tree, relation_names)
        relation_indexes = {
            rel: get_indexes(rel) for rel in relation_names
        }

        checks = _registry.load(relation_indexes=relation_indexes)
        fields = _registry.load_fields()

        report = analyze_plan(plan_json, checks=checks)
        report["plan_nodes"] = summarize_plan_node(plan_tree, fields)

        return json.dumps(report, indent=2, ensure_ascii=False)
    except ValueError as e:
        return f"Validation error: {e}"
    except Exception as e:
        return f"Execution error: {e}"

@mcp.tool()
def explain_tree(sql: str) -> str:
    """Run EXPLAIN ANALYZE and return the full plan as a navigable tree.

    Unlike ``explain``, which returns a curated ``plan_nodes`` list,
    this tool returns **every** field from the JSON plan, plus four
    structural fields for navigation:

    - ``id``           — unique node id (pre-order)
    - ``parent_id``    — id of the parent, ``null`` for the root
    - ``depth``        — 0 for the root, +1 per level
    - ``children_ids`` — ids of direct children

    Nodes are ordered pre-order: ``nodes[0]`` is always the root.
    Navigate top-down via ``children_ids``, bottom-up via ``parent_id``.

    Args:
        sql: A SQL query. Only SELECT and WITH statements are allowed.
    """
    try:
        raw = explain_query(sql, analyze=True, buffers=True)
        plan_json = raw["QUERY PLAN"]
        nodes = filtered_parse_plan(plan_json)
        return json.dumps(nodes, indent=2, ensure_ascii=False)
    except ValueError as e:
        return f"Validation error: {e}"
    except Exception as e:
        return f"Execution error: {e}"


@mcp.tool()
def explain_tree_text(sql: str) -> str:
    """Run EXPLAIN ANALYZE and return the plan tree as indented text.

    Same data as ``explain_tree``, rendered for human reading. Each
    node starts with its ``Node Type``; its fields follow, indented by
    two spaces; children are indented two more spaces per depth.

    Args:
        sql: A SQL query. Only SELECT and WITH statements are allowed.
    """
    try:
        raw = explain_query(sql, analyze=True, buffers=True)
        plan_json = raw["QUERY PLAN"]
        nodes = filtered_parse_plan(plan_json)
        return format_plan_tree(nodes)
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
