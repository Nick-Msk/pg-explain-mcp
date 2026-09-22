# sample: efficient Nested Loop with a small outer table

> scenario 1 of 2 for `NestedLoopCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_nested_loop(1000000);
```

## context

`data_nested_loop_norm` contains 100 rows; `data_nested_loop_inner`
contains 1M rows with an index on `val`. the planner picks a Nested
Loop: 100 outer rows × ~1 indexed lookup each. this is the optimal
strategy at this scale — a Hash Join would need to build a 1M-row
hash table, which is far more expensive.

`NestedLoopCheck` stays silent because the inner-side loop count is
only 100, well below its `threshold_loops = 1000`.

## input query

```sql
select count(*)
from mcp_explain_tool.data_nested_loop_norm n
join mcp_explain_tool.data_nested_loop_inner i on i.val = n.val;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 2.006,
  "planning_time_ms": 5.447,
  "total_time_ms": 7.453,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 2.01 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Aggregate",
      "actual_rows": 1.0,
      "actual_loops": 1,
      "plan_rows": 1,
      "shared_read_blocks": 0,
      "parallel_aware": false
    },
    {
      "depth": 1,
      "node_type": "Nested Loop",
      "actual_rows": 104.0,
      "actual_loops": 1,
      "plan_rows": 202,
      "shared_read_blocks": 0,
      "parallel_aware": false
    },
    {
      "depth": 2,
      "node_type": "Seq Scan",
      "relation": "data_nested_loop_norm",
      "actual_rows": 100.0,
      "actual_loops": 1,
      "plan_rows": 100,
      "shared_read_blocks": 0,
      "parallel_aware": false
    },
    {
      "depth": 2,
      "node_type": "Index Only Scan",
      "relation": "data_nested_loop_inner",
      "index": "idx_data_nested_loop_inner_val",
      "actual_rows": 1.04,
      "actual_loops": 100,
      "plan_rows": 2,
      "heap_fetches": 0,
      "shared_read_blocks": 0,
      "parallel_aware": false
    }
  ]
}
```

## analysis

| metric            | value      |
|-------------------|------------|
| execution time    | 2.01 ms    |
| outer rows        | 100        |
| inner-side loops  | **100**    |
| rows per loop     | ~1.04      |
| issues            | 0          |

the plan uses a Nested Loop with an **Index Only Scan** on the inner
side. each iteration touches a single index entry, and `heap_fetches`
is 0 — nothing is fetched from the heap. the loop count (100) is far
below the check's threshold (1000), so no issue is emitted.

### why `seq_scan` stays silent

the outer table is read with `Seq Scan`, and normally that would
trigger `SeqScanCheck`. here it does not, because the scan has **no
filter** — an index on a 100-row table would not change the plan.
`SeqScanCheck` ignores scans with `Rows Removed by Filter = 0`.

## what this proves

- `NestedLoopCheck` does not fire on efficient Nested Loops with a
  small outer table.
- the loop count is read from the **inner** child (`plans[1]`), not
  from the Nested Loop node itself — the outer node always reports
  `actual_loops = 1`.
- `SeqScanCheck` correctly ignores unfiltered full scans.

## next step

see [`sample_nested_loop_on_many.md`](sample_nested_loop_on_many.md)
for a Nested Loop with 5000 inner iterations.

