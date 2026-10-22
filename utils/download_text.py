#!/usr/bin/env python3
"""
Download a small subset of a Hugging Face dataset using streaming.

This avoids downloading the entire dataset to disk. It streams records
and stops after reaching the specified limit.
"""

import argparse
import csv
import os
import sys

try:
    from datasets import load_dataset
except ImportError:
    print("Error: 'datasets' library is not installed.")
    print("Please install it using: uv pip install datasets")
    sys.exit(1)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download a text dataset subset from Hugging Face using streaming."
    )
    parser.add_argument(
        "-l", "--limit",
        type=int,
        default=1000,
        help="Number of records to download (default: 1000)."
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default="books.csv",
        help="Output CSV filename (default: books.csv)."
    )
    parser.add_argument(
        "-d", "--dataset",
        type=str,
        default="AdhyanshVerma/pg-en",
        help="Hugging Face dataset name (default: AdhyanshVerma/pg-en)."
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print(f"Streaming dataset '{args.dataset}' (limit: {args.limit} records)...")
    print("No full download will occur; only the necessary data is fetched.")

    try:
        # Загружаем датасет в потоковом режиме БЕЗ срезов в split
        dataset = load_dataset(
            args.dataset,
            split="train",
            streaming=True
        )
    except Exception as e:
        print(f"Failed to start dataset stream: {e}")
        sys.exit(1)

    print(f"Stream ready. Writing first {args.limit} records to {args.output}...")

    try:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["title", "author", "text"])

            count = 0
            # Используем .take(), чтобы остановить поток ровно на limit записях
            for row in dataset.take(args.limit):
                title = row.get("title", "")
                author = row.get("author", "")
                text = row.get("text", "")

                # Заменяем переносы строк на пробелы, чтобы не ломать CSV
                text = " ".join(text.split())

                writer.writerow([title, author, text])
                count += 1

                if count % 100 == 0:
                    print(f"  Saved {count}/{args.limit} records...")

    except IOError as e:
        print(f"Failed to write to file: {e}")
        sys.exit(1)

    print(f"\nDone! File '{args.output}' contains {count} records.")
    print("To import into PostgreSQL, run:")
    print("  CREATE TABLE books (id SERIAL PRIMARY KEY, title TEXT, author TEXT, text TEXT);")
    print(f"  COPY books(title, author, text) FROM '/absolute/path/to/{args.output}' WITH (FORMAT csv, HEADER true);")


if __name__ == "__main__":
    main()
    # Принудительно завершаем процесс, чтобы фоновые потоки streaming-а
    # не блокировали выход из программы (ошибка ESRCH).
    os._exit(0)

