# sample: Index Scan from warm cache — no disk I/O

> baseline scenario for `IndexRegularScanCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_index_scan(5000000);
vacuum analyze mcp_explain_tool.data_index_scan_norm;
```

For this scenario the table should be **in cache** — either because
it was just read, or because the buffer pool is large enough. If you
want to reproduce the trigger case, flush the cache first (see
[`sample_index_regular_scan_unclastered.md`](sample_index_regular_scan_unclastered.md)).

## context

`data_index_scan_norm` has a plain B-tree index on `val`. The query
selects every column (`SELECT *`) and orders by `val` with a `LIMIT`.
Because the index covers `val` in sorted order, the planner uses it
directly — no separate `Sort` node, and `Limit` stops the scan after
5 000 rows.

`IndexRegularScanCheck` inspects `Index Scan` nodes and fires when
the scan reads **many disk blocks** for a **significant number of
rows**. Here the scan reads **0 disk blocks** — everything was in
shared buffers — so the check stays silent.

This is the "good" state: an Index Scan whose heap access is cheap
because the pages are already cached. The companion file
`sample_index_regular_scan_unclastered.md` shows the same plan with a
cold cache, where `shared_read_blocks` jumps and the check fires.

## input query

```sql
select *
from mcp_explain_tool.data_index_scan_norm
order by val
limit 5000;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "checks_applied": [
    "SeqScanCheck",
    "EstimateMismatchCheck",
    "DiskSpillSortCheck",
    "DiskSpillHashCheck",
    "NestedLoopCheck",
    "BitmapHeapScanCheck",
    "IndexRegularScanCheck",
    "IndexOnlyScanCheck",
    "PartitionPruningCheck",
    "NonSargableCheck"
  ],
  "execution_time_ms": 24.991,
  "planning_time_ms": 5.581,
  "total_time_ms": 30.572,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 24.99 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Limit",
      "actual_loops": 1,
      "actual_rows": 5000.0,
      "parallel_aware": false,
      "plan_rows": 5000,
      "shared_read_blocks": 0
    },
    {
      "depth": 1,
      "node_type": "Index Scan",
      "actual_loops": 1,
      "actual_rows": 5000.0,
      "index": "idx_data_index_scan_norm_val",
      "parallel_aware": false,
      "plan_rows": 999996,
      "relation": "data_index_scan_norm",
      "shared_read_blocks": 0
    }
  ]
}
```

## analysis

| metric             | value                          |
|--------------------|--------------------------------|
| execution time     | 24.99 ms                       |
| rows returned      | 5,000                          |
| access method      | `Index Scan`                   |
| index              | `idx_data_index_scan_norm_val` |
| shared read blocks | **0**                          |
| issues             | 0                              |

### why `IndexRegularScanCheck` stays silent

The check fires only when **both** conditions hold:

```python
if actual_rows < self.min_rows:        # 5000 >= 1000 → passes
    return False
return read_blocks >= self.min_disk_blocks   # 0 >= 100 → fails
```

`shared_read_blocks = 0` — nothing was read from disk. All pages
were already in shared buffers, so the scan never touched the disk
subsystem. Without disk I/O there is no clustering problem to report.

### why `EstimateMismatchCheck` stays silent

`Index Scan` shows `plan_rows: 999996` vs. `actual_rows: 5000` —
apparently a 200× mismatch. But the node's parent is `Limit`, and
`Limit` truncates the scan before it reaches the estimated row count.
The planner's `plan_rows` on an `Index Scan` under a `Limit` is the
**full-scan estimate**, not the truncated one.

`EstimateMismatchCheck` explicitly skips nodes whose parent is
`Limit`:

```python
if parent_type == "Limit":
    return None
```

Without that guard the check would fire on every `ORDER BY ... LIMIT`
query, producing nothing but noise. This case is why the guard
exists.

### why `IndexOnlyScanCheck` stays silent

Not applicable — the plan contains no `Index Only Scan` node. The
query uses `SELECT *`, so the index (which covers only `val`) cannot
satisfy it; the planner must visit the heap, and uses a plain
`Index Scan`.

### why `SeqScanCheck` stays silent

No `Seq Scan` node. The plan consists of `Limit` and `Index Scan`.

## what this proves

- `IndexRegularScanCheck` measures disk I/O, not plan shape. The
  same `Index Scan` shape with a cold cache will fire the check;
  from a warm cache it will not.
- `EstimateMismatchCheck` correctly suppresses the full-scan estimate
  under `Limit`. Without that guard, every `ORDER BY ... LIMIT`
  query would produce a false positive.
- `plan_rows` on a node is **not** the row count the query will
  return — it is what the node would return if nothing above it
  stopped it. `Limit` is the only common case where the difference
  matters, and the walker now passes `parent_type` so checks can
  react.

## test environment

- **LLM**: `qwen 3.8 27b-splash` via LM Studio
- **PostgreSQL**: 18.6

## next step

see
[`sample_index_regular_scan_unclastered.md`](sample_index_regular_scan_unclastered.md)
for the same plan with a cold cache — `shared_read_blocks` rises,
and `IndexRegularScanCheck` fires.

