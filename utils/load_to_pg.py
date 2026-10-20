#!/usr/bin/env python3
"""
Download text from Hugging Face and load it into PostgreSQL in one command.

Combines data download, table creation, and bulk load via COPY FROM STDIN
(works both for local and remote PostgreSQL — no need for psql or \\copy).

Examples:
    uv run python utils/load_to_pg.py --dbname mydb --table-name books --limit 1000
    uv run python utils/load_to_pg.py --dbname mydb --table-name books --limit 10000 --override
"""

import argparse
import csv
import os
import sys
import tempfile

try:
    from datasets import load_dataset
except ImportError:
    print("Error: 'datasets' library is not installed.", file=sys.stderr)
    print("Install it with: uv pip install datasets", file=sys.stderr)
    sys.exit(1)

try:
    import psycopg
    from psycopg import sql
except ImportError:
    print("Error: 'psycopg' library is not installed.", file=sys.stderr)
    print("Install it with: uv pip install 'psycopg[binary]'", file=sys.stderr)
    sys.exit(1)


def parse_args():
    p = argparse.ArgumentParser(
        description="Download text from Hugging Face and load it into PostgreSQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    g_db = p.add_argument_group("PostgreSQL connection (or use PG* env vars)")
    g_db.add_argument("--host",     default=os.environ.get("PGHOST", "localhost"))
    g_db.add_argument("--port",     type=int, default=int(os.environ.get("PGPORT", 5432)))
    g_db.add_argument("--dbname",   default=os.environ.get("PGDATABASE", "postgres"))
    g_db.add_argument("--user",     default=os.environ.get("PGUSER", os.environ.get("USER")))
    g_db.add_argument("--password", default=os.environ.get("PGPASSWORD"))

    g_tbl = p.add_argument_group("Target table")
    g_tbl.add_argument("--table-name", default="books", help="Target table name (default: books)")
    g_tbl.add_argument("--override", action="store_true", help="Drop the table if it already exists")

    g_src = p.add_argument_group("Data source")
    g_src.add_argument("--limit",   type=int, default=1000, help="Number of records to download (default: 1000)")
    g_src.add_argument("--dataset", default="AdhyanshVerma/pg-en", help="Hugging Face dataset name")
    g_src.add_argument("--keep-csv", default=None, metavar="PATH",
                       help="Keep the downloaded CSV at PATH (useful for debugging)")

    return p.parse_args()


def download_to_csv(dataset_name: str, limit: int, path: str) -> int:
    print(f"→ Streaming {limit} records from '{dataset_name}' ...")
    dataset = load_dataset(dataset_name, split="train", streaming=True)

    count = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["title", "author", "text"])
        for row in dataset.take(limit):
            text = " ".join(row.get("text", "").split())
            w.writerow([row.get("title", ""), row.get("author", ""), text])
            count += 1
            if count % 100 == 0:
                print(f"  {count}/{limit} records downloaded")

    print(f"✓ Downloaded {count} records to {path}")
    return count

def load_to_postgres(args, csv_path: str) -> None:
    conn_kwargs = {
        "host": args.host,
        "port": args.port,
        "dbname": args.dbname,
        "user": args.user,
    }
    if args.password:
        conn_kwargs["password"] = args.password

    print(f"→ Connecting to postgres://{args.user}@{args.host}:{args.port}/{args.dbname} ...")
    try:
        conn = psycopg.connect(**conn_kwargs)
    except psycopg.OperationalError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        table = sql.Identifier(args.table_name)

        with conn.cursor() as cur:
            # 1. Проверка существования
            cur.execute("SELECT to_regclass(%s)", (args.table_name,))
            exists = cur.fetchone()[0] is not None

            if exists and not args.override:
                print(f"Error: table '{args.table_name}' already exists. Use --override.", file=sys.stderr)
                sys.exit(2)

            if exists:
                print(f"→ Dropping existing table {args.table_name} ...")
                cur.execute(sql.SQL("DROP TABLE {} CASCADE").format(table))

            # 2. Создание таблицы
            print(f"→ Creating table {args.table_name} ...")
            cur.execute(
                sql.SQL(
                    "CREATE TABLE {} ("
                    "  id SERIAL PRIMARY KEY,"
                    "  title TEXT,"
                    "  author TEXT,"
                    "  text TEXT"
                    ")"
                ).format(table)
            )

            # 3. COPY FROM STDIN
            print("→ Loading data via COPY FROM STDIN ...")
            with open(csv_path, "r", encoding="utf-8") as f:
                next(f)  # skip CSV header
                with cur.copy(
                    sql.SQL(
                        "COPY {} (title, author, text) "
                        "FROM STDIN WITH (FORMAT csv)"
                    ).format(table)
                ) as copy:
                    for line in f:
                        copy.write(line)

        conn.commit()
        print("✓ Data loaded and committed.")

        # 4. VACUUM ANALYZE — вне транзакции
        conn.autocommit = True
        with conn.cursor() as cur:
            print("→ VACUUM ANALYZE ...")
            cur.execute(sql.SQL("VACUUM ANALYZE {}").format(table))
        print("✓ VACUUM ANALYZE done.")

    finally:
        conn.close()

def main() -> None:
    args = parse_args()

    if args.keep_csv:
        csv_path = args.keep_csv
        cleanup = False
    else:
        fd, csv_path = tempfile.mkstemp(suffix=".csv", prefix=f"{args.table_name}_")
        os.close(fd)
        cleanup = True

    try:
        download_to_csv(args.dataset, args.limit, csv_path)
        load_to_postgres(args, csv_path)
    finally:
        if cleanup and os.path.exists(csv_path):
            os.remove(csv_path)

    print("\n✓ All done.")

if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning)

    main()
    # Вместо os._exit(0) используем sys.exit(0), 
    # но принудительно чистим потоки
    sys.exit(0)

