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

    Args:
        table_name: Optional table name to filter by. If None, returns
                    indexes for all user tables.

    Returns:
        Each row contains: schema, table, index name, full definition,
        uniqueness, primary-key flag, and the list of indexed columns.
    """
    query = """
        SELECT
            ns.nspname                AS schema_name,
            tbl.relname               AS table_name,
            idx.relname               AS index_name,
            pg_get_indexdef(idx.oid)  AS index_def,
            i.indisunique             AS is_unique,
            i.indisprimary            AS is_primary,
            array_agg(att.attname ORDER BY att.attnum) AS columns
        FROM pg_index i
        JOIN pg_class idx ON idx.oid = i.indexrelid
        JOIN pg_class tbl ON tbl.oid = i.indrelid
        JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
        JOIN pg_attribute att
          ON att.attrelid = tbl.oid
         AND att.attnum = ANY(i.indkey)
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND tbl.relkind = 'r'
          AND (%(table_name)s IS NULL OR tbl.relname = %(table_name)s)
        GROUP BY ns.nspname, tbl.relname, idx.relname, idx.oid,
                 i.indisunique, i.indisprimary
        ORDER BY ns.nspname, tbl.relname, idx.relname
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, {"table_name": table_name})
            return cur.fetchall()

