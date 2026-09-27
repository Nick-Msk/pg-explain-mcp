# sample: Index Only Scan with a fresh visibility map

> baseline scenario for `IndexOnlyScanCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_index_scan(5000000);
vacuum analyze mcp_explain_tool.data_index_scan_norm;
```

## context

`data_index_scan_norm` has an index on `val` covering the only
column the query selects. `VACUUM ANALYZE` ran right after the load,
so the visibility map is fresh — every tuple in the index is marked
as visible without visiting the heap.

The planner chooses an **Index Only Scan**, and `heap_fetches` is 0.
`IndexOnlyScanCheck` stays silent: the check fires only when an
Index Only Scan performs significant heap fetches, i.e. when the
visibility map is stale. A clean scan is the "good" state.

## input query

```sql
select val
from mcp_explain_tool.data_index_scan_norm
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
  "execution_time_ms": 0.639,
  "planning_time_ms": 4.92,
  "total_time_ms": 5.559,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 0.64 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Limit",
      "actual_loops": 1,
      "actual_rows": 228.0,
      "parallel_aware": false,
      "plan_rows": 64,
      "shared_read_blocks": 2
    },
    {
      "depth": 1,
      "node_type": "Index Only Scan",
      "actual_loops": 1,
      "actual_rows": 228.0,
      "heap_fetches": 0,
      "index": "idx_data_index_scan_norm_val",
      "parallel_aware": false,
      "plan_rows": 64,
      "relation": "data_index_scan_norm",
      "shared_read_blocks": 2
    }
  ]
}
```

## analysis

| metric            | value                          |
|-------------------|--------------------------------|
| execution time    | 0.64 ms                        |
| rows returned     | 228                            |
| access method     | `Index Only Scan`              |
| index             | `idx_data_index_scan_norm_val` |
| heap fetches      | **0**                          |
| shared read blocks| 2                              |
| issues            | 0                              |

### why `IndexOnlyScanCheck` stays silent

The check fires when an Index Only Scan performs a significant
number of heap fetches (default threshold: ≥ 1000 fetches AND
≥ 10 % of returned rows). Here `heap_fetches` is 0 — the visibility
map is fully set, so the scan never touches the heap. Nothing to
report.

### why `EstimateMismatchCheck` stays silent

The planner estimated 64 rows (`plan_rows` on both `Limit` and the
scan); actual is 228. Ratio is ~3.6×, well below
`threshold_ratio = 10`. Additionally, `max(64, 228) = 228` is below
`min_rows = 1000` — either guard alone would suppress the check.

### why `IndexRegularScanCheck` stays silent

`IndexRegularScanCheck` inspects plain `Index Scan` nodes. Here the
scan is `Index Only Scan` — a different node type entirely, so the
check never sees it.

### why `SeqScanCheck` stays silent

No `Seq Scan` node in the plan. The `Limit` and `Index Only Scan`
nodes are inspected, neither triggers the check.

## what this proves

- `IndexOnlyScanCheck` does not fire on healthy Index Only Scans.
  `heap_fetches = 0` is the "all clear" signal.
- an Index Only Scan with fresh visibility is the ideal access path
  for a covering index — 2 blocks read, 0.64 ms total.
- a moderate row-estimate mismatch (3.6×) on tiny absolute numbers
  (64 vs. 228) does not warrant a warning; the `min_rows` guard
  filters it out.
- `IndexRegularScanCheck` and `IndexOnlyScanCheck` are scoped
  strictly: each inspects exactly one node type. A plan containing
  only one of them will only trigger one of them.

## test environment

This example was captured with:

- **MCP client**: Continue.dev
- **LLM**: `qwen 3.8 27b-splash` via LM Studio
- **PostgreSQL**: 18.6

Qwen tends to produce structured, report-style answers (tables,
headers, checklists) rather than conversational prose. If your
setup uses a different model, the raw tool output above will be
identical — only the surrounding prose may differ.

## next step

see
[`sample_index_only_scan_unclastered.md`](sample_index_only_scan_unclastered.md)
for the same query against a table with a stale visibility map,
where `IndexOnlyScanCheck` fires.

