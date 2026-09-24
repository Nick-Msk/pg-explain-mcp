# sample: partition pruning failed

> trigger scenario for `PartitionPruningCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_partition_pruning(1000000);
```

## context

`data_partition_pruning` is partitioned by month on `ts` — 12
partitions for 2026. The query filters June rows with a **non-sargable
predicate**:

```sql
where extract(month from ts) = 6
```

Because `extract()` wraps the partition key, the planner cannot map the
predicate to partition boundaries. It falls back to scanning **every**
partition. `PartitionPruningCheck` fires and reports the failure.

The plan also triggers `SeqScanCheck` on the 11 partitions that hold no
June rows, and `EstimateMismatchCheck` on the `Append` and on the
non-empty partition. These are **consequences** of the pruning failure:
the planner has no per-partition statistics to attribute rows to when
the predicate is non-sargable.

## input query

```sql
select count(*)
from mcp_explain_tool.data_partition_pruning
where extract(month from ts) = 6;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain (abridged)

Only the leading issue types are shown. In the full output,
`issues` contains 14 entries.

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
  "execution_time_ms": 149.41,
  "planning_time_ms": 13.575,
  "total_time_ms": 162.98,
  "issue_count": 14,
  "summary": "Found 14 issues (14 critical). Execution time: 149.41 ms.",
  "issues": [
    {
      "severity": "warning",
      "type": "partition_pruning",
      "message": "Append over 12 partitions (threshold: 3). Partition pruning may have failed — check that the predicate on the partition key is sargable: no function calls, no casts, no expressions. Note: any cardinality misestimates inside the partitions are a consequence, not stale statistics. The planner cannot attribute rows to partitions when the predicate is non-sargable, so ANALYZE will not help — rewrite the predicate. Scanned: data_partition_pruning_p202603, data_partition_pruning_p202605, data_partition_pruning_p202607, … (+9 more)",
      "node": "Append",
      "depth": 3,
      "parent_node": "Aggregate"
    },
    {
      "severity": "warning",
      "type": "estimate_mismatch",
      "message": "Planner misestimated cardinality on 'Append': expected 2083, got 27400.0 (ratio x13.2). Consider running ANALYZE.",
      "node": "Append",
      "depth": 3,
      "parent_node": "Aggregate"
    },
    {
      "severity": "warning",
      "type": "seq_scan",
      "message": "Sequential scan on 'data_partition_pruning_p202603' read 84940.0 rows (0.0 returned, 84940 filtered out, 100.0% discarded)....",
      "node": "Seq Scan",
      "depth": 4,
      "parent_node": "Append"
    },
    {
      "severity": "warning",
      "type": "seq_scan",
      "message": "Sequential scan on 'data_partition_pruning_p202605' read 84940.0 rows (0.0 returned, 84940 filtered out, 100.0% discarded)....",
      "node": "Seq Scan",
      "depth": 4,
      "parent_node": "Append"
    },
    {
      "severity": "warning",
      "type": "estimate_mismatch",
      "message": "Planner misestimated cardinality on 'Seq Scan' on 'data_partition_pruning_p202606': expected 242, got 27400.0 (ratio x113.2). Consider running ANALYZE.",
      "node": "Seq Scan",
      "depth": 4,
      "parent_node": "Append"
    }
  ],
  "plan_nodes": [
    {"depth": 0, "node_type": "Aggregate", "actual_rows": 1.0, "plan_rows": 1},
    {"depth": 1, "node_type": "Gather", "actual_rows": 3.0, "plan_rows": 2},
    {"depth": 2, "node_type": "Aggregate", "actual_rows": 1.0, "plan_rows": 1},
    {
      "depth": 3,
      "node_type": "Append",
      "actual_rows": 27400.0,
      "actual_loops": 3,
      "plan_rows": 2083,
      "parallel_aware": true,
      "shared_read_blocks": 31256
    },
    {"depth": 4, "node_type": "Seq Scan", "relation": "data_partition_pruning_p202601", "actual_rows": 0.0, "rows_removed_by_filter": 84939},
    {"depth": 4, "node_type": "Seq Scan", "relation": "data_partition_pruning_p202602", "actual_rows": 0.0, "rows_removed_by_filter": 76720},
    {"depth": 4, "node_type": "Seq Scan", "relation": "data_partition_pruning_p202603", "actual_rows": 0.0, "rows_removed_by_filter": 84940},
    {"depth": 4, "node_type": "Seq Scan", "relation": "data_partition_pruning_p202604", "actual_rows": 0.0, "rows_removed_by_filter": 82200},
    {"depth": 4, "node_type": "Seq Scan", "relation": "data_partition_pruning_p202605", "actual_rows": 0.0, "rows_removed_by_filter": 84940},
    {"depth": 4, "node_type": "Seq Scan", "relation": "data_partition_pruning_p202606", "actual_rows": 27400.0, "rows_removed_by_filter": 0},
    {"depth": 4, "node_type": "Seq Scan", "relation": "data_partition_pruning_p202607", "actual_rows": 0.0, "rows_removed_by_filter": 84940},
    {"depth": 4, "node_type": "Seq Scan", "relation": "data_partition_pruning_p202608", "actual_rows": 0.0, "rows_removed_by_filter": 84940},
    {"depth": 4, "node_type": "Seq Scan", "relation": "data_partition_pruning_p202609", "actual_rows": 0.0, "rows_removed_by_filter": 82193},
    {"depth": 4, "node_type": "Seq Scan", "relation": "data_partition_pruning_p202610", "actual_rows": 0.0, "rows_removed_by_filter": 84909},
    {"depth": 4, "node_type": "Seq Scan", "relation": "data_partition_pruning_p202611", "actual_rows": 0.0, "rows_removed_by_filter": 82170},
    {"depth": 4, "node_type": "Seq Scan", "relation": "data_partition_pruning_p202612", "actual_rows": 0.0, "rows_removed_by_filter": 84909}
  ]
}
```

## analysis

| metric           | value           |
|------------------|-----------------|
| execution time   | 149.41 ms       |
| partitions read  | **12 / 12**     |
| rows returned    | 27,400          |
| rows discarded   | ~925,000        |
| issues           | 14              |

### root cause: non-sargable predicate

`extract(month from ts) = 6` wraps the partition key in a function.
The planner cannot translate this into a partition boundary, so it
has no choice but to scan every partition. `PartitionPruningCheck`
fires — the message includes the list of scanned partitions and an
explicit note that `ANALYZE` will not help.

### consequences: `seq_scan` and `estimate_mismatch`

The 11 partitions that hold no June rows are read end-to-end and
discarded (`seq_scan` on each). The planner also misestimates
cardinality on the `Append` (13x) and on the June partition (113x) —
because it has no per-partition statistics to attribute rows to when
the predicate is non-sargable.

These are **not** independent problems. They are reported because the
walker checks every node, but their `depth` and `parent_node` make
the hierarchy visible:

- `depth: 3`, `parent_node: "Aggregate"` — root (`Append`).
- `depth: 4`, `parent_node: "Append"` — consequences.

The LLM groups them accordingly.

## recommendation

Rewrite the predicate so it compares `ts` directly against a range:

```sql
select count(*)
from mcp_explain_tool.data_partition_pruning
where ts >= '2026-06-01' and ts < '2026-07-01';
```

With this change the planner prunes 11 partitions and scans only
`p202606` — see
[`sample_partition_pruning_norm.md`](sample_partition_pruning_norm.md).

`ANALYZE` is **not** the fix here. Even with fresh statistics the
planner cannot map `extract(month from ts) = 6` onto partition
boundaries.

## what this proves

- `PartitionPruningCheck` detects failed pruning by counting `Append`
  children.
- the check's message explicitly rules out `ANALYZE` as a fix — this
  matters because LLMs default to `ANALYZE` whenever they see a
  cardinality misestimate.
- `Issue.depth` and `Issue.parent_node` let the assistant collapse
  14 individual warnings into two root causes and their consequences.

