# sample: statistics overwritten, planner misestimates cardinality

> scenario 2 of 3 for `EstimateMismatchCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_estimate_mismatch(1000000);
```

## context

`data_estimate_mismatch_skewed` contains 1M rows with a uniform
distribution of `val` in `0..100` — same data as the norm table. But
`fill_estimate_mismatch` overwrites the statistics for this table
using `pg_restore_attribute_stats`, telling the planner that **95 % of
rows have `val = 42`**.

Autovacuum is **disabled** on this table — otherwise it would re-`ANALYZE`
the table and overwrite the fake statistics with the real distribution
within minutes, and the fixture would stop working.

The query for `val = 42` therefore hits a planner that believes it
will return 950 000 rows, when in reality it returns ~10 000.

## input query

```sql
select *
from mcp_explain_tool.data_estimate_mismatch_skewed
where val = 42;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 2330.979,
  "planning_time_ms": 4.03,
  "total_time_ms": 2335.009,
  "issues": [
    {
      "severity": "warning",
      "type": "seq_scan",
      "message": "Sequential scan on 'data_estimate_mismatch_skewed' read 1000000.0 rows (10050.0 returned, 989950 filtered out, 99.0% discarded). Verify whether an index on the filter column exists; if it does, investigate why the planner ignored it (stale statistics, low correlation, or high random_page_cost). If no index exists, consider adding one.",
      "node": "Seq Scan"
    },
    {
      "severity": "warning",
      "type": "estimate_mismatch",
      "message": "Planner misestimated cardinality on 'Seq Scan': expected 950000, got 10050.0 (ratio x94.5). Consider running ANALYZE.",
      "node": "Seq Scan"
    }
  ],
  "issue_count": 2,
  "summary": "Found 2 issues (2 critical). Execution time: 2330.98 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Seq Scan",
      "relation": "data_estimate_mismatch_skewed",
      "actual_rows": 10050.0,
      "actual_loops": 1,
      "plan_rows": 950000,
      "rows_removed_by_filter": 989950,
      "shared_read_blocks": 29204,
      "parallel_aware": false
    }
  ]
}
```

## analysis

### `EstimateMismatchCheck` fires

| metric      | value         |
|-------------|---------------|
| plan_rows   | 950,000       |
| actual_rows | 10,050        |
| ratio       | **~94.5x**    |
| issue type  | `estimate_mismatch` |

The ratio exceeds the check's `threshold_ratio = 10`, and both
values exceed `min_rows = 1000`. The check emits a warning and
suggests `ANALYZE`.

### `SeqScanCheck` fires too

| metric                | value       |
|-----------------------|-------------|
| rows read             | 1,000,000   |
| rows returned         | 10,050      |
| rows discarded        | 989,950     |
| discard ratio         | 99.0 %      |
| issue type            | `seq_scan`  |

A `Seq Scan` with a selective filter that discards 99 % of the read
rows matches the pattern `SeqScanCheck` is designed to report.

### why both fire

The two checks are **independent**, and here they describe the **same
underlying problem from two angles**:

- `estimate_mismatch` says: *the planner's guess about cardinality is
  wrong*.
- `seq_scan` says: *the plan reads a lot of rows and throws most of
  them away*.

Both are true. The first is the **cause**; the second is the
**symptom**. The presence of both in the output gives the reader the
complete picture — a single check would only capture half.

### why the query is so slow

Total execution time is **2 330 ms** — 26× slower than the same query
on the norm table. This is the direct consequence of the
misestimation: with `plan_rows = 950000`, the planner correctly
concludes that a `Seq Scan` is cheaper than any index-based path (it
is right — *for its incorrect assumption*). The plan choice is
rational; the estimate is not.

## recommendations

The order matters:

1. **Fix the statistics first**:

   ```sql
   analyze mcp_explain_tool.data_estimate_mismatch_skewed;
   ```

   After `ANALYZE`, the planner sees the real distribution and is
   free to pick a better plan.

2. **Then add an index** if the query pattern is common:

   ```sql
   create index idx_data_estimate_mismatch_skewed_val
       on mcp_explain_tool.data_estimate_mismatch_skewed (val);
   ```

   Without step 1, the index would not be used: the planner still
   believes 95 % of rows match.

## what this proves

- `EstimateMismatchCheck` detects cardinality misestimates that
  mislead the planner into a slow but locally rational plan.
- multiple checks can fire on the same plan — they answer different
  questions and the reader benefits from seeing both.
- a correct `ANALYZE` is the cheapest and often most effective fix.
- fixture design must account for autovacuum: the fake statistics
  only survive because `autovacuum_enabled = false` is set on the
  table.

## next step

see
[`sample_estimate_mismatch_on_skewed_other_value.md`](sample_estimate_mismatch_on_skewed_other_value.md)
for the same table queried on `val = 43` — the statistics are only
wrong for `val = 42`, so the check correctly stays silent.


