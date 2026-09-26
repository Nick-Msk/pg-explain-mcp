# sample: skew leak — ratio above 10x, but check stays silent

> edge-case scenario for `EstimateMismatchCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_estimate_mismatch(1000000);
```

## context

Same table as [scenario 2](sample_estimate_mismatch_on_skewed.md):
`data_estimate_mismatch_skewed` has its `most_common_vals` /
`most_common_freqs` overwritten to claim that 95 % of rows have
`val = 42`.

The fake statistics **leak**. When the planner is told that one value
accounts for 95 % of all rows, the remaining 5 % must be distributed
across the other 99 values. The estimated selectivity for any other
value collapses to roughly `0.05 / 99 ≈ 0.0005`, i.e. ~505 rows out
of 1M.

In reality the data is uniform, so `val = 44` matches about 10 000
rows. The estimate is off by ~20x.

The check still stays silent. This file explains why.

## input query

```sql
select *
from mcp_explain_tool.data_estimate_mismatch_skewed
where val = 44;
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
    "PartitionPruningCheck",
    "NonSargableCheck"
  ],
  "execution_time_ms": 146.413,
  "planning_time_ms": 3.913,
  "total_time_ms": 150.326,
  "issues": [
    {
      "severity": "warning",
      "type": "seq_scan",
      "message": "Sequential scan on 'data_estimate_mismatch_skewed' read 333333.67 rows (3295.67 returned, 330038 filtered out, 99.0% discarded). Verify whether an index on the filter column exists; if it does, investigate why the planner ignored it (stale statistics, low correlation, or high random_page_cost). If no index exists, consider adding one.",
      "node": "Seq Scan",
      "depth": 1,
      "parent_node": "Gather"
    }
  ],
  "issue_count": 1,
  "summary": "Found 1 issues (1 critical). Execution time: 146.41 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Gather",
      "actual_loops": 1,
      "actual_rows": 9887.0,
      "parallel_aware": false,
      "plan_rows": 505,
      "shared_read_blocks": 23759
    },
    {
      "depth": 1,
      "node_type": "Seq Scan",
      "actual_loops": 3,
      "actual_rows": 3295.67,
      "parallel_aware": true,
      "plan_rows": 210,
      "relation": "data_estimate_mismatch_skewed",
      "rows_removed_by_filter": 330038,
      "shared_read_blocks": 23759
    }
  ]
}
```

## analysis

| node     | `plan_rows` | `actual_rows` | ratio  |
|----------|-------------|---------------|--------|
| Gather   | 505         | 9,887        | 19.6x  |
| Seq Scan | 210         | 3,295.67 ×3  | 15.7x  |

`EstimateMismatchCheck` sees the ratios above 10x, but does **not**
fire. Reason — the `min_rows` floor is applied to **both** sides:

```python
if planned < self.min_rows or actual < self.min_rows:
    return False
```

`planned = 505 < min_rows = 1000`. The check returns silently.

### why this is the right behaviour

`EstimateMismatchCheck` is not a hunt for "any large ratio". It
answers a specific question: *would a correct estimate have changed
the plan?*

For `val = 44` on this table:

- there is **no index** on `val`, so the planner's only option was a
  `Seq Scan` — regardless of the estimated cardinality;
- the actual number of matching rows is under 10 000 — for a
  full-table scan that is a small fraction, and even a perfect
  estimate would not have triggered a different strategy;
- the query runs in ~150 ms, dominated by reading the table, not by
  a bad plan choice.

A plan choice made for the wrong reasons but ending up at the same
place is not a problem worth reporting. The `min_rows` floor
filters exactly these cases out.

### the "leak" is a real production pattern

Fake statistics for one value leaking into estimates for all other
values is not an artefact of this fixture — it is how PostgreSQL's
selectivity model works. When the planner trusts an MCV entry that
claims "95 % of rows have X", the remaining probability mass is
pushed onto the histogram, and every other value gets an
underestimate.

In production this shows up after:

- bulk data loads without `ANALYZE`;
- hot values that grew fast since the last statistics refresh;
- partitioning where a single hot partition dominates a partition
  key.

The pattern is real; the response is to `ANALYZE` the table (which
would correct the leak). But on **this** fixture the leak cannot be
fixed by `ANALYZE` because autovacuum is disabled — that is deliberate
so the skewed state survives across runs.

## comparison

| file | query | `plan_rows` | `actual_rows` | `estimate_mismatch` |
|---|---|---|---|---|
| [scenario 1](sample_estimate_mismatch_on_norm.md) | `val = 42` on norm table | 9,033 | 9,852 | — |
| [scenario 2](sample_estimate_mismatch_on_skewed.md) | `val = 42` on skewed table | 950,000 | 10,050 | **fires** |
| **this file** | `val = 44` on skewed table | 505 | 9,887 | — |
| [scenario 4](sample_estimate_mismatch_on_norm_indexed.md) | `val = 42` on indexed norm | 9,933 | 10,013 | — |

Scenario 2 is the only one where a correct estimate would have
changed the plan: the planner "thought" it needed to read 95 % of
the table, and picked the same access path it would have picked for
that assumption. `ANALYZE` there would not help either — but the
spurious MCV entry is the root cause, and it is fixable.

## what this proves

- `EstimateMismatchCheck` uses `min_rows` on **both** sides of the
  comparison. A large ratio is not enough — the absolute numbers
  must also be material.
- a fake MCV entry for one value leaks into estimates for all other
  values; this is standard planner behaviour, not a fixture quirk.
- the check's silence is meaningful: it means "even a perfect
  estimate would not have changed the plan".
- all four `estimate_mismatch` scenarios together demonstrate:
  honest stats (silent), skewed stats on the hot value (fires),
  skewed stats on a cold value (silent), and honest stats with an
  index in place (silent).

