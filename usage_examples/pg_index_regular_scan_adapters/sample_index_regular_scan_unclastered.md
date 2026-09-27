# sample: Index Scan with poor heap clustering

> trigger scenario for `IndexRegularScanCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_index_scan(5000000);
vacuum analyze mcp_explain_tool.data_index_scan_norm;
```

For a cold cache, either restart PostgreSQL or read enough
unrelated data to evict the pages of `data_index_scan_norm` from
shared buffers.

## context

Same table and index as
[`sample_index_regular_scan_norm.md`](sample_index_regular_scan_norm.md),
but with a larger `LIMIT` (100 000) so that the scan touches more
heap pages than fit comfortably in the cache.

The planner still chooses an **Index Scan** on `val` — that is the
right shape for `ORDER BY val LIMIT N`. But the heap is not clustered
by `val`: the MD5-derived values were inserted in random order, so
consecutive index entries point to widely scattered heap pages.

The result is heavy random I/O: **57 194 disk blocks read for
100 000 rows** — roughly 0.57 reads per row. `IndexRegularScanCheck`
fires at `INFO` level and suggests `CLUSTER` on the index.

## input query

```sql
select *
from mcp_explain_tool.data_index_scan_norm
order by val
limit 100000;
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
  "execution_time_ms": 961.447,
  "planning_time_ms": 8.679,
  "total_time_ms": 970.126,
  "issues": [
    {
      "severity": "info",
      "type": "index_scan_poor_clustering",
      "message": "Index Scan on 'idx_data_index_scan_norm_val' (data_index_scan_norm) read 57194 blocks from disk for 100000.0 rows. Poor heap clustering may be causing random I/O. Consider CLUSTER on this index.",
      "node": "Index Scan",
      "depth": 1,
      "parent_node": "Limit"
    }
  ],
  "issue_count": 1,
  "summary": "Found 1 issues (0 critical). Execution time: 961.45 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Limit",
      "actual_loops": 1,
      "actual_rows": 100000.0,
      "parallel_aware": false,
      "plan_rows": 100000,
      "shared_read_blocks": 57194
    },
    {
      "depth": 1,
      "node_type": "Index Scan",
      "actual_loops": 1,
      "actual_rows": 100000.0,
      "index": "idx_data_index_scan_norm_val",
      "parallel_aware": false,
      "plan_rows": 999996,
      "relation": "data_index_scan_norm",
      "shared_read_blocks": 57194
    }
  ]
}
```

## analysis

| metric             | value                          |
|--------------------|--------------------------------|
| execution time     | 961.45 ms                      |
| rows returned      | 100,000                        |
| access method      | `Index Scan`                   |
| index              | `idx_data_index_scan_norm_val` |
| shared read blocks | **57,194**                     |
| blocks per row     | ~0.57                          |
| issues             | 1 (INFO)                       |

### why `IndexRegularScanCheck` fires

The check requires **both** conditions:

```python
if actual_rows < self.min_rows:        # 100000 >= 1000 → passes
    return False
return read_blocks >= self.min_disk_blocks   # 57194 >= 100 → passes
```

The scan reads almost 60 000 disk blocks to return 100 000 rows. When
rows per block approaches 1, the heap is essentially unclustered with
respect to the index — every index step is a fresh random page. That
is the signal the check reports, at `INFO` severity (a hint, not a
defect).

### why `EstimateMismatchCheck` stays silent

The `Index Scan` node shows `plan_rows: 999996` vs
`actual_rows: 100000`. That is a ~10× ratio and would normally be on
the edge of `threshold_ratio = 10`. But the node's parent is `Limit`,
and the planner's `plan_rows` under a `Limit` is the **full-scan**
estimate, not the truncated one. The check therefore skips it:

```python
if parent_type == "Limit":
    return None
```

This is the same suppression as in the norm file — the guard is what
keeps every `ORDER BY ... LIMIT` query from producing a false
positive.

### why `IndexOnlyScanCheck` stays silent

No `Index Only Scan` node. `SELECT *` needs columns the index does not
cover, so the planner can only use a plain `Index Scan`.

### why `SeqScanCheck` stays silent

No `Seq Scan` node. Plan is `Limit → Index Scan`.

## the LLM's recommendations

The assistant produced a set of fix suggestions, all aligned with the
check's `INFO` message:

1. **`CLUSTER` the table** on the index — the direct fix. Expected
   effect: many fewer blocks read, execution time well under 100 ms.
2. **`pg_repack`** — an online alternative to `CLUSTER` when the
   table has frequent writes and an exclusive lock is unacceptable.
3. **`fillfactor = 90`** — reduces future page splits that degrade
   clustering after it has been restored.
4. **Covering index with `INCLUDE`** — turns the plan into an
   Index Only Scan and eliminates heap access entirely, at the cost
   of a wider index.

All four are legitimate; the right choice depends on the workload.

## what this proves

- `IndexRegularScanCheck` measures disk I/O, not plan shape. The
  same plan with a warm cache reads 0 blocks and stays silent (see
  the norm file). With a cold cache and enough rows, it fires.
- `INFO` is the right severity here: the query is correct, the plan
  is correct, and the fix (`CLUSTER`) is a maintenance operation,
  not a code change.
- the `parent_type == "Limit"` guard in `EstimateMismatchCheck`
  prevents the full-scan estimate under `Limit` from being treated
  as a real misestimate.

## test environment

- **LLM**: `qwen 3.8 27b-splash` via LM Studio
- **PostgreSQL**: 18.6

Qwen produces structured, report-style answers — tables, headers,
numbered checklists. The tool output above is identical regardless of
model; only the surrounding prose changes.

## side-by-side with the norm case

| metric             | [norm](sample_index_regular_scan_norm.md) | trigger (this file) |
|--------------------|-------------------------------------------|---------------------|
| `LIMIT`            | 5000                                      | 100000              |
| cache state        | warm                                      | cold                |
| shared read blocks | 0                                         | **57,194**          |
| execution time     | 24.99 ms                                  | 961.45 ms           |
| `IndexRegularScanCheck` | silent                              | **INFO**            |

