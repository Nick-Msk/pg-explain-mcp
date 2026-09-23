# sample: honest statistics + index — no issues at all

> baseline scenario for `EstimateMismatchCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_estimate_mismatch(1000000);

-- optional: add an index to make this a fully clean run
create index on mcp_explain_tool.data_estimate_mismatch_norm (val);
```

## context

Same table as scenario 1 (`data_estimate_mismatch_norm`), but with an
index on `val`. Adding the index changes the plan shape — the planner
now has a realistic choice between `Seq Scan`, `Bitmap Heap Scan`, and
`Index Scan` — and, with honest statistics, it picks a plan that
satisfies every check.

This is the **baseline**: what a well-behaved query looks like when
neither statistics are stale nor indexes are missing. There is nothing
to fix, and the analyzer correctly says so.

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
  "execution_time_ms": 31.046,
  "planning_time_ms": 8.374,
  "total_time_ms": 39.42,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 31.05 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Bitmap Heap Scan",
      "relation": "data_estimate_mismatch_norm",
      "actual_rows": 10013.0,
      "actual_loops": 1,
      "plan_rows": 9933,
      "shared_read_blocks": 0,
      "parallel_aware": false
    },
    {
      "depth": 1,
      "node_type": "Bitmap Index Scan",
      "index": "data_estimate_mismatch_norm_val_idx",
      "actual_rows": 10013.0,
      "actual_loops": 1,
      "plan_rows": 9933,
      "shared_read_blocks": 0,
      "parallel_aware": false
    }
  ]
}
```

## analysis

| metric         | value       |
|----------------|-------------|
| execution time | 31.05 ms    |
| plan_rows      | 9,933       |
| actual_rows    | 10,013      |
| ratio          | **~1.008x** |
| issues         | 0           |

Both checks stay silent, and for different reasons:

### `EstimateMismatchCheck` — silent

The estimate (`plan_rows: 9933`) is within 0.8 % of reality
(`actual_rows: 10013`). The ratio is far below `threshold_ratio = 10`,
and the absolute count exceeds `min_rows = 1000` — but the ratio
condition fails, so no warning is emitted.

### `SeqScanCheck` — silent

The plan is **not** a `Seq Scan`. With an index available, the planner
chooses `Bitmap Index Scan` + `Bitmap Heap Scan`, which is efficient
for a ~1 % selectivity query on a 1M-row table. `SeqScanCheck` only
inspects `Seq Scan` nodes, so it has nothing to report.

### `IndexScanCheck` — silent

The plan contains `Bitmap Index Scan` and `Bitmap Heap Scan`, neither
of which is the `Index Scan` or `Index Only Scan` that this check
inspects. `IndexScanCheck` is scoped to stale visibility maps and
poor heap locality, and neither condition is present here.

## what this proves

- A well-tuned query produces **zero issues** — the analyzer does not
  manufacture warnings to justify its existence.
- Adding an index to a table changes the plan shape, which changes
  which checks are even applicable. Not every check runs on every
  plan.
- `EstimateMismatchCheck` fires on **ratios**, not on absolute
  magnitudes. Even with 10 000 rows, a 0.8 % error is well within
  tolerance.

## scenario summary

The full `estimate_mismatch` fixture now covers four cases:

| # | table | index | query | estimate_mismatch | seq_scan |
|---|-------|-------|-------|-------------------|----------|
| 1 | norm     | no  | `val = 42` | —         | fires    |
| 2 | skewed   | no  | `val = 42` | **fires** | fires    |
| 3 | skewed   | no  | `val = 43` | —         | fires    |
| 4 | norm     | yes | `val = 42` | —         | —        |

The only scenario where `EstimateMismatchCheck` fires is #2 — the one
where the statistics genuinely mislead the planner.

