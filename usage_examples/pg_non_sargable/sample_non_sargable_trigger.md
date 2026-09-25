# sample: non-sargable predicate blocks index usage

> trigger scenario for `NonSargableCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_non_sargable(5000000);
```

## context

`data_non_sargable` has a plain index on `email`. The query wraps
the column in `lower()`, which makes the predicate **non-sargable**:
the planner cannot use `idx_data_non_sargable_email` and falls back
to a parallel `Seq Scan`.

`NonSargableCheck` fires at `INFO` level — the predicate is
functionally correct, just not index-friendly. `SeqScanCheck` also
fires because the scan reads the whole table and discards everything.

Both issues sit at `depth: 1` with `parent_node: "Gather"`, so the
assistant groups them by root cause instead of listing them
independently.

## input query

```sql
select count(*)
from mcp_explain_tool.data_non_sargable
where lower(email) = 'user0000000003@example.com';
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
    "IndexScanCheck",
    "PartitionPruningCheck",
    "NonSargableCheck"
  ],
  "execution_time_ms": 221.982,
  "planning_time_ms": 2.845,
  "total_time_ms": 224.827,
  "issues": [
    {
      "severity": "warning",
      "type": "seq_scan",
      "message": "Sequential scan on 'data_non_sargable' read 333333.33 rows (0.33 returned, 333333 filtered out, 100.0% discarded). Verify whether an index on the filter column exists; if it does, investigate why the planner ignored it (stale statistics, low correlation, or high random_page_cost). If no index exists, consider adding one.",
      "node": "Seq Scan",
      "depth": 1,
      "parent_node": "Gather"
    },
    {
      "severity": "info",
      "type": "non_sargable",
      "message": "Non-sargable predicate on 'data_non_sargable': the filter wraps 'email' in 'lower(...)', so the index on 'email' cannot be used. Consider a functional index on the exact expression, or rewriting the predicate. Filter: (lower(email) = 'user0000000003@example.com'::text)",
      "node": "Seq Scan",
      "depth": 1,
      "parent_node": "Gather"
    }
  ],
  "issue_count": 2,
  "summary": "Found 2 issues (1 critical). Execution time: 221.98 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Gather",
      "actual_loops": 1,
      "actual_rows": 1.0,
      "parallel_aware": false,
      "plan_rows": 5000,
      "shared_read_blocks": 22274
    },
    {
      "depth": 1,
      "node_type": "Seq Scan",
      "actual_loops": 3,
      "actual_rows": 0.33,
      "parallel_aware": true,
      "plan_rows": 2083,
      "relation": "data_non_sargable",
      "rows_removed_by_filter": 333333,
      "shared_read_blocks": 22274
    }
  ]
}
```

## analysis

| metric              | value           |
|---------------------|-----------------|
| execution time      | 221.98 ms       |
| rows scanned        | ~1,000,000      |
| rows returned       | 1               |
| rows discarded      | ~1,000,000      |
| access method       | `Parallel Seq Scan` |
| issues              | 2               |

### root cause: non-sargable predicate

`lower(email) = '...'` wraps the indexed column in a function. The
planner cannot map the predicate onto the B-tree structure of
`idx_data_non_sargable_email`, so it falls back to a full scan.

The check emits an `INFO` — not a `WARNING` — because the predicate
itself is not wrong. It is simply not index-friendly. The fix is
optional: the query works, it is just slow.

### consequence: `seq_scan`

`SeqScanCheck` reports that the scan read ~1M rows and discarded all
of them. This is the same event viewed from a different angle:
without the non-sargable predicate, the planner would not have chosen
this scan at all.

### why `estimate_mismatch` stays silent

`plan_rows: 5000` on `Gather`, `actual_rows: 1`. The ratio is
enormous (5000x), but `actual_rows` is far below the check's absolute
floor (`min_rows = 1000`). A perfect estimate would not have changed
the plan: 1 vs. 5000 rows is noise. The check therefore ignores it
and lets `non_sargable` do the talking.

## recommendations

### option 1 — functional index

Best when case-insensitive search is a genuine requirement:

```sql
create index idx_data_non_sargable_lower_email
    on mcp_explain_tool.data_non_sargable (lower(email));
```

After this the planner uses the new index and the query becomes an
`Index Scan`. `SeqScanCheck` stops firing, and `NonSargableCheck`
stops firing because the predicate now appears in `Index Cond`
instead of `Filter`.

### option 2 — normalize data

If `email` is always stored lowercase, drop the function and query
the column directly:

```sql
select count(*)
from mcp_explain_tool.data_non_sargable
where email = 'user0000000003@example.com';
```

This uses the existing index — see
[`sample_non_sargable_norm.md`](sample_non_sargable_norm.md).

## what this proves

- `NonSargableCheck` detects predicates that wrap an indexed column
  in a function, without relying on a whitelist of function names.
- the check fires at `INFO` level, distinguishing "predicate is
  non-optimal" from "predicate is wrong".
- `Issue.depth` and `Issue.parent_node` let the assistant group
  `non_sargable` and `seq_scan` as root cause + consequence rather
  than two independent findings.
- the refined `EstimateMismatchCheck` (requiring both sides to exceed
  `min_rows`) suppresses low-signal ratios and keeps the issue list
  focused.

