# Sample: Index Scan on a Healthy Table

> Scenario 1 of 2 for `IndexScanCheck`.

## Prerequisites

```sql
CREATE EXTENSION mcp_explain_tool;
CALL mcp_explain_tool.fill_index_scan(5000000);
VACUUM ANALYZE mcp_explain_tool.data_index_scan_norm;
```

## Context

`data_index_scan_norm` is loaded with 5M rows and immediately
`VACUUM ANALYZE`-ed. The visibility map is fresh, so an Index Only Scan
can serve the query without visiting the heap.

This is the **expected healthy state** — `IndexScanCheck` must **not**
trigger any warnings here. If it did, it would be a false positive.

## Input Query

```sql
SELECT val
FROM mcp_explain_tool.data_index_scan_norm
WHERE val BETWEEN '000' AND '001'
LIMIT 10000;
```

## Prompt to the Assistant

> Analyze the plan for the query above.
> Also show me the raw JSON output you received.

## Raw Output from pg-explain

```json
{
  "execution_time_ms": 0.243,
  "planning_time_ms": 5.712,
  "total_time_ms": 5.955,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 0.24 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Limit",
      "actual_rows": 83.0,
      "plan_rows": 4,
      "shared_read_blocks": 0
    },
    {
      "depth": 1,
      "node_type": "Index Only Scan",
      "relation": "data_index_scan_norm",
      "index": "idx_data_index_scan_norm_val",
      "actual_rows": 83.0,
      "plan_rows": 4,
      "heap_fetches": 0,
      "shared_read_blocks": 0
    }
  ]
}
```

## Analysis

| Metric         | Value     |
|----------------|-----------|
| Execution Time | 0.24 ms   |
| Rows Returned  | 83        |
| Heap Fetches   | **0**     |
| Issues         | 0         |

The plan shows a clean **Index Only Scan**. `Heap Fetches: 0` confirms
that the visibility map is fresh and the index alone is sufficient.
No warnings are emitted — `IndexScanCheck` correctly stays silent.

## What This Proves

- `IndexScanCheck` does **not** produce false positives on healthy tables.
- The plan tree exposes `heap_fetches` so the LLM can reason about the
  actual execution, not guess.
- `EstimateMismatchCheck` (which would otherwise flag 4 vs 83) is
  suppressed by its absolute threshold (`MIN_ROWS = 1000`).

## Next Step

Run the same query against `data_index_scan_unclastered` — see
[`sample_index_on_unclastered.md`](sample_index_on_unclastered.md).

