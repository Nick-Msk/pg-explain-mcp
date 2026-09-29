"""PostgreSQL connection layer: safe, read-only access."""

import os
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _get_dsn() -> str:
    """Build a DSN string from environment variables."""
    host = os.getenv("PG_HOST", "localhost")
    port = os.getenv("PG_PORT", "5432")
    user = os.getenv("PG_USER", "postgres")
    password = os.getenv("PG_PASSWORD", "")
    database = os.getenv("PG_DATABASE", "postgres")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


@contextmanager
def get_connection():
    """Context manager for a read-only database connection.

    Forces READ ONLY at the transaction level to prevent accidental
    data modifications, even if a malicious or buggy query slips through.
    """
    conn = psycopg.connect(_get_dsn(), row_factory=dict_row)
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        yield conn
    finally:
        conn.close()


def get_schema() -> list[dict[str, Any]]:
    """Return the list of user tables and their columns."""
    query = """
        SELECT
            t.table_schema,
            t.table_name,
            c.column_name,
            c.data_type,
            c.is_nullable
        FROM information_schema.tables t
        JOIN information_schema.columns c
          ON c.table_schema = t.table_schema
         AND c.table_name = t.table_name
        WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY t.table_schema, t.table_name, c.ordinal_position
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            return cur.fetchall()


def explain_query(sql: str, analyze: bool = True, buffers: bool = True) -> dict[str, Any]:
    """Run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) for a SELECT query.

    Only SELECT and WITH statements are allowed.
    Returns the parsed JSON execution plan.
    """
    normalized = sql.strip().lower()
    if not (normalized.startswith("select") or normalized.startswith("with")):
        raise ValueError("Only SELECT and WITH statements are allowed")

    options = ["FORMAT JSON"]
    if analyze:
        options.append("ANALYZE")
    if buffers:
        options.append("BUFFERS")

    explain_sql = f"EXPLAIN ({', '.join(options)}) {sql}"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(explain_sql)
            result = cur.fetchone()
            return result

def get_indexes(table_name: str | None = None) -> list[dict[str, Any]]:
    """Return a list of indexes for user tables.

    Each row contains: schema, table, index name, full definition,
    uniqueness, primary-key flag, whether the index is functional,
    the leading attribute number (0 = expression), and the list of
    plain column names covered by the index.
    """
    query = """
        SELECT
            ns.nspname               AS schema_name,
            tbl.relname              AS table_name,
            idx.relname              AS index_name,
            pg_get_indexdef(idx.oid) AS index_def,
            i.indisunique            AS is_unique,
            i.indisprimary           AS is_primary,
            (i.indexprs IS NOT NULL) AS is_functional,
            i.indkey[0]              AS leading_attnum,
            coalesce(
                (
                    SELECT array_agg(a.attname ORDER BY ord.n)
                    FROM unnest(i.indkey) WITH ORDINALITY AS ord(attnum, n)
                    LEFT JOIN pg_attribute a
                        ON a.attrelid = tbl.oid
                       AND a.attnum   = ord.attnum
                    WHERE ord.attnum > 0
                ),
                ARRAY[]::name[]
            ) AS plain_columns
        FROM pg_index i
        JOIN pg_class idx     ON idx.oid = i.indexrelid
        JOIN pg_class tbl     ON tbl.oid = i.indrelid
        JOIN pg_namespace ns  ON ns.oid  = tbl.relnamespace
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND tbl.relkind = 'r'
          AND (%(table_name)s::text IS NULL OR tbl.relname = %(table_name)s::text)
        ORDER BY ns.nspname, tbl.relname, idx.relname
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {"table_name": table_name})
            return cur.fetchall()

# Parameters that are relevant when interpreting an execution plan.
# Users can request additional names by passing an explicit list.
ANALYSIS_PARAMS: tuple[str, ...] = (
    "work_mem",
    "hash_mem_multiplier",
    "shared_buffers",
    "effective_cache_size",
    "random_page_cost",
    "seq_page_cost",
    "max_parallel_workers_per_gather",
    "max_parallel_workers",
    "jit",
)


def get_params(names: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Return PostgreSQL runtime parameters relevant to plan analysis.

    Args:
        names: Parameter names to fetch. If None, uses ``ANALYSIS_PARAMS``.

    Returns:
        One dict per parameter with keys: ``name``, ``setting``, ``unit``,
        ``source``, ``short_desc``.
    """
    if names is None:
        names = ANALYSIS_PARAMS

    query = """
        select name, setting, unit, source, short_desc
        from pg_settings
        where name = any(%(names)s)
        order by name
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {"names": list(names)})
            return cur.fetchall()

# ---------------------------------------------------------------------------
# Relation metadata and statistics
# ---------------------------------------------------------------------------


def _relation_filter(
    relation: str | None,
    name_col: str = "c.relname",
    schema_col: str = "ns.nspname",
) -> tuple[str, dict[str, Any]]:
    """Build a WHERE fragment and params from a relation filter.

    Accepts either ``table`` or ``schema.table``. Returns
    ``("", {})`` if ``relation`` is None — the caller must decide how
    to combine the fragment with its own WHERE clause.
    """
    if not relation:
        return "", {}
    if "." in relation:
        schema, _, name = relation.partition(".")
        return (
            f"{schema_col} = %(schema)s AND {name_col} = %(name)s",
            {"schema": schema, "name": name},
        )
    return f"{name_col} = %(name)s", {"name": relation}


def get_relation_info(relation: str | None = None) -> list[dict[str, Any]]:
    """Return metadata for user relations from ``pg_class``.

    Covers tables, partitioned tables, matviews, views, and foreign
    tables. Size figures come from ``pg_relation_size`` /
    ``pg_indexes_size`` / ``pg_total_relation_size``.

    Args:
        relation: Optional filter — ``table`` or ``schema.table``.
            If omitted, returns every user relation.

    Returns:
        One dict per relation, ordered by (schema, name).
    """
    where = [
        "ns.nspname NOT IN ('pg_catalog', 'information_schema')",
        "c.relkind IN ('r', 'p', 'm', 'v', 'f')",
    ]
    extra, params = _relation_filter(relation)
    if extra:
        where.append(extra)

    query = f"""
        SELECT
            ns.nspname                              AS schema_name,
            c.relname                               AS relation_name,
            c.relkind                               AS relkind,
            c.relpersistence                        AS persistence,
            am.amname                               AS access_method,
            pg_get_userbyid(c.relowner)             AS owner,
            c.reltuples::bigint                     AS estimated_rows,
            c.relpages                              AS pages,
            pg_relation_size(c.oid)                 AS heap_size_bytes,
            pg_indexes_size(c.oid)                  AS index_size_bytes,
            pg_total_relation_size(c.oid)           AS total_size_bytes,
            (SELECT count(*)
             FROM pg_attribute a
             WHERE a.attrelid = c.oid
               AND a.attnum > 0
               AND NOT a.attisdropped)              AS column_count,
            (SELECT count(*)
             FROM pg_index i
             WHERE i.indrelid = c.oid)              AS index_count,
            ts.spcname                              AS tablespace,
            obj_description(c.oid, 'pg_class')      AS comment
        FROM pg_class c
        JOIN pg_namespace ns ON ns.oid = c.relnamespace
        LEFT JOIN pg_am am ON am.oid = c.relam
        LEFT JOIN pg_tablespace ts ON ts.oid = c.reltablespace
        WHERE {' AND '.join(where)}
        ORDER BY ns.nspname, c.relname
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()


def get_relation_stat_info(
    relation: str | None = None,
) -> list[dict[str, Any]]:
    """Return runtime statistics for user tables.

    Combines ``pg_stat_user_tables`` (scan counters, tuple changes,
    vacuum/analyze timestamps) with ``pg_class`` (``reltuples``,
    ``relpages``, ``relallvisible``).

    Args:
        relation: Optional filter — ``table`` or ``schema.table``.

    Returns:
        One dict per table, ordered by (schema, name).
    """
    where: list[str] = []
    extra, params = _relation_filter(
        relation,
        name_col="s.relname",
        schema_col="s.schemaname",
    )
    if extra:
        where.append(extra)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    query = f"""
        SELECT
            s.schemaname                            AS schema_name,
            s.relname                               AS relation_name,
            c.reltuples::bigint                     AS estimated_rows,
            c.relpages                              AS pages,
            c.relallvisible                         AS all_visible_pages,
            s.seq_scan,
            s.seq_tup_read,
            s.idx_scan,
            s.idx_tup_fetch,
            s.n_tup_ins,
            s.n_tup_upd,
            s.n_tup_del,
            s.n_tup_hot_upd,
            s.n_live_tup,
            s.n_dead_tup,
            s.n_mod_since_analyze,
            s.n_ins_since_vacuum,
            s.last_vacuum,
            s.last_autovacuum,
            s.last_analyze,
            s.last_autoanalyze,
            s.vacuum_count,
            s.autovacuum_count,
            s.analyze_count,
            s.autoanalyze_count
        FROM pg_stat_user_tables s
        JOIN pg_class c ON c.oid = s.relid
        {where_sql}
        ORDER BY s.schemaname, s.relname
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

