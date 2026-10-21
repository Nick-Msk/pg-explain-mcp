#!/usr/bin/env python3
"""
Download text from Hugging Face and load it into PostgreSQL.

Schema (fixed names):
  book_header (id, title, author, tsv)          tsv weights: title=A, author=B
  book_text   (book_id, chunk, text_chunk, tsv) tsv weight:  text=D

Search with find_expression() — see utils/find_expression.sql.

Caching:
  CSVs are stored in --storage-dir as
    books_<limit>_chunk<chunk_size>_header.csv
    books_<limit>_chunk<chunk_size>_text.csv

  --use-cache   reuse them if present; keep them after download otherwise
  --keep-csv    keep them after download, but don't use them automatically

  Changing --limit or --chunk-size invalidates the cache (different filename).
"""

import argparse
import csv
import os
import sys
import time
from pathlib import Path

try:
    from datasets import load_dataset
except ImportError:
    print("Error: 'datasets' not installed. Run: uv pip install datasets", file=sys.stderr)
    sys.exit(1)

try:
    import psycopg
    from psycopg import sql
except ImportError:
    print("Error: 'psycopg' not installed. Run: uv pip install 'psycopg[binary]'", file=sys.stderr)
    sys.exit(1)


SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_STORAGE_DIR = SCRIPT_DIR / "storage"
DEFAULT_CHUNK_SIZE = 8192

HEADER_TABLE = "book_header"
TEXT_TABLE   = "book_text"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download text from HF and load into PostgreSQL (book_header / book_text).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g_db = p.add_argument_group("PostgreSQL connection")
    g_db.add_argument("--host",     default=os.environ.get("PGHOST", "127.0.0.1"))
    g_db.add_argument("--port",     type=int, default=int(os.environ.get("PGPORT", 5432)))
    g_db.add_argument("--dbname",   default=os.environ.get("PGDATABASE", "postgres"))
    g_db.add_argument("--user",     default=os.environ.get("PGUSER", os.environ.get("USER")))
    g_db.add_argument("--password", default=os.environ.get("PGPASSWORD"))

    g_src = p.add_argument_group("Data source")
    g_src.add_argument("--limit",      type=int, default=1000,
                       help="Number of books (default: 1000)")
    g_src.add_argument("--dataset",    default="AdhyanshVerma/pg-en")
    g_src.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    g_src.add_argument("--storage-dir", default=str(DEFAULT_STORAGE_DIR))

    g_cache = p.add_argument_group("Caching")
    g_cache.add_argument("--use-cache", action="store_true",
                         help="Reuse CSVs from storage-dir if present; keep them after download")
    g_cache.add_argument("--keep-csv", action="store_true",
                         help="Keep CSVs after download (usable by a later --use-cache run)")

    g_proc = p.add_argument_group("Processing")
    g_proc.add_argument("--fts-config", default="english")
    g_proc.add_argument("--override", action="store_true",
                        help="Drop book_header/book_text if they exist")
    g_proc.add_argument("--skip-load",    action="store_true")
    g_proc.add_argument("--skip-process", action="store_true")
    g_proc.add_argument("--with-gin", action="store_true",
                        help="Create GIN indexes on book_header.tsv and book_text.tsv")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _size_str(path: Path) -> str:
    n = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def csv_paths(storage_dir: Path, limit: int, chunk_size: int) -> tuple[Path, Path]:
    """Predictable CSV filenames so cache lookups are deterministic."""
    stem = f"books_{limit}_chunk{chunk_size}"
    return (storage_dir / f"{stem}_header.csv",
            storage_dir / f"{stem}_text.csv")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def build_conn_kwargs(args) -> dict:
    kwargs = {"host": args.host, "port": args.port,
              "dbname": args.dbname, "user": args.user}
    if args.password:
        kwargs["password"] = args.password
    return kwargs


def connect(kwargs) -> psycopg.Connection:
    print(f"→ Connecting to postgres://{kwargs['user']}@{kwargs['host']}:"
          f"{kwargs['port']}/{kwargs['dbname']}")
    try:
        conn = psycopg.connect(**kwargs)
    except psycopg.OperationalError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), version()")
            db, ver = cur.fetchone()
        conn.rollback()
    except Exception as e:
        print(f"Error: connection check failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"✓ Connected to '{db}' — {ver.split(',')[0]}")
    return conn


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_size: int) -> list[str]:
    if not text:
        return [""]
    chunks, pos, n = [], 0, len(text)
    while pos < n:
        end = min(pos + chunk_size, n)
        if end < n:
            bp = text.rfind(" ", pos, end)
            if bp > pos + chunk_size // 2:
                end = bp
        chunks.append(text[pos:end].strip())
        pos = end
    return chunks


def download_to_csv(dataset_name, limit, chunk_size, header_path: Path, text_path: Path):
    print(f"→ Streaming {limit} books from '{dataset_name}' ...")
    print("  (this can take a while; use --use-cache on the next run)")
    dataset = load_dataset(dataset_name, split="train", streaming=True)

    books, total_chunks = 0, 0
    with open(header_path, "w", newline="", encoding="utf-8") as fh, \
         open(text_path,   "w", newline="", encoding="utf-8") as ft:
        wh, wt = csv.writer(fh), csv.writer(ft)
        wh.writerow(["id", "title", "author"])
        wt.writerow(["book_id", "chunk", "text_chunk"])

        for row in dataset.take(limit):
            books += 1
            title  = (row.get("title")  or "").replace("\x00", "")
            author = (row.get("author") or "").replace("\x00", "")
            text   = (row.get("text")   or "").replace("\x00", "")
            text   = " ".join(text.split())

            wh.writerow([books, title, author])
            for i, chunk in enumerate(chunk_text(text, chunk_size)):
                wt.writerow([books, i, chunk])
                total_chunks += 1

            if books % 100 == 0:
                print(f"  {books}/{limit} books, {total_chunks} chunks")

    print(f"✓ Downloaded {books} books, {total_chunks} chunks")
    print(f"  header: {header_path.name} ({_size_str(header_path)})")
    print(f"  text:   {text_path.name} ({_size_str(text_path)})")
    return books, total_chunks

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def create_obj(conn, args) -> None:
    header_tbl = sql.Identifier(HEADER_TABLE)
    text_tbl   = sql.Identifier(TEXT_TABLE)
    fts        = sql.Literal(args.fts_config)

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s), to_regclass(%s)",
                    (HEADER_TABLE, TEXT_TABLE))
        h_ex, t_ex = cur.fetchone()

        if (h_ex or t_ex) and not args.override:
            print(f"Error: tables exist. Use --override.", file=sys.stderr)
            sys.exit(2)

        if h_ex or t_ex:
            print(f"→ Dropping {HEADER_TABLE} / {TEXT_TABLE} ...")
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(text_tbl))
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(header_tbl))

        print(f"→ Creating {HEADER_TABLE} (title=A, author=B weights) ...")
        cur.execute(sql.SQL(
            "CREATE TABLE {} ("
            "  id SERIAL PRIMARY KEY,"
            "  title TEXT,"
            "  author TEXT,"
            "  tsv tsvector GENERATED ALWAYS AS ("
            "    setweight(to_tsvector({}::regconfig, coalesce(title,'')),  'A') ||"
            "    setweight(to_tsvector({}::regconfig, coalesce(author,'')), 'B')"
            "  ) STORED"
            ")"
        ).format(header_tbl, fts, fts))

        print(f"→ Creating {TEXT_TABLE} (PK: book_id, chunk) ...")
        cur.execute(sql.SQL(
            "CREATE TABLE {} ("
            "  book_id INT NOT NULL REFERENCES {}(id) ON DELETE CASCADE,"
            "  chunk INT NOT NULL,"
            "  text_chunk TEXT,"
            "  tsv tsvector GENERATED ALWAYS AS ("
            "    to_tsvector({}::regconfig, coalesce(text_chunk,''))"
            "  ) STORED,"
            "  PRIMARY KEY (book_id, chunk)"
            ")"
        ).format(text_tbl, header_tbl, fts))

    conn.commit()
    print("✓ Schema created.")


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_to_postgres(conn, header_path, text_path) -> None:
    header_tbl = sql.Identifier(HEADER_TABLE)
    text_tbl   = sql.Identifier(TEXT_TABLE)

    with conn.cursor() as cur:
        print("→ COPY headers ...")
        with open(header_path, "r", encoding="utf-8") as f:
            next(f)
            with cur.copy(sql.SQL(
                "COPY {} (id, title, author) FROM STDIN WITH (FORMAT csv)"
            ).format(header_tbl)) as c:
                for line in f:
                    c.write(line)

        print("→ COPY text chunks ...")
        with open(text_path, "r", encoding="utf-8") as f:
            next(f)
            with cur.copy(sql.SQL(
                "COPY {} (book_id, chunk, text_chunk) FROM STDIN WITH (FORMAT csv)"
            ).format(text_tbl)) as c:
                for line in f:
                    c.write(line)

        cur.execute(sql.SQL(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE(MAX(id), 1)) FROM {}"
        ).format(header_tbl), (HEADER_TABLE,))

    conn.commit()
    print("✓ Load committed.")


# ---------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------

def process_data(conn_kwargs, args) -> None:
    if args.with_gin:
        conn = psycopg.connect(**conn_kwargs)
        try:
            with conn.cursor() as cur:
                print(f"→ Creating GIN index on {HEADER_TABLE}.tsv ...")
                cur.execute(sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {} USING GIN (tsv)"
                ).format(sql.Identifier(f"idx_{HEADER_TABLE}_tsv"),
                         sql.Identifier(HEADER_TABLE)))

                print(f"→ Creating GIN index on {TEXT_TABLE}.tsv ...")
                cur.execute(sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {} USING GIN (tsv)"
                ).format(sql.Identifier(f"idx_{TEXT_TABLE}_tsv"),
                         sql.Identifier(TEXT_TABLE)))
            conn.commit()
        finally:
            conn.close()
        print("✓ GIN indexes created.")

    # VACUUM requires a fresh autocommit connection
    vac_conn = psycopg.connect(**conn_kwargs, autocommit=True)
    try:
        with vac_conn.cursor() as cur:
            print("→ VACUUM ANALYZE ...")
            cur.execute(sql.SQL("VACUUM ANALYZE {}").format(sql.Identifier(HEADER_TABLE)))
            cur.execute(sql.SQL("VACUUM ANALYZE {}").format(sql.Identifier(TEXT_TABLE)))
    finally:
        vac_conn.close()
    print("✓ VACUUM ANALYZE done.")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    conn_kwargs = build_conn_kwargs(args)
    conn = connect(conn_kwargs)

    storage_dir = Path(args.storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)

    header_path, text_path = csv_paths(storage_dir, args.limit, args.chunk_size)

    try:
        if not args.skip_load:
            create_obj(conn, args)

            is_cached = args.use_cache and header_path.exists() and text_path.exists()

            if is_cached:
                print(f"✓ Using cached CSVs from {storage_dir}:")
                print(f"  header: {header_path.name} ({_size_str(header_path)})")
                print(f"  text:   {text_path.name} ({_size_str(text_path)})")
            else:
                if args.use_cache:
                    print(f"→ No cache for limit={args.limit}, chunk_size={args.chunk_size}; downloading ...")
                download_to_csv(args.dataset, args.limit, args.chunk_size,
                                header_path, text_path)

            load_to_postgres(conn, header_path, text_path)

            # Cache management
            if args.use_cache or args.keep_csv:
                print(f"✓ CSVs kept in {storage_dir}")
                if not is_cached:
                    print(f"  → Next run with --use-cache will skip download")
            else:
                for p in (header_path, text_path):
                    if p.exists():
                        p.unlink()
                print("  (CSVs removed; use --use-cache or --keep-csv to keep them)")

        if not args.skip_process:
            process_data(conn_kwargs, args)
    finally:
        conn.close()

    print("\n✓ All done.")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)

