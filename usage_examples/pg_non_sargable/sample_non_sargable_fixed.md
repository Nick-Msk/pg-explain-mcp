# sample: after creating the functional index

> fix-verification scenario for `NonSargableCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_non_sargable(5000000);

-- The fix recommended in sample_non_sargable_trigger.md:
create index idx_data_non_sargable_lower_email
    on mcp_explain_tool.data_non_sargable (lower(email));
```

## context

The trigger scenario showed `lower(email) = '...'` forcing a full
table scan because no index matched the expression. The fix is a
functional index on `lower(email)`. With that index in place:

- the planner uses it directly (`Index Scan` on
  `idx_data_non_sargable_lower_email`);
- `SeqScanCheck` stops firing — the plan no longer contains a
  `Seq Scan`;
- `NonSargableCheck` stops firing — the predicate now appears in
  `Index Cond`, not in `Filter`.

This file confirms the fix. Same query, same table, only the index
was added.

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
  "execution_time_ms": 0.849,
  "planning_time_ms": 4.087,
  "total_time_ms": 4.936,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 0.85 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Index Scan",
      "actual_loops": 1,
      "actual_rows": 1.0,
      "index": "idx_data_non_sargable_lower_email",
      "parallel_aware": false,
      "plan_rows": 1,
      "relation": "data_non_sargable",
      "shared_read_blocks": 3
    }
  ]
}
```

## analysis

| metric          | value                                |
|-----------------|--------------------------------------|
| execution time  | 0.85 ms                              |
| rows returned   | 1                                    |
| access method   | `Index Scan`                         |
| index           | `idx_data_non_sargable_lower_email`  |
| disk blocks     | 3                                    |
| issues          | 0                                    |

### before vs. after

| metric          | [before](sample_non_sargable_trigger.md) | after (this file)  |
|-----------------|------------------------------------------|--------------------|
| execution time  | 221.98 ms                                | **0.85 ms**        |
| rows scanned    | ~1,000,000                               | 1                  |
| rows discarded  | ~1,000,000                               | 0                  |
| access method   | `Parallel Seq Scan`                      | `Index Scan`       |
| `shared_read_blocks` | 22,274                             | **3**              |
| issues          | 2 (`seq_scan` + `non_sargable`)          | **0**              |

### why `NonSargableCheck` stops firing

The check looks for `Filter` entries that wrap an indexed column in
a function. In the new plan the plan node has **no `Filter` at all**
— the predicate `lower(email) = '...'` was pushed into `Index Cond`
on the `Index Scan`. That is exactly the behaviour the functional
index enables: the expression `lower(email)` now matches the indexed
expression, so the planner can use the index.

The check cannot distinguish "sargable because the index matches the
expression" from "sargable because the column is used directly". Both
produce the same JSON shape (`Index Cond`, no `Filter`), and both
mean the predicate is index-friendly. That is all the check cares
about.

### why `SeqScanCheck` stops firing

The plan contains no `Seq Scan` node. There is nothing to inspect.

## what this proves

- the fix recommended by `NonSargableCheck` actually works: after
  adding the functional index, the query goes from ~222 ms to
  ~0.85 ms, a 260× speedup.
- the analyzer does not need to know about the fix — it simply
  reports the new plan, and the same checks stay silent because the
  plan is now clean.
- `NonSargableCheck` is **not** a heuristic about good or bad SQL. It
  reports facts about the plan: `Filter` + function around an indexed
  column. When the condition disappears, so does the warning.
- running the same check before and after a fix is a useful workflow:
  it confirms both the diagnosis and the outcome.

## sequence

1. [norm](sample_non_sargable_norm.md) — sargable predicate,
   `Index Scan`, no issues.
2. [trigger](sample_non_sargable_trigger.md) — non-sargable
   predicate, `seq_scan` + `non_sargable`, 222 ms.
3. **this file** — trigger query + functional index, `Index Scan`,
   no issues, 0.85 ms.

