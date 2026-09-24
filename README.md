# pg-explain-mcp

![Tag](https://img.shields.io/github/v/tag/Nick-Msk/pg-explain-mcp)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/MCP-compatible-purple)
![SQL lint](https://img.shields.io/badge/sql%20lint-sqlfluff-blue)

An MCP (Model Context Protocol) server for analyzing PostgreSQL query
execution plans. Built as a bridge between LLM-based coding assistants
(Continue.dev, Claude Desktop, Cursor) and a PostgreSQL database.

The server runs `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on `SELECT`
queries and returns a structured report highlighting performance
bottlenecks — so an LLM can explain *why* a query is slow and *what to
do* about it, instead of just describing the SQL.

## Features

### MCP tools

- **`explain`** — runs `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on a
  `SELECT` / `WITH` query and returns a structured report.
- **`list_tables`** — returns all user tables and their columns.
- **`list_indexes`** — returns existing indexes for a table (or all
  tables), so the assistant can avoid recommending an index that
  already exists.
- **`list_parameters`** — returns runtime parameters relevant to plan
  analysis: `work_mem`, `hash_mem_multiplier`, `shared_buffers`,
  `effective_cache_size`, `random_page_cost`, `seq_page_cost`,
  parallel worker limits, `jit`.
- **`ping`** — health check.

### Checks

Every `explain` response includes an `issues` array. Each entry is
produced by an independent, pluggable `PlanCheck`:

| Check                    | What it reports                                        |
|--------------------------|--------------------------------------------------------|
| `SeqScanCheck`           | Sequential scan that discards most of what it reads    |
| `IndexScanCheck`         | Stale visibility map / poor heap locality              |
| `BitmapHeapScanCheck`    | Large Bitmap Heap Scan                                 |
| `DiskSpillSortCheck`     | Sort spilling to disk (`external merge`)               |
| `DiskSpillHashCheck`     | Hash operation using multiple batches                  |
| `NestedLoopCheck`        | Nested Loop with a high number of inner iterations     |
| `EstimateMismatchCheck`  | Planner cardinality misestimate                        |

To add a new check, implement the `PlanCheck` protocol in
`src/pg_explain_mcp/analyzer.py`, register the class in
`src/pg_explain_mcp/config.py`, and add its default parameters to
`config/seed.sql`.

### Structured plan output

`explain` returns a compact `plan_nodes` tree alongside `issues`.
Only the fields that matter for reasoning are kept:

- `node_type`, `relation`, `index`
- `actual_rows`, `plan_rows`, `actual_loops`
- `rows_removed_by_filter`, `heap_fetches`, `shared_read_blocks`
- `sort_method`, `sort_space_type`, `sort_space_used_kb`
- `hash_buckets`, `hash_batches`, `peak_memory_usage_kb`
- `parallel_aware`

The list of fields is configurable — see [Configuration](#configuration).

## Installation

Requires Python 3.10+.

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

### Verify

```bash
python -c "from pg_explain_mcp import server; print('OK')"
# → OK

pytest -v
```

## Configuration

### Connection

The server reads PostgreSQL connection parameters from environment
variables:

| Variable      | Default     | Description          |
|---------------|-------------|----------------------|
| `PG_HOST`     | `localhost` | PostgreSQL host      |
| `PG_PORT`     | `5432`      | PostgreSQL port      |
| `PG_USER`     | `postgres`  | Database user        |
| `PG_PASSWORD` | —           | Database password    |
| `PG_DATABASE` | `postgres`  | Database name        |

### Check registry

Enabled checks and their thresholds live in a small SQLite database at
`config/checks.db`. The database is created automatically on first
`explain` if missing, and rebuilt from scratch by an explicit `--init`:

```bash
python -m pg_explain_mcp.config --init   # rebuild from schema + seed
python -m pg_explain_mcp.config --show   # print current state
```

Changes to the database take effect on the next `explain` call — no
MCP server restart required. For example:

```bash
# Disable NestedLoopCheck without touching the code.
sqlite3 config/checks.db \
  "update checks set enabled = 0 where name = 'NestedLoopCheck';"

# Raise SeqScanCheck's threshold from 1000 to 5000 rows.
sqlite3 config/checks.db \
  "update check_params set value = '5000'
   where num = 1 and param = 'threshold_rows';"
```

The schema is multi-database ready (`checks` and `plan_fields` are
keyed by `database`), but only `postgres` is currently populated. The
`databases` table is the FK root — removing a row from it cascades to
all of its checks, params, and plan fields.

To extend the registry with a new field for `plan_nodes`:

```bash
sqlite3 config/checks.db \
  "insert into plan_fields (database, raw, key, enabled)
   values ('postgres', 'Total Cost', 'total_cost', 1);"
```

The new field appears in `plan_nodes` on the next `explain` call.

## Usage with Continue.dev

Ready-to-use configuration files are available in
[`config_example/`](config_example/):

- [`config_example/mcpServers/pg-explain.yaml`](config_example/mcpServers/pg-explain.yaml)
  — MCP server registration.
- [`config_example/postgres-agent.md`](config_example/postgres-agent.md)
  — system prompt for an assistant that knows how to use `pg-explain`
  and a generic PostgreSQL MCP server, with per-check guidance.

## Companion to `universal-db-mcp`

`pg-explain-mcp` is designed to be used **alongside** a
general-purpose PostgreSQL MCP server, not to replace it. In the
reference setup we use
[`universal-db-mcp`](https://github.com/Anarkh-Lee/universal-db-mcp)
configured for the same database, and the two servers serve different
purposes:

| Server             | Purpose                                                 |
|--------------------|---------------------------------------------------------|
| `universal-db-mcp` | General SQL execution: `SELECT`, schema exploration, ad-hoc queries. |
| `pg-explain-mcp`   | Plan analysis: `EXPLAIN ANALYZE`, structured bottleneck detection, index/parameter inspection. |

The assistant decides which to call based on the question:

- *"How many rows are in `orders`?"* → `universal-db-mcp`.
- *"Why is this query slow?"* → `pg-explain-mcp`.

All examples in [`usage_examples/`](usage_examples/) were captured with
both servers loaded. The agent configuration in
[`config_example/postgres-agent.md`](config_example/postgres-agent.md)
describes both and includes guidance on when to prefer one over the
other.

### Why not use `pg-explain-mcp` alone?

You can — the server is self-contained and exposes `list_tables` for
schema inspection. Disabling `universal-db-mcp` is perfectly fine if
you only care about plan analysis.

In practice, however, mixing the two is more convenient: `pg-explain`'s
`EXPLAIN ANALYZE` **actually executes** every query, which is expensive
for large tables. For anything that does not need a plan — a row count,
a lookup, a quick sanity check — `universal-db-mcp` runs the same query
in read-only mode with much less overhead.

The split also reduces the blast radius of an LLM mistake: a bad
`SELECT` on `universal-db-mcp` is cheap, while a bad
`EXPLAIN ANALYZE` on `pg-explain-mcp` can be expensive. Keeping the
cheap path as the default makes the setup more robust.

### Setup

1. Copy `config_example/mcpServers/pg-explain.yaml` into your
   workspace's `.continue/mcpServers/` directory.
2. Replace the placeholders:
   - `command:` — full path to the Python interpreter inside your
     `.venv`.
   - `PG_USER`, `PG_PASSWORD`, `PG_DATABASE` — your PostgreSQL
     credentials.
3. (Optional) Copy `config_example/postgres-agent.md` into
   `.continue/agents/` to use it as a custom agent prompt.
4. In VS Code: `Cmd+Shift+P` → **`Continue: Reload Config`**.
5. Open a new chat in **Agent Mode** (not Chat, not Edit).

### Example

Prompt:

> Use the pg-explain tool to analyze:
> `SELECT val FROM onek1 WHERE hundred BETWEEN 5 AND 55;`

Response:

```
✓ Continue used the pg-explain explain tool

Execution Time: 0.904 ms
Planning Time:  1.228 ms
Issues Found:   None

Analysis:
The plan shows an Index Only Scan on idx_onek1_hundred.
Heap Fetches: 0 — the visibility map is fresh, no heap lookups needed.
```

## Test fixtures

The repository ships a PostgreSQL extension
[`mcp_explain_tool`](fixtures/) that creates empty tables and
`fill_*` / `clear_*` procedures for every check. It is versioned
independently from the Python package.

```bash
cd fixtures
make install
```

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_all(1000000);
```

See [`fixtures/README.md`](fixtures/README.md) for details, including
the VACUUM policy and the autovacuum exceptions for tables that need a
stale visibility map or fake statistics.

## Usage examples

Real-world runs of `pg-explain-mcp` against PostgreSQL, one set per
check, with the raw tool output and analysis:

| Check                  | Examples                                                              |
|------------------------|-----------------------------------------------------------------------|
| `IndexScanCheck`       | healthy vs. stale visibility map                                      |
| `SeqScanCheck`         | with and without an index                                             |
| `DiskSpillSortCheck`   | in-memory vs. external merge, plus a *precise* `work_mem` variant     |
| `DiskSpillHashCheck`   | single-batch vs. multi-batch spill, plus a *precise* variant          |
| `NestedLoopCheck`      | 100 vs. 5000 inner iterations                                         |
| `BitmapHeapScanCheck`  | narrow vs. wide range on the same table                               |
| `EstimateMismatchCheck`| norm, norm+index, skewed, skewed-other-value                          |

See [`usage_examples/`](usage_examples/) for the full index, the test
environment, and prompting tips.

## Project structure

```
pg-explain-mcp/
├── src/
│   └── pg_explain_mcp/
│       ├── __init__.py
│       ├── server.py         # MCP entry point — exposes tools
│       ├── db.py             # connection + EXPLAIN + schema queries
│       ├── analyzer.py       # PlanCheck adapters + traversal
│       └── config.py         # SQLite-backed check registry
├── config/                   # SQLite config: schema, seed, checks.db
├── tests/                    # unit tests for checks and helpers
├── fixtures/                 # mcp_explain_tool PostgreSQL extension
├── usage_examples/           # real-world runs, one set per check
├── config_example/           # Continue.dev MCP + agent config
├── images/                   # screenshots
├── pyproject.toml
├── CHANGELOG.md
├── DISCLAIMER.md
├── README.md
└── LICENSE
```

## Roadmap

- **Additional checks** — `partition_pruning` for partitioned tables,
  `jit_decision` for expensive JIT compilation on short queries.
- **Multi-database support** — the `PlanCheck` interface and the
  SQLite config are database-agnostic in principle; the next step is
  a MySQL/MariaDB adapter and its own `plan_fields`/`checks` rows
  under `TARGET_DB_TYPE=mysql`.
- **Audit log for config changes** — record every `set_checker_value`
  and `reset_checker_value` call into a `param_history` table with
  timestamp, old value, and new value. Useful in shared deployments.

## Disclaimer

This project is a **diagnostic tool provided "as is"**. Recommendations
from the analyzer — or from an LLM assistant using it — are
suggestions, not guarantees. Always validate against your own database
before applying changes to a production system. `EXPLAIN ANALYZE`
**actually executes** the query; avoid running it against production
databases during peak hours.

See [`DISCLAIMER.md`](DISCLAIMER.md) for the full text.

## License

MIT — see [`LICENSE`](LICENSE) for details.

