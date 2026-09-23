# sample: estimate matches reality (no mismatch)

> scenario 1 of 3 for `EstimateMismatchCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_estimate_mismatch(1000000);
```

## context

`data_estimate_mismatch_norm` contains 1M rows with a uniform
distribution of `val` in `0..100`. `ANALYZE` ran after the insert, so
the planner's statistics match the data. A query for a single value
returns ~10k rows, and the planner's estimate is close.

**The focus of this example is `EstimateMismatchCheck`.** It stays
silent: the ratio between `plan_rows` and `actual_rows` is ~1.03,
well below `threshold_ratio = 10`.

A **different** check — `SeqScanCheck` — does fire here, because the
table has no index on `val` and the filter discards 99 % of the
rows. That is not a failure of this fixture: it shows that different
checks on the same plan answer different questions. See
[`../pg_seq_scan_adapters/`](../pg_seq_scan_adapters/) for the
dedicated `SeqScanCheck` examples.

## input query

```sql
select *
from mcp_explain_tool.data_estimate_mismatch_norm
where val = 42;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 166.112,
  "planning_time_ms": 10.525,
  "total_time_ms": 176.637,
  "issues": [
    {
      "severity": "warning",
      "type": "seq_scan",
      "message": "Sequential scan on 'data_estimate_mismatch_norm' read 333333.0 rows (3284.0 returned, 330049 filtered out, 99.0% discarded). Verify whether an index on the filter column exists; if it does, investigate why the planner ignored it (stale statistics, low correlation, or high random_page_cost). If no index exists, consider adding one.",
      "node": "Seq Scan"
    }
  ],
  "issue_count": 1,
  "summary": "Found 1 issues (1 critical). Execution time: 166.11 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Gather",
      "actual_rows": 9852.0,
      "actual_loops": 1,
      "plan_rows": 10167,
      "shared_read_blocks": 30304,
      "parallel_aware": false
    },
    {
      "depth": 1,
      "node_type": "Seq Scan",
      "relation": "data_estimate_mismatch_norm",
      "actual_rows": 3284.0,
      "actual_loops": 3,
      "plan_rows": 4236,
      "rows_removed_by_filter": 330049,
      "shared_read_blocks": 30304,
      "parallel_aware": true
    }
  ]
}
```

## analysis

### what `EstimateMismatchCheck` sees (no fire)

| metric         | value         |
|----------------|---------------|
| plan_rows      | 10,167        |
| actual_rows    | 9,852         |
| ratio          | **~1.03x**    |
| `issue_count` for this type | 0 |

the planner's estimate (`plan_rows: 10167` at the Gather level) is
within 4 % of the actual row count (`actual_rows: 9852`). Both
thresholds in `EstimateMismatchCheck` are satisfied — the ratio is
below 10, and the larger value exceeds `min_rows = 1000`.

result: no `estimate_mismatch` warning. This is the point of the
fixture — statistics that reflect reality produce no alarm.

### what `SeqScanCheck` sees (fires)

The plan uses a `Seq Scan` with no filter fallback to an index,
reading ~1M rows (333,333 × 3 workers) and discarding 99 % of them
(`rows_removed_by_filter: 330049` per worker). That is exactly the
pattern `SeqScanCheck` is designed to report.

The two checks are **independent**: one cares about ratio between
plan and actual, the other about discarded rows. A single plan can
satisfy either, both, or neither.

## why the check does not fire

`EstimateMismatchCheck` fires only when **both** conditions hold:

- `max(plan_rows, actual_rows) ≥ min_rows` (1000), **and**
- `max / min > threshold_ratio` (10).

Here the ratio is ~1.03 — far below the threshold. The check is
intentionally conservative: small absolute counts and modest ratios
are ignored to avoid noise on perfectly normal plans.

## what this proves

- `EstimateMismatchCheck` does not fire when statistics reflect
  reality.
- the check ignores small queries (`min_rows` guard) and modest
  misestimates (`threshold_ratio` guard).
- **checks are independent.** A plan can trigger `seq_scan` while
  `estimate_mismatch` stays silent. The tool reports each issue
  separately, and the reader should consult the corresponding check's
  documentation for details.
- both `plan_rows` and `actual_rows` are exposed in `plan_nodes`, so
  the assistant can verify the check's decision.

## next step

see
[`sample_estimate_mismatch_on_skewed.md`](sample_estimate_mismatch_on_skewed.md)
for the same table with overwritten statistics — the ratio there
reaches ~94x and `EstimateMismatchCheck` fires.


