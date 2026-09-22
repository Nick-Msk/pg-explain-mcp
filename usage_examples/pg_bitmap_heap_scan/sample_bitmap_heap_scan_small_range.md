# sample: Bitmap Heap Scan on a narrow range

> scenario 1 of 2 for `BitmapHeapScanCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_bitmap_heap_scan(5000000);
```

## context

`data_bitmap_heap_scan` contains 5M rows, roughly uniformly
distributed over `val` from 0 to 1M. The index on `val` supports
range queries; the planner chooses a **Bitmap Heap Scan** when the
range is selective enough that an index scan beats a sequential scan,
but not so selective that a plain index scan is preferable.

A narrow range (`500000..505000`, ~0.5 % selectivity) returns
~25k rows. That is **below** `BitmapHeapScanCheck`'s threshold
(100,000 rows), so the check stays silent.

## input query

```sql
select *
from mcp_explain_tool.data_bitmap_heap_scan
where val between 500000 and 505000;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 905.948,
  "planning_time_ms": 6.714,
  "total_time_ms": 912.662,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 905.95 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Bitmap Heap Scan",
      "relation": "data_bitmap_heap_scan",
      "actual_rows": 25166.0,
      "actual_loops": 1,
      "plan_rows": 25274,
      "shared_read_blocks": 23321,
      "parallel_aware": false
    },
    {
      "depth": 1,
      "node_type": "Bitmap Index Scan",
      "index": "idx_data_bitmap_heap_scan_val",
      "actual_rows": 25166.0,
      "actual_loops": 1,
      "plan_rows": 25274,
      "shared_read_blocks": 59,
      "parallel_aware": false
    }
  ]
}
```

## analysis

| metric              | value          |
|---------------------|----------------|
| execution time      | 905.95 ms      |
| rows returned       | 25,166         |
| heap blocks read    | 23,321         |
| index blocks read   | 59             |
| rows per heap block | ~1.08          |
| issues              | 0              |

`BitmapHeapScanCheck` stays silent — 25k rows is below its
`threshold_rows = 100000`. But the plan still reveals a real
characteristic worth noting: **23,321 heap blocks for 25,166 rows**
means roughly **one row per block**. The heap is not clustered by
`val`.

### the LLM added context the check did not

Because the analyzer only emits structured facts, the assistant was
free to point out the I/O dispersal:

> *"25,166 rows, 23,321 heap blocks — on average, each block contains
> only ~1 relevant row. The table is not physically ordered by `val`."*

It then suggested `CLUSTER` or a covering index. Those suggestions
are correct and useful, but they are **not** what `BitmapHeapScanCheck`
is designed to detect — the check reports when a Bitmap Heap Scan
processes many rows, not when the underlying heap is poorly clustered.

This is the intended division of labour:

- **analyzer** — deterministic, narrow, predictable checks;
- **assistant** — flexible reasoning over the structured plan data.

## what this proves

- `BitmapHeapScanCheck` does not fire below its row threshold, even
  when the plan is sub-optimal in other ways.
- the structured `plan_nodes` output gives the assistant enough
  context to reach conclusions the individual checks do not encode.
- not every useful observation needs a dedicated check.

## next step

see
[`sample_bitmap_heap_scan_large_range.md`](sample_bitmap_heap_scan_large_range.md)
for the same query shape with a range large enough to trip the check.

