#!/usr/bin/env python3
"""
Download text from Hugging Face and load it into PostgreSQL.

Two phases:
  1. load    — download from HF + create table + COPY FROM STDIN
  2. process — add tsvector column + build GIN index + VACUUM ANALYZE

Examples:
    uv run python load_to_pg.py --dbname mydb --table-name books --limit 1000
    uv run python load_to_pg.py --dbname mydb --table-name books --limit 1000 --override
    uv run python load_to_pg.py --dbname mydb --table-name books --skip-load
    uv run python load_to_pg.py --dbname mydb --table-name books --skip-process
"""

import argparse
import csv
import os
import sys
import tempfile

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download text from Hugging Face and load it into PostgreSQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    g_db = p.add_argument_group("PostgreSQL connection (or PG* env vars)")
    g_db.add_argument("--host",     default=os.environ.get("PGHOST", "127.0.0.1"))
    g_db.add_argument("--port",     type=int, default=int(os.environ.get("PGPORT", 5432)))
    g_db.add_argument("--dbname",   default=os.environ.get("PGDATABASE", "postgres"))
    g_db.add_argument("--user",     default=os.environ.get("PGUSER", os.environ.get("USER")))
    g_db.add_argument("--password", default=os.environ.get("PGPASSWORD"))

    g_tbl = p.add_argument_group("Target table")
    g_tbl.add_argument("--table-name", default="books", help="Target table name (default: books)")
    g_tbl.add_argument("--override", action="store_true", help="Drop the table if it exists (load phase only)")

    g_src = p.add_argument_group("Data source")
    g_src.add_argument("--limit",   type=int, default=1000, help="Number of records (default: 1000)")
    g_src.add_argument("--dataset", default="AdhyanshVerma/pg-en", help="Hugging Face dataset")
    g_src.add_argument("--keep-csv", default=None, metavar="PATH", help="Keep the downloaded CSV")

    g_proc = p.add_argument_group("Processing")
    g_proc.add_argument("--fts-config", default="english",
                        help="Text search configuration (default: english; try 'russian')")
    g_proc.add_argument("--skip-load",    action="store_true", help="Skip download and COPY")
    g_proc.add_argument("--skip-process", action="store_true", help="Skip tsvector + GIN + VACUUM")
    g_proc.add_argument("--with-gin", action="store_true",
                        help="Also create GIN index on the tsv column (default: no index)")

    return p.parse_args()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(args) -> "psycopg.Connection":
    kwargs = {"host": args.host, "port": args.port,
              "dbname": args.dbname, "user": args.user}
    if args.password:
        kwargs["password"] = args.password

    print(f"→ Connecting to postgres://{args.user}@{args.host}:{args.port}/{args.dbname}")
    try:
        return psycopg.connect(**kwargs)
    except psycopg.OperationalError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Phase 1: download
# ---------------------------------------------------------------------------

def download_to_csv(dataset_name: str, limit: int, path: str) -> int:
    print(f"→ Streaming {limit} records from '{dataset_name}' ...")
    dataset = load_dataset(dataset_name, split="train", streaming=True)

    count = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["title", "author", "text"])
        for row in dataset.take(limit):
            # Безопасно извлекаем поля (None -> "")
            text = row.get("text") or ""
            title = row.get("title") or ""
            author = row.get("author") or ""

            # Нормализуем пробелы
            text = " ".join(text.split())

            # Вычищаем нулевые байты
            text = text.replace("\x00", "")
            title = title.replace("\x00", "")
            author = author.replace("\x00", "")

            w.writerow([title, author, text])
            count += 1
            if count % 100 == 0:
                print(f"  {count}/{limit}")

    print(f"✓ Downloaded {count} records")
    return count


# ---------------------------------------------------------------------------
# Phase 1: load
# ---------------------------------------------------------------------------

def load_to_postgres(conn, args, csv_path: str) -> None:
    table = sql.Identifier(args.table_name)

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (args.table_name,))
        exists = cur.fetchone()[0] is not None

        if exists and not args.override:
            print(f"Error: table '{args.table_name}' already exists. Use --override.", file=sys.stderr)
            sys.exit(2)

        if exists:
            print(f"→ Dropping existing table {args.table_name} ...")
            cur.execute(sql.SQL("DROP TABLE {} CASCADE").format(table))

        print(f"→ Creating table {args.table_name} ...")
        cur.execute(sql.SQL(
            "CREATE TABLE {} ("
            "  id SERIAL PRIMARY KEY,"
            "  title TEXT,"
            "  author TEXT,"
            "  text TEXT"
            ")"
        ).format(table))

        print("→ Loading data via COPY FROM STDIN ...")
        with open(csv_path, "r", encoding="utf-8") as f:
            next(f)  # skip header
            with cur.copy(
                sql.SQL("COPY {} (title, author, text) FROM STDIN WITH (FORMAT csv)").format(table)
            ) as copy:
                for line in f:
                    copy.write(line)

    conn.commit()
    print("✓ Load committed.")


# ---------------------------------------------------------------------------
# Phase 2: process
# ---------------------------------------------------------------------------

def process_data(conn, args) -> None:
    table = sql.Identifier(args.table_name)
    fts_config = sql.Literal(args.fts_config)

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (args.table_name,))
        if cur.fetchone()[0] is None:
            print(f"Error: table '{args.table_name}' does not exist (run without --skip-load first).",
                  file=sys.stderr)
            sys.exit(3)

        print(f"→ Adding tsvector column (config: {args.fts_config}) ...")
        cur.execute(sql.SQL(
            "ALTER TABLE {} ADD COLUMN IF NOT EXISTS tsv tsvector "
            "GENERATED ALWAYS AS ("
            "  to_tsvector({}::regconfig,"
            "    coalesce(title,'') || ' ' || coalesce(author,'') || ' ' || coalesce(text,''))"
            ") STORED"
        ).format(table, fts_config))

        # Создаем GIN только если явно попросили (--with-gin)
        if args.with_gin:
            index_name = sql.Identifier(f"idx_{args.table_name}_tsv")
            print(f"→ Creating GIN index {index_name.as_string()} ...")
            cur.execute(sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON {} USING GIN (tsv)"
            ).format(index_name, table))

    conn.commit()
    print("✓ Processing committed.")

    # VACUUM вне транзакции (обновляем статистику)
    conn.autocommit = True
    with conn.cursor() as cur:
        print("→ VACUUM ANALYZE ...")
        cur.execute(sql.SQL("VACUUM ANALYZE {}").format(table))
    print("✓ VACUUM ANALYZE done.")

# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    csv_path = None
    cleanup = False

    if not args.skip_load:
        if args.keep_csv:
            csv_path = args.keep_csv
        else:
            fd, csv_path = tempfile.mkstemp(suffix=".csv", prefix=f"{args.table_name}_")
            os.close(fd)
            cleanup = True

    conn = connect(args)
    try:
        if not args.skip_load:
            download_to_csv(args.dataset, args.limit, csv_path)
            load_to_postgres(conn, args, csv_path)

        if not args.skip_process:
            process_data(conn, args)
    finally:
        conn.close()
        if cleanup and csv_path and os.path.exists(csv_path):
            os.remove(csv_path)

    print("\n✓ All done.")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)  # avoid hanging on HF background threads

