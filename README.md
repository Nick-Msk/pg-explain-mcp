# pg-explain-mcp

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/MCP-compatible-purple)

An MCP (Model Context Protocol) server for analyzing PostgreSQL query
execution plans. Built as a bridge between LLM-based coding assistants
(Continue.dev, Claude Desktop, Cursor) and a PostgreSQL database.

The server runs `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on `SELECT`
queries and returns a structured report highlighting performance
bottlenecks — so an LLM can explain *why* a query is slow and *what to do*
about it, instead of just describing the SQL.

## Features

- **`ping`** — health check.
- **`list_tables`** — returns all user tables with their columns.
- **`explain`** — runs `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on a
  `SELECT`/`WITH` query and returns a structured report highlighting:
  - Sequential scans on large tables (likely missing indexes)
  - Large mismatches between planner estimates and actual row counts
  - Disk spills in sorts and hash joins (`work_mem` issues)
  - Excessive `Nested Loop` iterations

All database access is **read-only** — the connection runs inside
`SET TRANSACTION READ ONLY`, so even a buggy query cannot modify data.

## Architecture

```
LLM (Continue.dev) ──MCP──> pg-explain-mcp ──psycopg3──> PostgreSQL
                                  │
                                  ├── db.py        (connection, EXPLAIN)
                                  ├── analyzer.py  (plan analysis)
                                  └── server.py    (MCP tools)
```

- **`db.py`** — connection layer. Opens a read-only transaction,
  provides `get_schema()` and `explain_query()`.
- **`analyzer.py`** — recursive traversal of the JSON plan tree. Detects
  bottlenecks and produces a structured report with `issues` and
  `summary`.
- **`server.py`** — MCP entry point. Exposes three tools via `FastMCP`.

## Requirements

- Python 3.10+
- PostgreSQL 12+ (tested on 16, 17, 18)
- An MCP-compatible client (Continue.dev, Claude Desktop, Cursor, etc.)

## Installation

```bash
git clone https://github.com/Nick-Msk/pg-explain-mcp.git
cd pg-explain-mcp

python3.12 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install --upgrade pip
pip install -e .
```

`pip install -e .` installs the package in editable mode and registers
the `pg-explain-mcp` console script.

### Verify installation

```bash
python -c "from pg_explain_mcp import server; print('OK')"
# → OK
```

## Configuration

The server reads connection parameters from environment variables:

| Variable      | Default     | Description          |
|---------------|-------------|----------------------|
| `PG_HOST`     | `localhost` | PostgreSQL host      |
| `PG_PORT`     | `5432`      | PostgreSQL port      |
| `PG_USER`     | `postgres`  | Database user        |
| `PG_PASSWORD` | —           | Database password    |
| `PG_DATABASE` | `postgres`  | Database name        |

## Usage with Continue.dev

Create `.continue/mcpServers/pg-explain.yaml` in your workspace:

```yaml
name: PostgreSQL Explain MCP
version: 0.0.1
schema: v1
mcpServers:
  - name: pg-explain
    command: /path/to/pg-explain-mcp/.venv/bin/python
    args:
      - "-m"
      - "pg_explain_mcp.server"
    env:
      PG_HOST: localhost
      PG_PORT: "5432"
      PG_USER: your_user
      PG_PASSWORD: your_password
      PG_DATABASE: your_database
```

Then in VS Code:

1. `Cmd+Shift+P` → **`Continue: Reload Config`**.
2. Open a new chat in **Agent Mode** (not Chat, not Edit).
3. Ask:

   > Use the pg-explain tool to analyze:
   > `SELECT * FROM onek1 WHERE hundred BETWEEN 5 AND 55;`

The agent will call the `explain` tool and return a structured report
with execution time, detected issues, and recommendations.

### Debugging

If the tool doesn't appear, check that:

- The path in `command:` points to the actual Python inside `.venv`.
- The package is installed: `pip show pg-explain-mcp` (should show
  `Editable project location: .../pg-explain-mcp`).
- You are in **Agent Mode**, not Chat or Edit.

You can also test the server standalone via the official MCP Inspector:

```bash
mcp dev src/pg_explain_mcp/server.py
```

## Example

Prompt:

> Use the pg-explain tool to analyze:
> `SELECT * FROM onek1 WHERE hundred BETWEEN 5 AND 55;`

Response (abridged):

```
✓ Continue used the pg-explain explain tool

Execution Time: 0.904 ms
Planning Time:  1.228 ms
Total Time:     2.132 ms
Issues Found:   None

Analysis:
The query is running very fast, likely because the onek1 table is
relatively small (estimated at 1,000 rows), allowing a Sequential Scan
almost instantly.

Note: There is no index on the `hundred` column. For a small table this
is fine, but if the table grows to millions of rows, this query would
become slower...
```
## Demo

See [`usage_examples/`](usage_examples/) for full write-ups.

- [Ordered-set aggregates over a 5M-row table](usage_examples/sample_query1.md)
- [IndexScanCheck: healthy table](usage_examples/pg_index_scan_adapters/sample_index_on_normal.md)
- [IndexScanCheck: stale visibility map after churn](usage_examples/pg_index_scan_adapters/sample_index_on_unclastered.md)

The IndexScanCheck examples include a reproducible SQL scenario:
[`usage_examples/pg_index_scan_adapters/pg_samples.sql`](usage_examples/pg_index_scan_adapters/pg_samples.sql).

## Development

```bash
# Run the server manually (waits for JSON-RPC on stdio)
python -m pg_explain_mcp.server

# Or use the console script
pg-explain-mcp
```

Note: running the server manually in a terminal is **not** a valid test
— MCP servers speak JSON-RPC over stdio and expect a client. Use
`mcp dev` or an MCP-compatible assistant for interactive testing.

## Project structure

```
pg-explain-mcp/
├── src/
│   └── pg_explain_mcp/
│       ├── __init__.py
│       ├── server.py       # MCP entry point
│       ├── db.py           # connection + EXPLAIN
│       └── analyzer.py     # plan analysis
├── pyproject.toml
├── README.md
└── LICENSE
```

## Contributing

Pull requests are welcome. For major changes, please open an issue first
to discuss what you would like to change.

## License

MIT — see [LICENSE](LICENSE) for details.
