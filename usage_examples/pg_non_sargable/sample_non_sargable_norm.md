# sample: sargable predicate uses the index

> baseline scenario for `NonSargableCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_non_sargable(5000000);
```

## context

`data_non_sargable` has a plain index on `email`. The query uses a
**sargable** predicate — the column is compared directly against a
constant, with no function wrapping and no cast. The planner uses
`idx_data_non_sargable_email` and reads exactly one row.

`NonSargableCheck` stays silent: the `Filter` field is absent from the
plan, which means the predicate was pushed down into `Index Cond`. By
definition, an indexed condition is sargable.

## input query

```sql
select *
from mcp_explain_tool.data_non_sargable
where email = 'user0000000003@example.com';
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
  "execution_time_ms": 0.202,
  "planning_time_ms": 3.682,
  "total_time_ms": 3.884,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 0.20 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Index Scan",
      "actual_loops": 1,
      "actual_rows": 1.0,
      "index": "idx_data_non_sargable_email",
      "parallel_aware": false,
      "plan_rows": 1,
      "relation": "data_non_sargable",
      "shared_read_blocks": 0
    }
  ]
}
```

## analysis

| metric         | value     |
|----------------|-----------|
| execution time | 0.20 ms   |
| rows returned  | 1         |
| access method  | `Index Scan` |
| index          | `idx_data_non_sargable_email` |
| issues         | 0         |

### why `NonSargableCheck` stays silent

The check fires only on `Filter` entries that wrap an indexed column
in a function. Here the plan has **no `Filter` at all** — the
predicate lives in `Index Cond` on the `Index Scan` node, meaning the
index was usable. The check has nothing to inspect, so it returns no
issue.

### why `SeqScanCheck` stays silent

The plan uses `Index Scan`, not `Seq Scan`. `SeqScanCheck` only
inspects `Seq Scan` nodes, so it never even looks at this plan.

### why `IndexScanCheck` stays silent

`IndexScanCheck` looks for stale visibility maps (Index Only Scan with
high `Heap Fetches`) or poor heap locality (Index Scan reading many
disk blocks). Here the scan reads 1 row, touches 0 disk blocks, and
the index is a plain `Index Scan` — nothing to flag.

## what this proves

- `NonSargableCheck` does not produce false positives on sargable
  predicates.
- the `Filter` vs. `Index Cond` distinction is what tells the check
  whether a column is being used through an index or evaluated
  row-by-row. Both are visible in the JSON plan.
- a well-tuned query produces zero issues across all checks. The
  analyzer does not manufacture warnings to justify its existence.

## next step

see
[`sample_non_sargable_trigger.md`](sample_non_sargable_trigger.md)
for the same table queried with a non-sargable predicate —
`lower(email) = '...'` — where the index becomes unusable and
`NonSargableCheck` fires.

