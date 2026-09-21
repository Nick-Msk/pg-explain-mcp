# sample: range query with an index

> scenario 1 of 2 for `SeqScanCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_seq_scan(1000000);
```

## context

`data_seq_scan_norm` contains 1M rows and has an index on `val`. the
query filters by a narrow range (`between 100 and 200`), so the planner
uses an index-only scan — no seq scan appears in the plan.

## input query

```sql
select val
from mcp_explain_tool.data_seq_scan_norm
where val between 100 and 200;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 0.123,
  "planning_time_ms": 1.006,
  "total_time_ms": 1.129,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 0.12 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Index Only Scan",
      "relation": "data_seq_scan_norm",
      "index": "idx_data_seq_scan_norm_val",
      "actual_rows": 111.0,
      "plan_rows": 102,
      "heap_fetches": 0,
      "shared_read_blocks": 0
    }
  ]
}
```

## analysis

| metric         | value     |
|----------------|-----------|
| execution time | 0.12 ms   |
| rows returned  | 111       |
| heap fetches   | 0         |
| issues         | 0         |

the plan uses an **index only scan** on `idx_data_seq_scan_norm_val`.
`heap_fetches: 0` confirms that all data was served from the index
without touching the heap. no `seq scan` node is present, so
`SeqScanCheck` stays silent — this is the expected healthy state.

the optimizer's estimate (`plan_rows: 102`) is close to the actual
number of rows (`actual_rows: 111`), so `EstimateMismatchCheck` also
stays silent.

## what this proves

- `SeqScanCheck` does not produce false positives when an index is used.
- the plan tree exposes enough context (`node_type`, `heap_fetches`,
  `plan_rows`, `actual_rows`) for the llm to reason about the plan
  instead of guessing.

## next step

see [`sample_seq_scan_on_nonindex.md`](sample_seq_scan_on_nonindex.md)
for the same query against a table without an index.

