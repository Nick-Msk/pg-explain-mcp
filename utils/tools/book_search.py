"""
MCP tools for full-text book search.

Wraps the SQL functions from utils/find_expression.sql:
  find_expression()         -> search_books()
  get_snippet()             -> get_book_snippet()
  find_expression_ranked()  -> rank_books()
  get_chunks()              -> read_book_chunks()

Precondition: the book corpus is already loaded into the target database
(use utils/load-def.sh for that). These tools only read.

Connection:
  By default reads PGHOST / PGPORT / PGDATABASE / PGUSER / PGPASSWORD.
  To override at runtime (e.g. in a server bootstrap):

      from utils.tools import set_connection, register_tools
      set_connection(host="db.local", dbname="books", user="reader")

      mcp = FastMCP("book-search")
      register_tools(mcp)
"""

import os
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row


# ---------------------------------------------------------------------------
# Connection configuration
# ---------------------------------------------------------------------------

_conn_kwargs: dict[str, Any] = {}


def set_connection(
    host: str | None = None,
    port: int | None = None,
    dbname: str | None = None,
    user: str | None = None,
    password: str | None = None,
    dsn: str | None = None,
) -> None:
    """Override default PostgreSQL connection settings.

    Any argument left as None is filled from the corresponding PG* env var
    (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD) at connect time.

    Call once at server startup. Per-request connections are short-lived
    and read-only.

    Examples:
        set_connection(dsn="postgresql://reader@db.local/books")
        set_connection(host="db.local", dbname="books", user="reader")
    """
    global _conn_kwargs
    _conn_kwargs = {}
    if dsn is not None:
        _conn_kwargs["conninfo"] = dsn
        return
    if host     is not None: _conn_kwargs["host"]     = host
    if port     is not None: _conn_kwargs["port"]     = port
    if dbname   is not None: _conn_kwargs["dbname"]   = dbname
    if user     is not None: _conn_kwargs["user"]     = user
    if password is not None: _conn_kwargs["password"] = password


def _connect() -> psycopg.Connection:
    if _conn_kwargs:
        conn = psycopg.connect(**_conn_kwargs)
    else:
        conn = psycopg.connect(
            host=os.environ.get("PGHOST", "127.0.0.1"),
            port=int(os.environ.get("PGPORT", 5432)),
            dbname=os.environ.get("PGDATABASE", "postgres"),
            user=os.environ.get("PGUSER", os.environ.get("USER")),
            password=os.environ.get("PGPASSWORD"),
        )
    conn.read_only = True
    return conn


@contextmanager
def _cursor():
    conn = _connect()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            yield cur
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 1: search_books (fast, one row per matching chunk)
# ---------------------------------------------------------------------------

def search_books(
    query: str,
    limit: int = 20,
    offset: int = 0,
    unique_books: bool = False,
) -> list[dict[str, Any]]:
    """Fast full-text search (~10ms).

    Two modes:

    - unique_books=False (default): one row per matching CHUNK.
      Fields: book_id, chunk, title, author.
      A book that mentions "war" on 20 pages appears 20 times.
      Use when you want to find every mention, or to grab many snippets.

    - unique_books=True: one row per BOOK.
      Fields: book_id, title, author, n_chunks, chunks.
      `n_chunks` = how many chunks matched (relevance indicator).
      `chunks` = array of matching chunk numbers.
      Ordered by n_chunks DESC — most-mentioned books first.
      Use when showing a result list to the user.

    Query syntax (Google-like):
      war peace               -> both words (AND)
      "to be or not to be"    -> exact phrase
      love -war               -> love but not war
      hamlet OR macbeth       -> either

    Args:
        query: Natural-language search query.
        limit: Max rows (1-500, default 20).
        offset: Pagination offset (default 0).
        unique_books: One row per book instead of per chunk.

    Returns:
        See description above.
    """
    limit = max(1, min(limit, 500))
    with _cursor() as cur:
        if unique_books:
            cur.execute(
                "SELECT * FROM find_books(%s, %s, 'english', %s)",
                (query, limit, offset),
            )
        else:
            cur.execute(
                "SELECT * FROM find_expression(%s, %s, 'english', %s)",
                (query, limit, offset),
            )
        return cur.fetchall()

# ---------------------------------------------------------------------------
# Tool 2: get_book_snippet (one highlighted fragment)
# ---------------------------------------------------------------------------

def get_book_snippet(book_id: int, chunk: int, query: str) -> str | None:
    """Return a highlighted text fragment from a specific chunk (~15ms).

    Use this when the user says "show me what's in that book" and you have
    a (book_id, chunk) pair from search_books() or rank_books().

    Each call returns ONE snippet — up to ~30 words with matched terms
    wrapped in <b>…</b>. For multiple fragments from the same book, call
    this once per chunk.

    Do NOT use to find matches — this requires a known (book_id, chunk).
    The query parameter should be the SAME query you used in search_books()
    so the correct terms get highlighted.

    Args:
        book_id: Book id from a previous search result.
        chunk:   Chunk number from a previous search result.
        query:   Same query that produced the match.

    Returns:
        Highlighted text fragment, or None if the chunk doesn't exist.
    """
    with _cursor() as cur:
        cur.execute(
            "SELECT get_snippet(%s, %s, %s, 'english') AS snippet",
            (book_id, chunk, query),
        )
        row = cur.fetchone()
        return row["snippet"] if row else None


# ---------------------------------------------------------------------------
# Tool 3: rank_books (ranked + snippets in one shot)
# ---------------------------------------------------------------------------

def rank_books(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Ranked search WITH snippets in one call (~150ms). One row per book.

    Use ONLY when the user wants "the best books about X" or "top matches
    with previews" — i.e. when they explicitly need relevance ordering or
    ready-to-display snippets.

    For plain "which books mention X" — use search_books() instead. It's
    ~15x faster and gives you (book_id, chunk) pairs that you can enrich
    with get_book_snippet() on demand.

    Ranking formula: header_relevance × 2.0 + min(chunk_count, 10) × 0.1.
    Header matches (title/author) count double. Full-text ts_rank is NOT
    used (would cost 250ms extra on TOASTed text chunks).

    Args:
        query: Natural-language search query.
        limit: Number of books to return (1-50, default 10).

    Returns:
        [{"book_id": int, "title": str, "author": str,
          "snippet": str, "chunks": [int], "rank": float}, ...]
    """
    limit = max(1, min(limit, 50))
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM find_expression_ranked(%s, %s, 'english', 2.0)",
            (query, limit),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Tool 4: read_book_chunks (full text of specific chunks)
# ---------------------------------------------------------------------------

def read_book_chunks(
    book_id: int,
    chunks: list[int],
    context: int = 1,
    max_centers: int = 3,
    max_chars_per_chunk: int = 2500
) -> list[dict[str, Any]]:
    """Fetch contextual fragments around specific matching chunks from one book.

    Use this when the user wants a summary or compilation based on the text.
    Give it the chunk numbers from search_books() or rank_books(). The tool
    will pull those chunks PLUS the surrounding `context` chunks (to preserve
    narrative flow), but strictly limits the output size.

    Always cite the results in your final answer. Format the citation as:
      [book_id=3941, chapter="The Forged Coupon", chunk=12]
    If the chapter is empty or null, use just [book_id=..., chunk=...].

    Args:
        book_id: Book id.
        chunks: List of target chunk numbers (e.g. [5, 78, 145]).
        context: Neighboring chunks before/after (default 1, max 2).
        max_centers: Max target chunks to process (default 3).
        max_chars_per_chunk: Truncate each to this many chars (default 2500).

    Returns:
        [{"chunk": int, "chapter": str, "text_chunk": str}, ...]
    """
    if not chunks:
        return []

    chunks = sorted(list(set(chunks)))[:max_centers]
    context = max(0, min(context, 2))
    limit_chars = max(500, min(max_chars_per_chunk, 5000))

    all_chunks = set()
    for c in chunks:
        for i in range(c - context, c + context + 1):
            if i >= 0:
                all_chunks.add(i)

    all_chunks = sorted(list(all_chunks))

    with _cursor() as cur:
        cur.execute(
            """
            SELECT chunk, chapter, LEFT(text_chunk, %s) AS text_chunk
            FROM get_chunks(%s, %s::int[])
            """,
            (limit_chars, book_id, all_chunks),
        )
        return cur.fetchall()

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_tools(mcp) -> None:
    """Register all 4 book-search tools on an MCP server instance.

    Usage:
        from fastmcp import FastMCP
        from utils.tools import set_connection, register_tools

        set_connection(host="db.local", dbname="books", user="reader")

        mcp = FastMCP("book-search")
        register_tools(mcp)
        mcp.run()
    """
    mcp.tool()(search_books)
    mcp.tool()(get_book_snippet)
    mcp.tool()(rank_books)
    mcp.tool()(read_book_chunks)

