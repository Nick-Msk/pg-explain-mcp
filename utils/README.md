# Utils

A collection of helper scripts for test data preparation and PostgreSQL operations.

## `download_text.py`

A script to download real-world text from [Hugging Face](https://huggingface.co/) datasets and save it as CSV. Its primary purpose is to quickly obtain a large volume of real text for testing full-text search and GIN indexes in PostgreSQL.

### What it does

- Connects to a dataset in **streaming mode** — it does not download the entire dataset to disk, but reads only the number of records you specify.
- Saves the result as a CSV with `title`, `author`, and `text` columns.
- Cleans newlines in the text so the CSV reads correctly via `COPY` / `\copy`.
- Force-exits the process after writing to prevent background Hugging Face threads from blocking termination.

### Requirements

- Python 3.10+
- The `datasets` package (installed via `uv`)

```bash
uv pip install datasets
```

### Usage

```bash
uv run python download_text.py [OPTIONS]
```

#### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `-l`, `--limit` | `1000` | Number of records to download |
| `-o`, `--output` | `books.csv` | Output CSV filename |
| `-d`, `--dataset` | `AdhyanshVerma/pg-en` | Hugging Face dataset name |

#### Examples

```bash
# Download 9 books into books_9.csv
uv run python download_text.py --limit 9 --output books_9.csv

# Download 1000 books into books_1000.csv
uv run python download_text.py --limit 1000 --output books_1000.csv

# Use a different dataset
uv run python download_text.py -d "some-other/dataset" -l 500 -o other.csv
```

### Importing into PostgreSQL

Once the CSV is ready, create the table and load the data. **Important:** if PostgreSQL is not running locally, use `\copy` (a client-side psql command) instead of `COPY` (a server-side command). Otherwise, the server will look for the file on its own filesystem, not yours.

#### Step 1. Create the table

```sql
CREATE TABLE books (
    id SERIAL PRIMARY KEY,
    title TEXT,
    author TEXT,
    text TEXT
);
```

#### Step 2. Load the data

From psql, in the directory where the CSV is located:

```sql
\copy books(title, author, text) FROM 'books_9.csv' WITH (FORMAT csv, HEADER true)
```

Or with an absolute path:

```sql
\copy books(title, author, text) FROM '/<path>/books_9.csv' WITH (FORMAT csv, HEADER true)
```

#### Step 3. Gather statistics

```sql
VACUUM ANALYZE books;
```

### Verifying the import

```sql
SELECT count(*) FROM books;
SELECT id, title, author, LEFT(text, 100) FROM books LIMIT 5;
```

### Testing a GIN Index

Once the data is loaded, you can build a `tsvector` and a GIN index:

```sql
-- Add a search vector column
ALTER TABLE books ADD COLUMN tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(title, '') || ' ' ||
            coalesce(author, '') || ' ' ||
            coalesce(text, '')
        )
    ) STORED;

-- Build the GIN index
CREATE INDEX idx_books_tsv ON books USING GIN (tsv);

-- Check index size
SELECT
    pg_size_pretty(pg_relation_size('idx_books_tsv')) AS gin_index,
    pg_size_pretty(pg_total_relation_size('books'))    AS total_with_data;

-- Test full-text search
EXPLAIN (ANALYZE, BUFFERS)
SELECT title, author
FROM books
WHERE tsv @@ to_tsquery('english', 'war & peace');
```

### Performance Notes

- **TOAST.** Large texts are automatically offloaded by PostgreSQL into a separate TOAST storage. Therefore, `pg_relation_size('books')` will show only a few kilobytes (just the pointers), while `pg_table_size('books')` shows the actual size including texts. This is normal.
- **Streaming.** Thanks to `streaming=True` and `dataset.take(n)`, the script reads only the required number of records and does not attempt to download the entire dataset.
- **Hugging Face Token.** For large volumes (tens of thousands of records), it makes sense to register at huggingface.co, get a token, and pass it via an environment variable to avoid rate limits:

  ```bash
  HF_TOKEN=your_token uv run python download_text.py --limit 10000
  ```

### One-step load: `load_to_pg.py`

If you want to skip the CSV/manual-copy dance, use the wrapper that does
everything in one command:

```bash
uv run python utils/load_to_pg.py --dbname mydb --table-name books --limit 1000
```

It connects to PostgreSQL, creates the table, downloads the data, loads
it via `COPY FROM STDIN`, and runs `VACUUM ANALYZE` — atomically, in one
transaction.

Options:

- `--table-name NAME` — target table (default: `books`)
- `--override` — drop the table if it already exists
- `--limit N` — number of records to download
- `--host`, `--port`, `--dbname`, `--user`, `--password` — PostgreSQL connection
- `--keep-csv PATH` — save the intermediate CSV for debugging

Or via environment variables (`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`,
`PGPASSWORD`), which are the standard libpq variables.


### License

The script uses third-party datasets from Hugging Face. Before using them in production, check the license of the specific dataset on its page.


