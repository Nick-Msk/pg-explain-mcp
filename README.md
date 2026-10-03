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
- **`list_relation_info`** — `pg_class` metadata: sizes, row/page
  counts, column/index counts, owner, persistence, tablespace, comment.
- **`list_relation_stats`** — `pg_stat_user_tables` counters combined
  with `pg_class` estimates (`reltuples`, `relpages`, `relallvisible`).
- **`list_column_stats`** — per-column planner statistics from
  `pg_stats`: `null_frac`, `avg_width`, `n_distinct`, `correlation`,
  `most_common_vals`, `most_common_freqs`, `histogram_bounds`.
- **`show_params`**, **`set_checker_value`**, **`reset_checker_value`** —
  runtime check management without editing the SQLite database by hand.
- **`ping`** — health check.
- **`history_checker_values`** — returns the audit log of config
  changes (`config_audit`), most recent first. Filters: `table_name`,
  `column_name`, `optype` (`'I'` / `'U'` / `'D'`), `count` (0 = all),
  `include_seed` (default `False` — seed rows are hidden). Each row
  carries old/new values and a `who` tag (`system`, `llm`, `seed`).

### Checks

Every `explain` response includes an `issues` array. Each entry is
produced by an independent, pluggable check — a subclass of
`ParsedPlanCheckBase`:

| Check                     | What it reports                                         |
|---------------------------|---------------------------------------------------------|
| `SeqScanCheck`            | Sequential scan that discards most of what it reads     |
| `IndexRegularScanCheck`   | Index Scan reading too many blocks — poor clustering    |
| `IndexOnlyScanCheck`      | Index Only Scan with a stale visibility map             |
| `BitmapHeapScanCheck`     | Large Bitmap Heap Scan                                  |
| `DiskSpillSortCheck`      | Sort spilling to disk (`external merge`)                |
| `DiskSpillHashCheck`      | Hash operation using multiple batches                   |
| `NestedLoopCheck`         | Nested Loop with a high number of inner iterations      |
| `EstimateMismatchCheck`   | Planner cardinality misestimate                         |
| `PartitionPruningCheck`   | `Append` over many partitions — pruning may have failed |
| `NonSargableCheck`        | Predicate wraps an indexed column in a function         |
| `JitDecisionCheck`        | JIT compilation overhead exceeds the plan's useful work |

To add a new check, subclass `ParsedPlanCheckBase` in
`src/pg_explain_mcp/analyzer.py`, implement the three phases
(`gather_info` → `validate_rule` → `generate_msg`), register the class
in `src/pg_explain_mcp/config.py`, and add its default parameters to
`config/seed.sql`. Each subclass declares `name`, `type`, and
`PARAMS = {param: type}`; values are loaded from the SQLite registry
and coerced at construction time.

### Structured plan output

`explain` returns a compact `plan_nodes` tree alongside `issues`.
Only the fields that matter for reasoning are kept:

- `node_type`, `relation_name`, `index_name`
- `actual_rows`, `plan_rows`, `actual_loops`
- `rows_removed_by_filter`, `heap_fetches`, `shared_read_blocks`
- `sort_method`, `sort_space_type`, `sort_space_used`
- `hash_batches`, `peak_memory_usage`
- `parallel_aware`

The report also carries `root_meta` — top-level EXPLAIN metadata
(`Planning`, `Planning Time`, `JIT`, `Triggers`, `Execution Time`) —
so an LLM can read `JIT.Timing.Total` without a second EXPLAIN callThe report also carries `root_meta` — top-level EXPLAIN metadata
(`Planning`, `Planning Time`, `JIT`, `Triggers`, `Execution Time`) —
so an LLM can read `JIT.Timing.Total` without a second EXPLAIN call..

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
three console scripts: `pg-explain-mcp` (the MCP server),
`pg-explain-parse` (plan parser / CLI), and `pg-explain-config`
(SQLite registry management).

### Verify

```bash
python -c "from pg_explain_mcp import server; print('OK')"
# → OK

pytest -v
```

## Command-line tools

### `pg-explain-parse`

Runs `EXPLAIN` on a query and prints the plan tree, or parses a saved
JSON plan offline. Useful for inspecting plans without an MCP client:

```bash
# SQL as argument
pg-explain-parse "select * from t limit 10"

# SQL from a file
pg-explain-parse query.sql

# SQL from stdin
cat query.sql | pg-explain-parse

# Parse a saved plan JSON instead
pg-explain-parse --plan plan.json
cat plan.json | pg-explain-parse --plan -

# Flags
pg-explain-parse --json              # flat node list as JSON
pg-explain-parse --no-analyze        # plan only, don't execute
pg-explain-parse --no-buffers        # skip BUFFERS
pg-explain-parse --all-fields        # keep zero-valued numeric fields
pg-explain-parse --marker-tabs 3     # tab padding in the text tree
pg-explain-parse --with-meta         # wrap JSON as {meta, plan}

python -m pg_explain_mcp.config --init   # rebuild from schema + seed
python -m pg_explain_mcp.config --show   # print current state

--init drops and recreates the seed tables only. The audit log
(config_audit) and the writer tag (_audit_session) survive, so
the history of config changes is preserved across rebuilds. Note
that --init resets every param to its seed default — see
Audit log.

## Configuration

### Connection

The server reads PostgreSQL connection parameters from environment
variables:

| Variable         | Default     | Description                |
|------------------|-------------|----------------------------|
| `PG_HOST`        | `localhost` | PostgreSQL host            |
| `PG_PORT`        | `5432`      | PostgreSQL port            |
| `PG_USER`        | `postgres`  | Database user              |
| `PG_PASSWORD`    | —           | Database password          |
| `PG_DATABASE`    | `postgres`  | Database name              |
| `TARGET_DB_TYPE` | `postgres`  | Database (pg/orcl/mysql..) |

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


---

### Plan fields

`plan_fields` controls which EXPLAIN fields appear in `plan_nodes` and
in the text tree. Each row has a raw EXPLAIN name, a compact `key`,
and an `enabled` mode:

| `enabled` | Meaning                                                        |
|-----------|----------------------------------------------------------------|
| `0`       | Hide the field (`Parallel Aware: false`, `Disabled: false`, `Async Capable: false`). |
| `1`       | Keep the field even when its value is a numeric zero (`Heap Fetches`, `Rows Removed by Filter`, `Shared Read Blocks`, `Temp Read Blocks`, `Temp Written Blocks`). |
| `999`     | Auto: keep the field, drop numeric zeros (the default).        |

Fields not listed in `plan_fields` default to `999` and get an
auto-derived `snake_case` key.

To add a new field:

```bash
sqlite3 config/checks.db \
  "insert into plan_fields (database, raw, key, enabled)
   values ('postgres', 'Total Cost', 'total_cost', 1);"

## Usage with Continue.dev

Ready-to-use configuration files are available in
[`config_mcp/`](config_mcp/):

- [`config_mcp/mcpServers/pg-explain.yaml`](config_mcp/mcpServers/pg-explain.yaml)
  — MCP server registration.
- [`config_mcp/postgres-agent.md`](config_mcp/postgres-agent.md)
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
[`config_mcp/postgres-agent.md`](config_mcp/postgres-agent.md)
describes both and includes guidance on when to prefer one over the
other.

### Audit log

Every change to the config database is recorded in `config_audit`.
The table is populated automatically by triggers — no application
code writes to it directly.

**What is tracked:**

| Table          | Column     | Op recorded on        |
|----------------|------------|-----------------------|
| `checks`       | `enabled`  | insert / update / delete |
| `check_params` | `value`    | insert / update / delete |
| `plan_fields`  | `enabled`  | insert / update / delete |
| `plan_fields`  | `key`      | insert / update / delete |

**Row shape:** `(id, table_name, column_name, optype, ts, old_value,
new_value, who)`. `optype` is `'I'` (insert), `'U'` (update), or
`'D'` (delete) — so a delete and an update-that-sets-NULL are
distinguishable. `ts` is UTC ISO-8601 with milliseconds.

**Writer tags** (`who`):

| Tag        | Set by                                                         |
|------------|----------------------------------------------------------------|
| `system`   | Default. Manual `sqlite3` edits, `--init` overhead.            |
| `llm`      | MCP calls to `set_checker_value` / `reset_checker_value`.      |
| `seed`     | `seed.sql` inserts during `--init`. Hidden by default in `history_checker_values`. |

**Reading the log via MCP:**

history_checker_values() # all non-seed rows
history_checker_values(table_name="check_params") # one table
history_checker_values(optype="U") # updates only
history_checker_values(count=20) # newest 20
history_checker_values(include_seed=True) # include --init noise

**Reading the log via SQL:**

```bash
sqlite3 config/checks.db \
  "select ts, optype, table_name, column_name, old_value, new_value, who
   from config_audit order by id desc limit 20;"
Survives --init. --init drops and rebuilds the seed tables
(checks, check_params, plan_fields, tags, databases) but
leaves config_audit and _audit_session intact. This makes it
possible to answer "who changed this, and when?" across rebuilds.

Caveat: --init resets every param to its seed default. If you
had a non-default value in check_params and you re-run --init,
the value is lost — but the change that produced it is preserved in
config_audit. Before running --init in a shared environment,
check history_checker_values() for recent edits.

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

1. Copy `config_mcp/mcpServers/pg-explain.yaml` into your
   workspace's `.continue/mcpServers/` directory.
2. Replace the placeholders:
   - `command:` — full path to the Python interpreter inside your
     `.venv`.
   - `PG_USER`, `PG_PASSWORD`, `PG_DATABASE` — your PostgreSQL
     credentials.
3. (Optional) Copy `config_mcp/postgres-agent.md` into
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
The plan shows an Index Only Scan on `idx_onek1_hundred`.
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
| `IndexOnlyScanCheck`   | healthy vs. stale visibility map                                      |
| `IndexRegularScanCheck`| warm vs. cold cache
| `SeqScanCheck`         | with and without an index                                             |
| `DiskSpillSortCheck`   | in-memory vs. external merge, plus a *precise* `work_mem` variant     |
| `DiskSpillHashCheck`   | single-batch vs. multi-batch spill, plus a *precise* variant          |
| `NestedLoopCheck`      | 100 vs. 5000 inner iterations                                         |
| `BitmapHeapScanCheck`  | narrow vs. wide range on the same table                               |
| `EstimateMismatchCheck`| norm, norm+index, skewed, skewed-other-value                          |
| `PartitionPruningCheck`| range predicate vs. non-sargable predicate on partitioned table       |
| `NonSargableCheck`     | sargable vs. non-sargable predicate on indexed column                 |
| JitDecisionCheck | 5M-row aggregate (JIT pays off) vs. 100-row query (JIT dominates) |

See [`usage_examples/`](usage_examples/) for the full index, the test
environment, and prompting tips.

## Project structure

```
pg-explain-mcp/
├── src/
│   └── pg_explain_mcp/
│       ├── __init__.py
│       ├── server.py         # MCP entry point — exposes tools
│       ├── parse.py          # pg-explain-parse CLI
│       ├── db.py             # connection + EXPLAIN + schema queries
│       ├── analyzer.py       # PlanNode, checks, new_* pipeline
│       └── config.py         # SQLite-backed check registry + CLI
├── config/                   # SQLite config: schema, seed, checks.db
├── tests/                    # unit tests for checks and helpers
├── fixtures/                 # mcp_explain_tool PostgreSQL extension
├── usage_examples/           # real-world runs, one set per check
├── config_mcp/           # Continue.dev MCP + agent config
├── images/                   # screenshots
├── pyproject.toml
├── CHANGELOG.md
├── DISCLAIMER.md
├── README.md
└── LICENSE
```

## Roadmap

### Additional checks

- **`RepeatedScanCheck`** — report the same relation scanned more
  than once within a single plan (via CTEs, subqueries, or lateral
  joins, not self-joins). Often signals that CTE materialisation or
  a temp table would reduce I/O.

### Database health checker

A second kind of check, alongside plan checks — `health_check` —
that inspects the database as a whole instead of a single query.

Health checks live in the **same** SQLite registry as plan checks,
distinguished by a new `check_type` column (`'PLAN'` or `'HEALTH'`).
This means they are configured, disabled, and tuned with the same
tools: `show_params`, `set_checker_value`, `reset_checker_value`,
and the `TARGET_DB_TYPE` scope all apply without change.

Candidate checks, each independently toggleable:

1. **Tablespace free space.** Warn when any tablespace has less than
   a configurable percentage of free space.
2. **Invalid objects.** Report indexes with `indisvalid = false` and
   constraints with `convalidated = false`.
3. **`plpgsql_check` integration.** If the extension is installed,
   run `plpgsql_check_function()` over every procedure and function
   in the target schema. If the extension is missing, skip with an
   INFO-level note.
4. **Bloat estimation.** Compare `n_dead_tup` to `n_live_tup` in
   `pg_stat_user_tables` and flag tables above a configurable ratio.
5. **Connection and lock pressure.** Report long-running
   transactions and locks held beyond a configurable interval.

Implementation shape:

- new `HealthCheck` protocol in `analyzer.py` — same `Issue` type,
  same `name` / `type` attributes, but `check()` takes no arguments
  and reads from `pg_catalog` / `pg_stat_*` directly;
- `CheckRegistry.load_health()` filtering by `check_type = 'HEALTH'`;
- a single new MCP tool `health_check` returning a report with
  `checks_applied` and `issues`, mirroring the `explain` output shape.

### Multi-database support

The analyzer interface and the SQLite config are database-agnostic
in principle. The next step is a MySQL/MariaDB adapter and its own
`plan_fields` / `checks` rows under `TARGET_DB_TYPE=mysql`.

### Audit log for config changes

Record every `set_checker_value` and `reset_checker_value` call into
a `param_history` table with timestamp, old value, and new value.
Useful in shared deployments.

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

