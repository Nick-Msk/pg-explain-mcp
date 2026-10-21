# Utils — Book Loader & Full-Text Search

Helper scripts for downloading real book texts from Hugging Face and loading them into PostgreSQL for testing full-text search with GIN indexes.

Built and tested on PostgreSQL 18–19. Works on any PostgreSQL 12+ (needs `GENERATED ALWAYS AS ... STORED` and `websearch_to_tsquery`).

## Requirements

- Python 3.10+
- PostgreSQL 12+
- `uv` for environment management
- `datasets` and `psycopg[binary]` packages

Dependencies are declared in `utils/pyproject.toml`; install with:

```bash
cd utils
uv sync
```

---

## Schema

Two tables, fixed names:

```sql
book_header (id, title, author, tsv)
  tsv: setweight(title,  'A') ||
       setweight(author, 'B')

book_text   (book_id, chunk, text_chunk, tsv)
  PK: (book_id, chunk)
  FK: book_id → book_header(id) ON DELETE CASCADE
  tsv: to_tsvector(text_chunk)
```

Each book is split into chunks of up to 250 000 characters at word boundaries, because PostgreSQL's `tsvector` cannot exceed **1 MB**. Without chunking, any book longer than ~50 pages would fail to load with:

```
ERROR: string is too long for tsvector (… bytes, max 1048575 bytes)
```

`book_header.tsv` is weighted so header matches rank higher than text matches. It's also tiny (title + author only, never TOASTed), which makes header ranking essentially free.

---

## Quick start

```bash
cd utils
./load-def.sh 5000 --override --use-cache
```

This:

1. Connects to PostgreSQL (using `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD` or defaults).
2. Drops `book_header` and `book_text` if they exist.
3. Downloads 5000 books from `AdhyanshVerma/pg-en` (streaming; only fetches what's needed).
4. Splits each book into ≤250 000-character chunks.
5. `COPY`s both tables from CSVs.
6. Runs `VACUUM ANALYZE`.
7. Keeps the CSVs in `storage/` for fast reload.

Expected time on a modern laptop:

- **First run:** 10–20 minutes (mostly downloading).
- **Subsequent runs with `--use-cache`:** ~30 seconds (just COPY + VACUUM).

---

## CLI reference

### `load-def.sh <limit> [options]`

Bash wrapper. Positional `<limit>` is the number of books (0 is allowed with `--skip-load`). All flags are passed to `load_to_pg.py`.

| Flag | Description |
|------|-------------|
| `--override` | Drop `book_header` and `book_text` before loading |
| `--use-cache` | Reuse CSVs from `storage/` if present; keep newly downloaded CSVs afterwards |
| `--keep-csv` | Keep CSVs after download (usable by a later `--use-cache` run) |
| `--skip-load` | Skip download and COPY (process existing tables only) |
| `--skip-process` | Skip GIN indexes and VACUUM ANALYZE |
| `--with-gin` | Create GIN indexes on `book_header.tsv` and `book_text.tsv` |
| `--chunk-size N` | Characters per chunk (default: 250 000) |
| `--fts-config NAME` | Text search config (default: `english`; also `russian`, `simple`, …) |
| `--dataset NAME` | Hugging Face dataset (default: `AdhyanshVerma/pg-en`) |
| `--host`, `--port`, `--dbname`, `--user`, `--password` | PostgreSQL connection overrides |

### `load_to_pg.py`

The underlying Python script. Accepts the same flags as the wrapper, plus `--storage-dir PATH`. Useful if you want to drive it from another script.

### `find_expression.sql`

Creates three search functions. Apply once per database:

```bash
psql -d yourdb -f utils/find_expression.sql
```

---

## Caching

CSV files are stored in `utils/storage/` with deterministic names:

```
books_<limit>_chunk<chunk_size>_header.csv
books_<limit>_chunk<chunk_size>_text.csv
```

So:

```bash
./load-def.sh 5000 --override --use-cache   # first time: downloads and keeps
./load-def.sh 5000 --override --use-cache   # second time: instant reload
./load-def.sh 5000 --override --use-cache --with-gin   # add GIN, no download
```

Changing `--limit` or `--chunk-size` produces a different filename → cache miss → new download. That's intentional: the data really is different.

To free space:

```bash
rm -rf utils/storage/
```

---

## Search API

Three tools with different costs. Pick the right one for your use case.

### `find_expression(query, limit, fts_config, offset)` — fast

Plain GIN scan + join, returns one row per matching chunk.

```sql
SELECT * FROM find_expression('war peace');
SELECT * FROM find_expression('"to be or not to be"');
SELECT * FROM find_expression('love -war');
SELECT * FROM find_expression('hamlet OR macbeth');
```

Returns:

| Column | Description |
|--------|-------------|
| `book_id` | Header id |
| `chunk` | Matching chunk number |
| `title` | Book title |
| `author` | Author |

**Cost:** ~6–10 ms on 5000 books, no matter how many matches.

Backed by `websearch_to_tsquery()`, so it understands Google-like syntax: `"exact phrase"`, `word -exclude`, `a OR b`, and default `AND`.

### `get_snippet(book_id, chunk, query, fts_config)` — one highlighted fragment

```sql
SELECT get_snippet(book_id, chunk, 'war & peace')
FROM find_expression('war peace')
LIMIT 3;
```

Returns a text fragment with matched words wrapped in `<b>…</b>`. Works best when you only need a few snippets.

**Cost:** ~15 ms per call, because `ts_headline` must decompress and parse the whole `text_chunk` (up to 250 KB from TOAST).

So: 20 fast results + 3 snippets ≈ 6 + 45 = **51 ms**.

### `find_expression_ranked(query, limit, fts_config, header_weight)` — everything

One row per book, with snippets, list of matching chunks, and a relevance score.

```sql
SELECT * FROM find_expression_ranked('war peace', 10);
```

Returns:

| Column | Description |
|--------|-------------|
| `book_id`, `title`, `author` | Book metadata |
| `snippet` | Best fragment, matched words in `<b>…</b>` |
| `chunks` | `int[]` — all matching chunk numbers |
| `rank` | Weighted score (header × weight + chunk count) |

**Cost:** ~150–170 ms on 5000 books (10 snippets). Use `--header_weight` (default 2.0) to control how much title/author matches matter.

For UI-facing search: use `find_expression()` for the results list, then call `get_snippet()` lazily when the user expands a row. Use `find_expression_ranked()` only when you need everything at once.

### `get_chunks(book_id, chunks)` — reconstruct a book

```sql
SELECT * FROM get_chunks(42, '{0,2,5}'::int[]);
```

Returns `(chunk, text_chunk)` for the specified chunks. Useful when you want to show the full context around a match.

---

## Building GIN indexes

After loading, create indexes once:

```bash
./load-def.sh 0 --skip-load --with-gin
```

Or manually, if you want to tune parameters:

```sql
\timing on
CREATE INDEX idx_book_text_tsv   ON book_text   USING GIN (tsv);
CREATE INDEX idx_book_header_tsv ON book_header USING GIN (tsv);
```

Sizes on 5000 books (~5 GB of text):

```
idx_book_text_tsv    ~800 MB
idx_book_header_tsv  ~1 MB
```

The header index is tiny because `title || author` is at most a few hundred bytes per row. It can still be a huge win: `find_expression_ranked()` uses it to rank books by title/author relevance without ever touching the text chunks.

---

## Performance notes

### TOAST and why `pg_relation_size()` lies

Large `text_chunk` values are offloaded to TOAST storage. That means:

- `pg_relation_size('book_text')` shows ~100 KB (just TOAST pointers in the main heap).
- `pg_table_size('book_text')` shows the real size (~5 GB) including TOAST.

Both are correct. Use `pg_total_relation_size('book_text')` when you need the "all indexes + TOAST + heap" number.

### Why `ts_rank_cd` on `book_text.tsv` is slow

`ts_rank_cd` requires the full `tsvector`. On `book_text` that means reading ~10 TOAST pages per row, decompressing, and parsing. On 7895 matching chunks that's ~70 000 page reads → **250 ms**.

`find_expression_ranked()` avoids this by ranking only on `book_header.tsv` (tiny, never TOASTed) + a chunk-count tie-breaker. It never calls `ts_rank_cd` on `book_text.tsv`.

If you need true text-level ranking and can afford the cost, add it yourself:

```sql
SELECT book_id, MAX(ts_rank_cd(tsv, q)) AS r
FROM book_text, websearch_to_tsquery('english', 'war & peace') q
WHERE tsv @@ q
GROUP BY book_id
ORDER BY r DESC;
```

Budget ~300 ms.

### Chunk size

`--chunk-size` controls the trade-off:

- **250 000 (default):** safe margin below the 1 MB `tsvector` limit, even for pathological text (dense unique tokens). Reliable.
- **100 000:** smaller `ts_headline` payload per chunk (~5 ms instead of 15 ms), but more chunks and more join work.
- **500 000:** fewer chunks, but some books will hit the 1 MB limit and fail to load.

250 K is the sweet spot for a book corpus.

### Hugging Face rate limits

Unauthenticated requests to Hugging Face Hub are rate-limited. For large downloads (>10 000 books), register at huggingface.co and pass a token:

```bash
HF_TOKEN=hf_xxxxx ./load-def.sh 50000 --override --use-cache
```

---

## License

The scripts use third-party datasets from Hugging Face. Check the license of the specific dataset on its page before using it in production.


