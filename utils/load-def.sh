#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
deactivate 2>/dev/null || true

usage() {
    cat <<'EOF'
Usage: load-def.sh <limit> [options]

Download books from Hugging Face and load them into PostgreSQL.
Creates tables: book_header, book_text.

Positional:
  <limit>              Number of books to load (0 is allowed with --skip-load)

Caching:
  --use-cache          Reuse CSVs from storage/ if present for this <limit>
                       and --chunk-size; save newly downloaded CSVs afterwards
  --keep-csv           Keep CSVs after download (usable by a later --use-cache run)

Load:
  --override           Drop book_header/book_text before loading
  --skip-load          Skip download and COPY (process existing tables only)
  --skip-process       Skip GIN indexes and VACUUM ANALYZE
  --with-gin           Create GIN indexes on book_header.tsv and book_text.tsv

Data:
  --dataset NAME       Hugging Face dataset (default: AdhyanshVerma/pg-en)
  --chunk-size N       Chars per text chunk (default: 250000)
  --fts-config NAME    Text search configuration (default: english)

Connection (also accepts PG* env vars):
  --host HOST          default: 127.0.0.1
  --port PORT          default: 5432
  --dbname DBNAME      default: postgres
  --user USER          default: $USER
  --password PASS      default: (none)

Examples:
  load-def.sh 5000 --override
  load-def.sh 5000 --override --with-gin
  load-def.sh 5000 --override --use-cache           # fast reload after first run
  load-def.sh 0    --skip-load --with-gin           # index existing tables only
  load-def.sh 5000 --keep-csv --override            # download, keep for later
  PGDATABASE=bench load-def.sh 10000 --override --use-cache
EOF
}

if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    [[ $# -lt 1 ]] && exit 1 || exit 0
fi

LIMIT="$1"; shift

if ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
    echo "Error: limit must be a non-negative integer, got '$LIMIT'" >&2
    echo >&2
    usage >&2
    exit 1
fi

cd "$SCRIPT_DIR"
exec uv run python load_to_pg.py --limit "$LIMIT" "$@"

