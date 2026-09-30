# sample: full tuning workflow across two MCP servers

> end-to-end example: diagnosis with `pg-explain-mcp`, fix execution
> via a general-purpose PostgreSQL MCP server, verification back in
> `pg-explain-mcp`.

## why this example exists

Every other example in `usage_examples/` shows a single tool in
isolation. This one shows what the two MCP servers are meant to do
**together**:

- **`pg-explain-mcp`** — read-only diagnostic. Explain plans,
  detect issues, inspect indexes, column statistics, and relation
  metadata.
- **A general-purpose PostgreSQL MCP server** — read-write DDL.
  Runs `ANALYZE`, `CREATE INDEX`, `ALTER TABLE`, etc.

The assistant decides which server to call based on intent. It never
tries to run DDL through `pg-explain-mcp` (which is read-only), and
it never tries to interpret an execution plan through the generic
server (which has no plan-aware tooling).

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_estimate_mismatch(1000000);
```

At this point `data_estimate_mismatch_skewed` has:
- 1M rows.
- `autovacuum_enabled = false` (so stats stay as we set them).
- Fake `most_common_vals = {42}`, `most_common_freqs = {0.95}` set
  by `pg_restore_attribute_stats`.
- No index on `val` (only the primary key on `id`).

## input query

```sql
select *
from mcp_explain_tool.data_estimate_mismatch_skewed
where val = 42;
```

## prompt to the assistant

> analyze this query and fix any issues you find.
> run whatever is needed.

## step 1 — diagnosis

### `pg-explain explain`

The plan is a single `Seq Scan`:

| metric         | value     |
|----------------|-----------|
| `plan_rows`    | 950 000   |
| `actual_rows`  | 9 896     |
| ratio          | **x96**   |
| execution      | 229.5 ms  |
| blocks read    | 30 210    |

`EstimateMismatchCheck` fires:

```json
{
  "severity": "warning",
  "type": "estimate_mismatch",
  "message": "Planner misestimated cardinality on 'Seq Scan' on 'data_estimate_mismatch_skewed': expected 950000, got 9896.0 (ratio x96.0). Investigate why: stale statistics, a non-sargable predicate on the column, or a distribution not covered by the column histogram. ANALYZE helps only in the first case.",
  "depth": 0,
  "parent_node": ""
}
```

### `pg-explain list_indexes`

Confirms the only index is the primary key on `id` — no index on
`val`. So even a correct estimate would not switch to an index scan
yet.

### `pg-explain list_column_stats`

The smoking gun:

```
mcp_explain_tool.data_estimate_mismatch_skewed.val
  null_frac:            0.0
  avg_width:            4
  n_distinct:           100.0
  correlation:          0.014863347
  most_common_vals:     {42}
  most_common_freqs:    {0.95}
  histogram_bounds:     {0,1,2,...,41,...}
```

`most_common_freqs = {0.95}` tells the planner that 95% of rows have
`val = 42`. That is why it estimates 950 000 rows.

> **note about this fixture.** In reality the table is uniformly
> distributed — the MCV entry was written manually by
> `pg_restore_attribute_stats`. The assistant cannot know that; from
> its point of view, the statistics simply disagree with the actual
> data. The recommended fix (`ANALYZE`) is exactly right: it will
> overwrite the fake MCV with the real distribution.

### `pg-explain list_relation_stats`

Confirms the table has 1M rows, no dead tuples, `n_mod_since_analyze:
1000000`, and `last_analyze` set — but no `last_autoanalyze`
(autovacuum is disabled on this fixture).

## step 2 — fix #1: refresh statistics

The assistant switches to the **general-purpose** MCP server and runs
DDL. `pg-explain-mcp` is read-only and cannot do this itself.

```
Call the postgres-test1 execute_query tool with:
ANALYZE mcp_explain_tool.data_estimate_mismatch_skewed;
```

> **note.** in the actual session the assistant first tried
> `execute_query` in the default `safe` mode (read-only) and was
> rejected. It then called `connect_database` with `readwrite`, and
> finally `full` to allow DDL. The default `pg-explain-mcp` connection
> is unaffected — it stays read-only throughout.

## step 3 — re-explain

### `pg-explain explain` (after `ANALYZE`)

```json
{
  "issue_count": 0,
  "plan_nodes": [
    {"node_type": "Gather", "plan_rows": 9867, ...},
    {"node_type": "Seq Scan",
     "plan_rows": 9867,
     "actual_rows": 9896,
     "filter": "(val = 42)", ...}
  ]
}
```

The estimate is now accurate. The assistant notices that the plan
shape did not improve — it is still a `Parallel Seq Scan`, because
there is no index on `val` to switch to.

| metric         | before  | after ANALYZE |
|----------------|---------|---------------|
| `plan_rows`    | 950 000 | 9 867         |
| `actual_rows`  | 9 896   | 9 896         |
| ratio          | x96     | x1.0          |
| execution      | 229 ms  | 170 ms        |
| issues         | 1       | 0             |

The `estimate_mismatch` warning is gone. The query is still slow
because a full table scan is the only option available.

## step 4 — fix #2: create an index

Back to the general-purpose server:

```
Call the postgres-test1 execute_query tool with:
CREATE INDEX idx_data_estimate_mismatch_skewed_val
    ON mcp_explain_tool.data_estimate_mismatch_skewed (val);
```

## step 5 — verify

### `pg-explain explain` (after `CREATE INDEX`)

```json
{
  "issue_count": 0,
  "plan_nodes": [
    {"node_type": "Bitmap Heap Scan", "plan_rows": 9933, "actual_rows": 9990, ...},
    {"node_type": "Bitmap Index Scan",
     "index_name": "idx_data_estimate_mismatch_skewed_val", ...}
  ]
}
```

The plan switched to a `Bitmap Index Scan` feeding a `Bitmap Heap
Scan`. No issues detected.

## results summary

| metric          | original | after ANALYZE | after ANALYZE + INDEX |
|-----------------|----------|---------------|-----------------------|
| plan shape      | Seq Scan | Parallel Seq Scan | Bitmap Heap Scan  |
| index used      | —        | —             | `idx_..._val`         |
| blocks read     | 30 210   | 29 860        | 8 207                 |
| I/O time        | 65 ms    | 308 ms        | 21 ms                 |
| execution time  | 229 ms   | 170 ms        | **67 ms**             |
| issues          | 1        | 0             | 0                     |

Overall: **229 ms → 67 ms, ~3.4× faster** (cold cache; warm-cache
runs are expected in the ~5–10 ms range since the index lookup itself
took 5.7 ms).

## what this example demonstrates

### 1. Two servers, two responsibilities

| task                              | server                    |
|-----------------------------------|---------------------------|
| Read plans, detect issues         | `pg-explain-mcp`         |
| Inspect indexes / stats / schema  | `pg-explain-mcp`         |
| Run `ANALYZE`, `CREATE INDEX`     | general-purpose MCP       |
| Re-verify the fix                 | `pg-explain-mcp`         |

The assistant never asks `pg-explain-mcp` for DDL — the tool is
read-only by design. It also never asks the generic server to
interpret a plan. Each question goes to the server built for it.

### 2. Iterative diagnosis, not one-shot

The assistant did **not** guess "add an index". It:

1. Confirmed the estimate was wrong (`estimate_mismatch`).
2. Confirmed there was no index on `val` (`list_indexes`).
3. Confirmed the cause was the MCV entry (`list_column_stats`).
4. Fixed the statistics first (`ANALYZE`).
5. Re-measured — saw the estimate was fixed but the plan unchanged.
6. Added the index — second, structural fix.
7. Re-measured again.

Two separate problems (bad stats, missing index), two separate fixes,
two verification steps.

### 3. Statistics and indexes are different problems

A common mistake is to conflate "the planner estimates wrong" with
"we need an index". Here:

- **Bad stats alone** → correct estimate, still slow scan.
- **Index alone** (with bad stats) → the index would be **ignored**,
  because the planner would still think 95% of rows match.
- **Both fixes** → fast plan.

The `estimate_mismatch` warning was the entry point, not the whole
story.

### 4. The assistant uses facts, not guesses

Every recommendation was grounded in a tool call:

- "95% of rows" ← `list_column_stats.most_common_freqs`
- "no index on val" ← `list_indexes`
- "`n_mod_since_analyze` = 1M" ← `list_relation_stats`
- "~10K actual rows" ← `explain.actual_rows`

Nothing was inferred from table names or general knowledge.

## test environment

- **LLM**: `qwen 3.8 27b-splash` via LM Studio
- **PostgreSQL**: 18.6
- **MCP servers**:
  - `pg-explain-mcp` (this project, read-only)
  - `universal-db-mcp` (read-write, DDL-capable)

## see also

- [`../pg_estimate_mismatch/`](../pg_estimate_mismatch/) — the
  same table, in isolated before/after examples.
- [`../pg_index_regular_scan_adapters/`](../pg_index_regular_scan_adapters/) —
  the index-access side of the same story.

