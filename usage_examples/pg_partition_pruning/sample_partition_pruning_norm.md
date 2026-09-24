# sample: partition pruning works

> baseline scenario for `PartitionPruningCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_partition_pruning(1000000);
```

## context

`data_partition_pruning` is partitioned by month on `ts` — 12
partitions for 2026. The query filters a three-month range with a
**sargable predicate**:

```sql
where ts >= '2026-06-01' and ts < '2026-09-01'
```

The planner maps the range onto partition boundaries and prunes 9 of
the 12 partitions before execution. `PartitionPruningCheck` stays
silent because `Append` covers only 3 children — well below its
threshold of 3.

No other check fires either. `SeqScanCheck` sees three partition scans
but each has `Rows Removed by Filter: 0` — the predicate is fully
satisfied by partition pruning, so nothing is discarded. In other
words, all rows read are relevant, and there is nothing to fix.

## input query

```sql
select count(*)
from mcp_explain_tool.data_partition_pruning
where ts >= '2026-06-01' and ts < '2026-09-01';
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
    "PartitionPruningCheck"
  ],
  "execution_time_ms": 50.952,
  "planning_time_ms": 9.623,
  "total_time_ms": 60.575,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 50.95 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Aggregate",
      "actual_loops": 1,
      "actual_rows": 1.0,
      "parallel_aware": false,
      "plan_rows": 1,
      "shared_read_blocks": 5310
    },
    {
      "depth": 1,
      "node_type": "Gather",
      "actual_loops": 1,
      "actual_rows": 3.0,
      "parallel_aware": false,
      "plan_rows": 2,
      "shared_read_blocks": 5310
    },
    {
      "depth": 2,
      "node_type": "Aggregate",
      "actual_loops": 3,
      "actual_rows": 1.0,
      "parallel_aware": false,
      "plan_rows": 1,
      "shared_read_blocks": 5310
    },
    {
      "depth": 3,
      "node_type": "Append",
      "actual_loops": 3,
      "actual_rows": 84026.67,
      "parallel_aware": true,
      "plan_rows": 105034,
      "shared_read_blocks": 5310
    },
    {
      "depth": 4,
      "node_type": "Seq Scan",
      "actual_loops": 3,
      "actual_rows": 28313.33,
      "parallel_aware": true,
      "plan_rows": 49965,
      "relation": "data_partition_pruning_p202607",
      "rows_removed_by_filter": 0,
      "shared_read_blocks": 2655
    },
    {
      "depth": 4,
      "node_type": "Seq Scan",
      "actual_loops": 2,
      "actual_rows": 42470.0,
      "parallel_aware": true,
      "plan_rows": 49965,
      "relation": "data_partition_pruning_p202608",
      "rows_removed_by_filter": 0,
      "shared_read_blocks": 2655
    },
    {
      "depth": 4,
      "node_type": "Seq Scan",
      "actual_loops": 1,
      "actual_rows": 82200.0,
      "parallel_aware": true,
      "plan_rows": 48353,
      "relation": "data_partition_pruning_p202606",
      "rows_removed_by_filter": 0,
      "shared_read_blocks": 0
    }
  ]
}
```

## analysis

| metric              | value             |
|---------------------|-------------------|
| execution time      | 50.95 ms          |
| partitions scanned  | **3 / 12**        |
| partitions pruned   | 9                 |
| rows read           | ~252,080          |
| rows discarded      | 0                 |
| issues              | 0                 |

### why `PartitionPruningCheck` stays silent

`Append` covers exactly 3 children (`p202606`, `p202607`, `p202608`) —
the threshold is 3, so the check returns no issue.

### why `SeqScanCheck` stays silent

Three partition scans would normally look suspicious — the check
usually fires when a `Seq Scan` reads many rows. Here it does not,
because every scan has `Rows Removed by Filter: 0`. The filter is
already satisfied by pruning, so no rows are wasted. A `Seq Scan`
that reads only what it needs is **not** an anti-pattern.

### why `EstimateMismatchCheck` stays silent

`plan_rows: 105034` on the `Append`, `actual_rows: 84026.67 × 3`
≈ 252,080. The planner's estimate is in the right ballpark: the
predicate is sargable, so per-partition statistics apply, and the
aggregate estimate is accurate within the analyzer's tolerance.

## comparison with the trigger scenario

| | norm (this file) | [trigger](sample_partition_pruning_trigger.md) |
|---|---|---|
| predicate | `ts >= '2026-06-01' AND ts < '2026-09-01'` | `extract(month from ts) = 6` |
| partitions scanned | 3 / 12 | 12 / 12 |
| issues | 0 | 14 (1 `partition_pruning` + 12 `seq_scan` + 1 `estimate_mismatch`) |
| execution time | 50.95 ms | 149.41 ms |

Rewriting the predicate is the entire fix. The analyzer's job is to
make the connection between the two plans obvious.

## what this proves

- `PartitionPruningCheck` does not fire when pruning works — no
  false positives on healthy plans.
- `SeqScanCheck` distinguishes "reads all rows and discards them"
  (a problem) from "reads only the relevant rows" (fine), by looking
  at `Rows Removed by Filter`.
- the check threshold of 3 partitions is a reasonable default:
  month-based partitioning with quarterly queries scans 3 partitions
  naturally, while a failed-pruning query scans all of them.

