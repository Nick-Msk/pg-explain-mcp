#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Деактивируем чужое venv, чтобы uv не ругался
deactivate 2>/dev/null || true

usage() {
    cat <<EOF
Usage: $(basename "$0") <table_name> <limit> [extra options]

Examples:
    $(basename "$0") books3 66
    $(basename "$0") books3 66 --override
    $(basename "$0") books3 66 --override --with-gin
    $(basename "$0") books3 66 --skip-load               # только пересобрать tsv
    $(basename "$0") books_ru 500 --fts-config russian

Environment variables (libpq conventions):
    PGHOST       default: 127.0.0.1
    PGPORT       default: 5432
    PGDATABASE   default: postgres
    PGUSER       default: \$USER
    PGPASSWORD   (optional)

Full options: uv run python load_to_pg.py --help
EOF
}

if [[ $# -lt 2 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 1
fi

TABLE="$1"
LIMIT="$2"
shift 2

if ! [[ "$LIMIT" =~ ^[1-9][0-9]*$ ]]; then
    echo "Error: limit must be a positive integer, got '$LIMIT'" >&2
    exit 1
fi

cd "$SCRIPT_DIR"
exec uv run python load_to_pg.py --table-name "$TABLE" --limit "$LIMIT" "$@"

