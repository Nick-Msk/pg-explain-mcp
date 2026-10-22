#!/usr/bin/env python3
"""
Download text from Hugging Face and load it into PostgreSQL.

Pipeline:
  1. load_from_origin  -> storage/raw_books_<limit>.jsonl (raw text with \n)
  2. process_to_csv    -> storage/books_<limit>_chunk<size>_*.csv (chunked)
  3. load_to_db        -> book_header / book_text in PostgreSQL

Caches are independent: changing chunk-size or chapter-parsing logic only
re-runs step 2; changing --limit re-runs step 1.
"""

import argparse
import csv
import json
import os
import re
import sys
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

from chapter_parser import ChapterParser

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_STORAGE_DIR = SCRIPT_DIR / "storage"
DEFAULT_CHUNK_SIZE = 4096

HEADER_TABLE = "book_header"
TEXT_TABLE   = "book_text"

_parser = ChapterParser()


def parse_args():
    p = argparse.ArgumentParser(
        description="Download books from HF and load into PostgreSQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g_db = p.add_argument_group("PostgreSQL connection")
    g_db.add_argument("--host",     default=os.environ.get("PGHOST", "127.0.0.1"))
    g_db.add_argument("--port",     type=int, default=int(os.environ.get("PGPORT", 5432)))
    g_db.add_argument("--dbname",   default=os.environ.get("PGDATABASE", "postgres"))
    g_db.add_argument("--user",     default=os.environ.get("PGUSER", os.environ.get("USER")))
    g_db.add_argument("--password", default=os.environ.get("PGPASSWORD"))

    g_src = p.add_argument_group("Data source")
    g_src.add_argument("--limit",      type=int, default=1000)
    g_src.add_argument("--dataset",    default="AdhyanshVerma/pg-en")
    g_src.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    g_src.add_argument("--storage-dir", default=str(DEFAULT_STORAGE_DIR))

    g_cache = p.add_argument_group("Caching")
    g_cache.add_argument("--use-cache", action="store_true",
                         help="Reuse files from storage-dir if present")
    g_cache.add_argument("--keep-csv", action="store_true",
                         help="Keep generated CSVs after load")

    g_proc = p.add_argument_group("Processing")
    g_proc.add_argument("--fts-config", default="english")
    g_proc.add_argument("--override", action="store_true")
    g_proc.add_argument("--skip-load",    action="store_true")
    g_proc.add_argument("--skip-process", action="store_true")
    g_proc.add_argument("--with-gin", action="store_true")

    g_src.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                        help=f"Chars per text chunk (default: {DEFAULT_CHUNK_SIZE})")
    return p.parse_args()


def build_conn_kwargs(args) -> dict:
    kw = {"host": args.host, "port": args.port, "dbname": args.dbname, "user": args.user}
    if args.password:
        kw["password"] = args.password
    return kw


def connect(kwargs) -> psycopg.Connection:
    print(f"→ Connecting to postgres://{kwargs['user']}@{kwargs['host']}:{kwargs['port']}/{kwargs['dbname']}")
    try:
        conn = psycopg.connect(**kwargs)
    except psycopg.OperationalError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    conn.rollback()  # clear any implicit txn
    print("✓ Connected.")
    return conn


# ---------------------------------------------------------------------------
# Step 1: load_from_origin
# ---------------------------------------------------------------------------

def load_from_origin(dataset_name: str, limit: int, path: Path) -> None:
    """Download raw text from HF to JSONL (preserves newlines for parsing)."""
    print(f"→ Streaming {limit} books from '{dataset_name}' ...")
    print("  (this may take a while; the result is cached for future runs)")
    dataset = load_dataset(dataset_name, split="train", streaming=True)

    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in dataset.take(limit):
            count += 1
            title  = (row.get("title")  or "").replace("\x00", "")
            author = (row.get("author") or "").replace("\x00", "")
            text   = (row.get("text")   or "").replace("\x00", "")

            # В JSONL сохраняем \n как есть, чтобы парсер глав потом работал
            record = {"id": count, "title": title, "author": author, "text": text}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

            if count % 100 == 0:
                print(f"  {count}/{limit} books downloaded")

    print(f"✓ Downloaded {count} books to {path.name}")


# ---------------------------------------------------------------------------
# Step 2: process_to_csv
# ---------------------------------------------------------------------------

def _split_text(text: str, chunk_size: int) -> list[tuple[str, str | None, int | None]]:
    """Normalize whitespace, parse chapters, split into chunks."""
    if not text:
        return [("", None, None)]

    # Нормализуем пробелы, но СОХРАНЯЕМ \n (иначе парсер глав сломается)
    text = re.sub(r'[^\S\n]+', ' ', text)
    text = re.sub(r'\n{2,}', '\n', text)
    text = text.strip()

    sections = _parser.parse(text)

    def find_section(pos: int) -> tuple[str | None, int | None]:
        current_title, current_num = None, None
        for header_pos, header_title, header_num in sections:
            if header_pos > pos:
                break
            current_title, current_num = header_title, header_num
        return current_title, current_num

    chunks = []
    pos, n = 0, len(text)
    while pos < n:
        end = min(pos + chunk_size, n)
        if end < n:
            bp = text.rfind(" ", pos, end)
            if bp > pos + chunk_size // 2:
                end = bp

        chunk_body = text[pos:end].strip()
        chapter, chap_num = find_section(pos)
        chunks.append((chunk_body, chapter, chap_num))
        pos = end

    return chunks


def process_to_csv(raw_path: Path, chunk_size: int, header_path: Path, text_path: Path) -> None:
    """Read raw JSONL, parse chapters, chunk, write two CSVs."""
    print(f"→ Processing {raw_path.name} into chunks (size {chunk_size}) ...")

    total_books, total_chunks = 0, 0
    with open(raw_path, encoding="utf-8") as f_raw, \
         open(header_path, "w", newline="", encoding="utf-8") as f_h, \
         open(text_path, "w", newline="", encoding="utf-8") as f_t:

        wh, wt = csv.writer(f_h), csv.writer(f_t)
        wh.writerow(["id", "title", "author"])
        wt.writerow(["book_id", "chunk", "chapter", "chap_num", "text_chunk"])

        for line in f_raw:
            total_books += 1
            rec = json.loads(line)
            book_id = rec["id"]

            wh.writerow([book_id, rec["title"], rec["author"]])

            chunks = _split_text(rec["text"], chunk_size)
            for i, (chunk, chapter, chap_num) in enumerate(chunks):
                wt.writerow([book_id, i, chapter or "", chap_num or "", chunk])
                total_chunks += 1

    print(f"✓ Processed {total_books} books into {total_chunks} chunks")
    print(f"  header: {header_path.name}")
    print(f"  text:   {text_path.name}")


# ---------------------------------------------------------------------------
# Step 3: load_to_db
# ---------------------------------------------------------------------------

def create_obj(conn, fts_config: str, override: bool):
    header_tbl = sql.Identifier(HEADER_TABLE)
    text_tbl   = sql.Identifier(TEXT_TABLE)
    fts        = sql.Literal(fts_config)

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s), to_regclass(%s)", (HEADER_TABLE, TEXT_TABLE))
        h_ex, t_ex = cur.fetchone()

        if (h_ex or t_ex) and not override:
            print("Error: tables exist. Use --override.", file=sys.stderr)
            sys.exit(2)

        if h_ex or t_ex:
            print(f"→ Dropping {HEADER_TABLE} / {TEXT_TABLE} ...")
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(text_tbl))
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(header_tbl))

        print(f"→ Creating {HEADER_TABLE} ...")
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

        print(f"→ Creating {TEXT_TABLE} ...")
        cur.execute(sql.SQL(
            "CREATE TABLE {} ("
            "  book_id INT NOT NULL REFERENCES {}(id) ON DELETE CASCADE,"
            "  chunk INT NOT NULL,"
            "  chapter TEXT,"
            "  chap_num INT,"
            "  text_chunk TEXT,"
            "  tsv tsvector GENERATED ALWAYS AS ("
            "    to_tsvector({}::regconfig, coalesce(text_chunk,''))"
            "  ) STORED,"
            "  PRIMARY KEY (book_id, chunk)"
            ")"
        ).format(text_tbl, header_tbl, fts))

    conn.commit()
    print("✓ Schema created.")


def load_to_db(conn, header_path: Path, text_path: Path):
    header_tbl = sql.Identifier(HEADER_TABLE)
    text_tbl   = sql.Identifier(TEXT_TABLE)

    with conn.cursor() as cur:
        print("→ COPY headers ...")
        with open(header_path, encoding="utf-8") as f:
            next(f)
            with cur.copy(sql.SQL("COPY {} (id, title, author) FROM STDIN WITH (FORMAT csv)").format(header_tbl)) as c:
                for line in f:
                    c.write(line)

        print("→ COPY text chunks ...")
        with open(text_path, encoding="utf-8") as f:
            next(f)
            with cur.copy(sql.SQL("COPY {} (book_id, chunk, chapter, chap_num, text_chunk) FROM STDIN WITH (FORMAT csv)").format(text_tbl)) as c:
                for line in f:
                    c.write(line)

        cur.execute(sql.SQL(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE(MAX(id), 1)) FROM {}"
        ).format(header_tbl), (HEADER_TABLE,))

    conn.commit()
    print("✓ Load committed.")


def process_data(conn_kwargs, with_gin: bool):
    if with_gin:
        conn = psycopg.connect(**conn_kwargs)
        try:
            with conn.cursor() as cur:
                print("→ Creating GIN indexes ...")
                cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING GIN (tsv)").format(
                    sql.Identifier(f"idx_{HEADER_TABLE}_tsv"), sql.Identifier(HEADER_TABLE)))
                cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING GIN (tsv)").format(
                    sql.Identifier(f"idx_{TEXT_TABLE}_tsv"), sql.Identifier(TEXT_TABLE)))
            conn.commit()
        finally:
            conn.close()
        print("✓ GIN indexes created.")

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

def main():
    args = parse_args()

    conn_kwargs = build_conn_kwargs(args)
    conn = connect(conn_kwargs)

    storage_dir = Path(args.storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)

    # Пути для кэшей
    raw_path = storage_dir / f"raw_books_{args.limit}.jsonl"
    header_path = storage_dir / f"books_{args.limit}_chunk{args.chunk_size}_header.csv"
    text_path   = storage_dir / f"books_{args.limit}_chunk{args.chunk_size}_text.csv"

    try:
        if not args.skip_load:
            # Шаг 1: Скачивание
            if args.use_cache and raw_path.exists():
                print(f"✓ Using cached raw text: {raw_path.name}")
            else:
                load_from_origin(args.dataset, args.limit, raw_path)

            # Шаг 2: Обработка
            if args.use_cache and header_path.exists() and text_path.exists():
                print(f"✓ Using processed CSVs for chunk_size={args.chunk_size}")
            else:
                process_to_csv(raw_path, args.chunk_size, header_path, text_path)

            # Шаг 3: Заливка в БД
            create_obj(conn, args.fts_config, args.override)
            load_to_db(conn, header_path, text_path)

            if not (args.use_cache or args.keep_csv):
                for p in (header_path, text_path):
                    if p.exists():
                        p.unlink()
                print("  (CSVs removed; use --use-cache to keep them)")

        if not args.skip_process:
            process_data(conn_kwargs, args.with_gin)
    finally:
        conn.close()

    print("\n✓ All done.")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
