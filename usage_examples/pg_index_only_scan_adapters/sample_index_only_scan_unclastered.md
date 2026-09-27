# sample: Index Only Scan with a stale visibility map

> trigger scenario for `IndexOnlyScanCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_index_scan(5000000);

-- Deliberately do NOT vacuum data_index_scan_unclastered.
-- Autovacuum is disabled on it — see fixtures/README.md.
```

The `_unclastered` table is populated by `fill_index_scan`, which
runs 20 rounds of UPDATE/DELETE/INSERT churn and leaves the heap with
a large dead-tuple ratio. Autovacuum is disabled on this table, so the
visibility map stays stale until a manual `VACUUM`.

## context

`data_index_scan_unclastered` has a plain B-tree index on `val`. The
query selects only `val`, so the planner can use an Index Only Scan —
in principle, the index alone answers the query.

But the visibility map is stale: the engine cannot trust the index to
decide tuple visibility, so it falls back to the heap for each row.
The plan still says `Index Only Scan`, but the `Heap Fetches` counter
is non-zero — the scan is effectively a plain Index Scan in disguise.

`IndexOnlyScanCheck` fires at `WARNING` level and suggests `VACUUM`.

## input query

```sql
select val
from mcp_explain_tool.data_index_scan_unclastered
where val between '000' and '001'
limit 10000;
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
  "execution_time_ms": 4.962,
  "planning_time_ms": 2.871,
  "total_time_ms": 7.833,
  "issues": [
    {
      "severity": "warning",
      "type": "index_only_scan_stale_vm",
      "message": "Index Only Scan on 'idx_data_index_scan_unclastered_val' (data_index_scan_unclastered) performed 594 heap fetches for 440.0 rows (135.0%). The visibility map is stale. Run VACUUM, or consider CLUSTER to improve locality.",
      "node": "Index Only Scan",
      "depth": 0,
      "parent_node": ""
    }
  ],
  "issue_count": 1,
  "summary": "Found 1 issues (1 critical). Execution time: 4.96 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Index Only Scan",
      "actual_loops": 1,
      "actual_rows": 440.0,
      "heap_fetches": 594,
      "index": "idx_data_index_scan_unclastered_val",
      "parallel_aware": false,
      "plan_rows": 111,
      "relation": "data_index_scan_unclastered",
      "shared_read_blocks": 0
    }
  ]
}
```

## analysis

| metric             | value                          |
|--------------------|--------------------------------|
| execution time     | 4.96 ms                        |
| rows returned      | 440                            |
| access method      | `Index Only Scan`              |
| index              | `idx_data_index_scan_unclastered_val` |
| heap fetches       | **594**                        |
| heap fetch ratio   | **135%**                       |
| issues             | 1 (WARNING)                    |

### why `IndexOnlyScanCheck` fires

The check has three conditions:

```python
if actual_rows < min_rows:           # 440 < 100 → False
    return False
if heap_fetches == 0:                # 594 != 0 → False
    return False
return ratio >= heap_fetch_ratio     # 1.35 >= 0.10 → True
```

`actual_rows` (440) is above `min_rows` (100), `heap_fetches` is
non-zero, and the ratio (1.35) exceeds `heap_fetch_ratio` (0.10).
All three hold, so the check emits a `WARNING`.

### the ratio can exceed 100%

`594 / 440 = 1.35` — more heap fetches than rows returned. This is
normal when the visibility map is stale: a single heap fetch can
read a page that contains several candidate tuples, and the page
may be revisited as the scan walks the index. The counter counts
fetch operations, not distinct pages.

A ratio **above 1** is a strong signal: the engine is doing more
heap work than the query's cardinality warrants.

### why `IndexRegularScanCheck` stays silent

The plan contains no plain `Index Scan` node. Both index checks are
scoped to one node type each: `IndexOnlyScanCheck` to `Index Only
Scan`, `IndexRegularScanCheck` to `Index Scan`. Only one of them is
relevant per plan.

### why `EstimateMismatchCheck` stays silent

Planner estimated 111 rows, actual 440. Ratio ~4×, below
`threshold_ratio = 10`. Also `min_rows = 1000` would suppress it
even if the ratio were higher. The mismatch is real but not
material.

## recommendation

```sql
vacuum analyze mcp_explain_tool.data_index_scan_unclastered;
```

After `VACUUM`, the visibility map is rebuilt and subsequent Index
Only Scans skip the heap. `Heap Fetches` should drop to 0, and the
check stays silent — see
[`sample_index_only_scan_norm.md`](sample_index_only_scan_norm.md)
for the clean state.

`CLUSTER` is a stronger fix if the query also suffers from poor
clustering: it physically reorders the heap to match the index. But
for a plain stale-VM case, `VACUUM` is enough and cheaper.

## comparison with norm

| metric             | [norm](sample_index_only_scan_norm.md) | trigger (this file) |
|--------------------|----------------------------------------|---------------------|
| visibility map     | fresh                                  | **stale**           |
| heap fetches       | 0                                      | **594**             |
| heap fetch ratio   | 0%                                     | **135%**            |
| execution time     | 0.64 ms                                | 4.96 ms             |
| `IndexOnlyScanCheck` | silent                               | **WARNING**         |

Same query, same index, same table shape — the only difference is
the state of the visibility map.

## what this proves

- `IndexOnlyScanCheck` reports stale visibility maps by observing
  the `Heap Fetches` counter on an `Index Only Scan` node. The
  counter is zero when the VM is fresh and non-zero otherwise.
- a ratio above 1 is not a bug in the counter: it reflects the
  engine doing more heap work than the query's cardinality implies.
- `min_rows` applies to `actual_rows`, not to `heap_fetches`. A small
  scan with a proportionally large number of heap fetches is still
  worth flagging — as long as the absolute row count is above the
  floor.
- the fix (`VACUUM`) is a maintenance operation, not a code change.
  The check's `WARNING` severity reflects that.

## test environment

- **LLM**: `qwen 3.8 27b-splash` via LM Studio
- **PostgreSQL**: 18.6

Qwen produces structured, report-style answers — a table, a bulleted
list of fix options, and a short recommendation. The raw tool output
above is model-independent; only the surrounding prose changes.

