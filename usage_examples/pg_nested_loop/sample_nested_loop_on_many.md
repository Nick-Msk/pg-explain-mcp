# sample: Nested Loop with 5000 inner iterations

> scenario 2 of 2 for `NestedLoopCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_nested_loop(1000000);
```

## context

`data_nested_loop_many` contains 5000 rows; `data_nested_loop_inner`
contains 1M rows with an index on `val`. the planner still picks a
Nested Loop — 5000 outer rows × ~1 indexed lookup each. even at 5000
iterations this is faster than a Hash Join on a 1M-row table.

`NestedLoopCheck` fires, but at **`INFO`** level, not `WARNING`. this
is intentional: the plan is optimal for the current data. the check
is a heads-up for future growth, not a fix-me signal.

## input query

```sql
select count(*)
from mcp_explain_tool.data_nested_loop_many n
join mcp_explain_tool.data_nested_loop_inner i on i.val = n.val;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 44.767,
  "planning_time_ms": 5.877,
  "total_time_ms": 50.644,
  "issues": [
    {
      "severity": "info",
      "type": "nested_loop",
      "message": "Nested Loop ran the inner side 5000 times ('Index Only Scan' on 'data_nested_loop_inner', ~0.98 rows per loop). This is optimal for the current data, but execution time grows linearly with the outer row count — re-check if the outer side becomes much larger.",
      "node": "Nested Loop"
    }
  ],
  "issue_count": 1,
  "summary": "Found 1 issues (0 critical). Execution time: 44.77 ms.",
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
      "actual_rows": 4914.0,
      "actual_loops": 1,
      "plan_rows": 10091,
      "shared_read_blocks": 0,
      "parallel_aware": false
    },
    {
      "depth": 2,
      "node_type": "Seq Scan",
      "relation": "data_nested_loop_many",
      "actual_rows": 5000.0,
      "actual_loops": 1,
      "plan_rows": 5000,
      "shared_read_blocks": 0,
      "parallel_aware": false
    },
    {
      "depth": 2,
      "node_type": "Index Only Scan",
      "relation": "data_nested_loop_inner",
      "index": "idx_data_nested_loop_inner_val",
      "actual_rows": 0.98,
      "actual_loops": 5000,
      "plan_rows": 2,
      "heap_fetches": 0,
      "shared_read_blocks": 0,
      "parallel_aware": false
    }
  ]
}
```

## analysis

| metric            | value          |
|-------------------|----------------|
| execution time    | 44.77 ms       |
| outer rows        | 5000           |
| inner-side loops  | **5000**       |
| rows per loop     | ~0.98          |
| issues            | 1 (INFO)       |

the plan is the same shape as scenario 1: Nested Loop over a `Seq
Scan` of the outer side, with an `Index Only Scan` on the inner side.
the only difference is the loop count — 5000 instead of 100 — which
crosses the check's threshold (`threshold_loops = 1000`).

### why the check fires at `INFO`, not `WARNING`

a Nested Loop with an **indexed** inner side and ~1 row per lookup is
not a problem. it is the correct choice whenever the outer side is
small enough that the total number of lookups is cheaper than
building a hash table. the analyzer therefore emits an `INFO` note:
useful when reviewing a plan at scale, harmless otherwise.

### what would actually be a problem

if the planner used Nested Loop **without** an index on the inner
side, each iteration would scan the whole inner table. but the
planner almost never does this — it would choose Hash Join instead.
the realistic failure mode is a **misestimated cardinality**: the
planner believes each lookup returns 1 row, but the data distribution
makes some lookups return thousands. that case is caught by
`EstimateMismatchCheck`, not here.

## what this proves

- `NestedLoopCheck` reads the loop count from the **inner** child
  (`plans[1]`). the Nested Loop node itself always reports
  `actual_loops = 1`; a naive check would never fire.
- the check distinguishes severity: `INFO` for an optimal plan,
  leaving room for `WARNING` checks (like `seq_scan`) to remain
  visually prominent.
- the message is intentionally worded as a future-looking note, not
  as an instruction to change the query.

## next step

see [`sample_nested_loop_on_norm.md`](sample_nested_loop_on_norm.md)
for the same join shape with a smaller outer table.

