# sample: Bitmap Heap Scan on a wide range

> scenario 2 of 2 for `BitmapHeapScanCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_bitmap_heap_scan(5000000);
```

## context

Same table as scenario 1, but the range is wider
(`500000..540000`, ~4 % selectivity). The planner still prefers a
Bitmap Heap Scan over a Sequential Scan — but now the scan returns
~200k rows, which crosses the check's threshold.

`BitmapHeapScanCheck` fires at **`INFO`** level. Bitmap Heap Scan is
not a problem in itself; it is the right choice for medium
selectivity. The check is a heads-up that I/O grows with the number
of heap blocks touched.

## input query

```sql
select *
from mcp_explain_tool.data_bitmap_heap_scan_norm
where val between 500000 and 540000;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 1299.573,
  "planning_time_ms": 2.864,
  "total_time_ms": 1302.437,
  "issues": [
    {
      "severity": "info",
      "type": "bitmap_heap_scan",
      "message": "Bitmap Heap Scan on 'data_bitmap_heap_scan' processed 199943.0 rows. Consider a composite index or partitioning to reduce the number of heap fetches.",
      "node": "Bitmap Heap Scan"
    }
  ],
  "issue_count": 1,
  "summary": "Found 1 issues (0 critical). Execution time: 1299.57 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Bitmap Heap Scan",
      "relation": "data_bitmap_heap_scan",
      "actual_rows": 199943.0,
      "actual_loops": 1,
      "plan_rows": 196266,
      "shared_read_blocks": 112474,
      "parallel_aware": false
    },
    {
      "depth": 1,
      "node_type": "Bitmap Index Scan",
      "index": "idx_data_bitmap_heap_scan_val",
      "actual_rows": 199943.0,
      "actual_loops": 1,
      "plan_rows": 196266,
      "shared_read_blocks": 403,
      "parallel_aware": false
    }
  ]
}
```

## analysis

| metric              | value            |
|---------------------|------------------|
| execution time      | 1299.57 ms       |
| rows returned       | 199,943          |
| heap blocks read    | 112,474          |
| index blocks read   | 403              |
| rows per heap block | ~1.78            |
| issues              | 1 (INFO)         |

The plan shape matches scenario 1, but the row count now exceeds
100,000, so `BitmapHeapScanCheck` emits an `INFO` note.

### why `INFO`, not `WARNING`

A Bitmap Heap Scan is not a defect. The planner chooses it when the
range is too wide for an index scan to be efficient, but too narrow
for a full sequential scan. The check simply flags that the scan is
processing a large number of rows — useful when reviewing plans at
scale.

### what a fix might look like

The message suggests three directions, in increasing order of effort:

- **composite index** — if the query also filters on other columns,
  an index on `(val, other_col)` may avoid heap fetches for the
  filter.
- **partitioning** — partitioning by `val` allows the planner to
  prune partitions that fall outside the range, reducing heap I/O.
- **`CLUSTER`** — physically reordering the heap by `val` dramatically
  improves `rows per heap block`, at the cost of an exclusive lock
  during the operation.

The assistant also suggested a **covering index** — but that is not
applicable here because the query uses `SELECT *`, and the `pad`
column is not part of the index.

## what this proves

- `BitmapHeapScanCheck` fires when a Bitmap Heap Scan processes more
  than 100,000 rows.
- the check reports at `INFO` level, keeping the severity scale
  meaningful: `WARNING` is reserved for issues that call for action,
  `INFO` for observations.
- the assistant can combine the check's `INFO` message with the raw
  `plan_nodes` data (heap blocks, rows per block) to give a richer
  answer than either alone.

