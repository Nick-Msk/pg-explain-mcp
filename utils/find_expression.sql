-- find_expression.sql
--
-- Three tools, increasing cost:
--   1. find_expression()         — plain GIN + JOIN, ~6-10 ms
--   2. get_snippet()             — ts_headline for one (book_id, chunk), ~15 ms
--   3. find_expression_ranked()  — ranked + snippets, ~150-300 ms


-- ---------------------------------------------------------------------------
-- 1. find_expression — plain, fast. One row per matching chunk.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION find_expression(
    p_query         text,
    p_limit         int       DEFAULT 100,
    p_fts_config    regconfig DEFAULT 'english',
    p_offset        int       DEFAULT 0
)
RETURNS TABLE (
    book_id  int,
    chunk    int,
    title    text,
    author   text
)
LANGUAGE sql STABLE
AS $$
    SELECT
        bt.book_id,
        bt.chunk,
        COALESCE(bh.title, '<NO TITLE>') AS title,
        bh.author
    FROM book_text bt
    JOIN book_header bh ON bh.id = bt.book_id
    WHERE bt.tsv @@ websearch_to_tsquery(p_fts_config, p_query)
    ORDER BY bt.book_id, bt.chunk
    LIMIT p_limit
    OFFSET p_offset;
$$;

COMMENT ON FUNCTION find_expression(text, int, regconfig, int) IS
    'Plain fast search: one row per matching chunk. ~6-10ms on 5000 books. '
    'Use get_snippet() to fetch a highlighted fragment for a specific chunk.';


-- ---------------------------------------------------------------------------
-- 2. get_snippet — ts_headline for one (book_id, chunk). ~15ms per call.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION get_snippet(
    p_book_id       int,
    p_chunk         int,
    p_query         text,
    p_fts_config    regconfig DEFAULT 'english'
)
RETURNS text
LANGUAGE sql STABLE
AS $$
    SELECT ts_headline(
        p_fts_config,
        bt.text_chunk,
        websearch_to_tsquery(p_fts_config, p_query),
        'StartSel=<b>, StopSel=</b>, MaxWords=30, MinWords=15, ShortWord=3'
    )
    FROM book_text bt
    WHERE bt.book_id = p_book_id AND bt.chunk = p_chunk;
$$;

COMMENT ON FUNCTION get_snippet(int, int, text, regconfig) IS
    'Highlighted fragment from a specific chunk. ~15ms per call because '
    'ts_headline must decompress and parse the whole text_chunk from TOAST.';


-- ---------------------------------------------------------------------------
-- 3. find_expression_ranked — heavy: ranking + snippets. ~150-300ms.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION find_expression_ranked(
    p_query         text,
    p_limit         int       DEFAULT 10,
    p_fts_config    regconfig DEFAULT 'english',
    p_header_weight float     DEFAULT 2.0
)
RETURNS TABLE (
    book_id  int,
    title    text,
    author   text,
    snippet  text,
    chunks   int[],
    rank     real
)
LANGUAGE sql STABLE
AS $$
    WITH q AS (
        SELECT websearch_to_tsquery(p_fts_config, p_query) AS query
    ),
    text_hits AS (
        SELECT bt.book_id, bt.chunk
        FROM book_text bt, q
        WHERE bt.tsv @@ q.query
    ),
    grouped AS (
        SELECT book_id,
               array_agg(chunk ORDER BY chunk) AS chunks,
               count(*)::int AS n_chunks
        FROM text_hits
        GROUP BY book_id
    ),
    header_ranked AS (
        SELECT h.id AS book_id,
               h.title,
               h.author,
               ts_rank_cd(h.tsv, q.query, 32)::real AS hr
        FROM book_header h, q
        WHERE h.tsv @@ q.query
    ),
    candidates AS (
        SELECT
            COALESCE(h.book_id, g.book_id) AS book_id,
            COALESCE(h.title, '<NO TITLE>') AS title,
            COALESCE(h.author, '')          AS author,
            COALESCE(g.chunks, ARRAY[]::int[]) AS chunks,
            COALESCE(g.n_chunks, 0)         AS n_chunks,
            COALESCE(h.hr, 0)::real         AS hr
        FROM header_ranked h
        FULL OUTER JOIN grouped g ON h.book_id = g.book_id
    ),
    top_n AS (
        SELECT book_id, title, author, chunks,
               (hr * p_header_weight + LEAST(n_chunks, 10) * 0.1)::real AS rank
        FROM candidates
        ORDER BY rank DESC
        LIMIT p_limit
    )
    SELECT
        t.book_id,
        t.title,
        t.author,
        CASE
            WHEN t.chunks <> ARRAY[]::int[] THEN
                (SELECT ts_headline(
                    p_fts_config,
                    bt.text_chunk,
                    q.query,
                    'StartSel=<b>, StopSel=</b>, MaxWords=30, MinWords=15, ShortWord=3'
                 )
                 FROM book_text bt
                 WHERE bt.book_id = t.book_id AND bt.chunk = t.chunks[1])
            ELSE
                (SELECT ts_headline(
                    p_fts_config,
                    coalesce(t.title, '') || ' — ' || coalesce(t.author, ''),
                    q.query,
                    'StartSel=<b>, StopSel=</b>'
                 ))
        END,
        t.chunks,
        t.rank
    FROM top_n t, q
    ORDER BY t.rank DESC;
$$;

COMMENT ON FUNCTION find_expression_ranked(text, int, regconfig, float) IS
    'Ranked search with snippets. Uses cheap header rank + matched-chunk count '
    'to pick top candidates, then ts_headline for those. ~150-300ms. '
    'Prefer find_expression() + get_snippet() for interactive use.';


-- ---------------------------------------------------------------------------
-- get_chunks — fetch full text for (book_id, chapter, chunks[]).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION get_chunks(p_book_id int, p_chunks int[])
RETURNS TABLE (chunk int, chapter text, text_chunk text)
LANGUAGE sql STABLE
AS $$
    SELECT chunk, chapter, text_chunk
    FROM book_text
    WHERE book_id = p_book_id AND chunk = ANY(p_chunks)
    ORDER BY chunk;
$$;

-- ---------------------------------------------------------------------------
-- find_books — one row per book. For result lists in a UI/agent.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION find_books(
    p_query         text,
    p_limit         int       DEFAULT 20,
    p_fts_config    regconfig DEFAULT 'english',
    p_offset        int       DEFAULT 0
)
RETURNS TABLE (
    book_id  int,
    title    text,
    author   text,
    n_chunks int,
    chunks   int[]
)
LANGUAGE sql STABLE
AS $$
    SELECT
        bt.book_id,
        COALESCE(bh.title, '<NO TITLE>') AS title,
        bh.author,
        COUNT(*)::int AS n_chunks,
        array_agg(bt.chunk ORDER BY bt.chunk) AS chunks
    FROM book_text bt
    JOIN book_header bh ON bh.id = bt.book_id
    WHERE bt.tsv @@ websearch_to_tsquery(p_fts_config, p_query)
    GROUP BY bt.book_id, bh.title, bh.author
    ORDER BY n_chunks DESC, bt.book_id
    LIMIT p_limit
    OFFSET p_offset;
$$;

COMMENT ON FUNCTION find_books(text, int, regconfig, int) IS
    'One row per matching book: title, author, number of matching chunks, '
    'and array of matching chunk numbers. Ordered by relevance (most mentions first).';

