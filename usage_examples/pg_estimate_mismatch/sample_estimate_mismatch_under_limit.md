# sample: misestimate under a deep Limit — check stays silent

> edge-case scenario for `EstimateMismatchCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_estimate_mismatch(1000000);
```

Same fixture as the other `pg_estimate_mismatch/` examples:
`data_estimate_mismatch_skewed` has fake MCV statistics
(`most_common_freqs = {0.95}` for `val = 42`), while the actual data
is uniform.

## context

The check compares `plan_rows` against `actual_rows` on every plan
node. When a node sits **under a `Limit`**, those two numbers are not
comparable:

- `plan_rows` is the planner's estimate for the **full scan** — what
  the node would return if nothing above it stopped it.
- `actual_rows` is the number of rows the node *actually* emitted,
  which is usually far smaller because the Limit terminated the scan
  early.

Comparing them produces a large spurious ratio on every
`ORDER BY … LIMIT` query, even when the plan is perfectly correct.
The check therefore suppresses any node that has a `Limit` anywhere
up its ancestor chain.

This example exercises that suppression with `Limit` **three levels
above** the misestimated scan:

```
Limit
  └─ Gather Merge
      └─ Sort (top-N heapsort)
          └─ Parallel Seq Scan   ← plan 395 833, actual 3 389 (x117)
```

`Sort`, not `Limit`, is the scan's direct parent. The check cannot
rely on the immediate parent alone — it must walk the whole chain.

## input query

```sql
select *
from mcp_explain_tool.data_estimate_mismatch_skewed
where val = 42
order by pad
limit 1000;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 139.774,
  "planning_time_ms": 5.15,
  "total_time_ms": 144.924,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 139.77 ms.",
  "plan_nodes": [
    {
      "id": 0,
      "parent_id": null,
      "depth": 0,
      "path": "0:0",
      "children_ids": [1],
      "node_type": "Limit",
      "plan_rows": 1000,
      "actual_rows": 1000.0,
      "actual_loops": 1,
      "shared_read_blocks": 15372
    },
    {
      "id": 1,
      "parent_id": 0,
      "depth": 1,
      "path": "0:0/1:0",
      "children_ids": [2],
      "node_type": "Gather Merge",
      "plan_rows": 949999,
      "actual_rows": 1000.0,
      "actual_loops": 1,
      "workers_planned": 2,
      "workers_launched": 2
    },
    {
      "id": 2,
      "parent_id": 1,
      "depth": 2,
      "path": "0:0/1:0/2:0",
      "children_ids": [3],
      "node_type": "Sort",
      "plan_rows": 395833,
      "actual_rows": 515.33,
      "actual_loops": 3,
      "sort_key": ["pad"],
      "sort_method": "top-N heapsort",
      "sort_space_used": 306,
      "sort_space_type": "Memory"
    },
    {
      "id": 3,
      "parent_id": 2,
      "depth": 3,
      "path": "0:0/1:0/2:0/3:0",
      "children_ids": [],
      "node_type": "Seq Scan",
      "relation_name": "data_estimate_mismatch_skewed",
      "plan_rows": 395833,
      "actual_rows": 3389.33,
      "actual_loops": 3,
      "filter": "(val = 42)",
      "rows_removed_by_filter": 329944
    }
  ]
}
```

## analysis

| node           | `plan_rows` | `actual_rows` | ratio    |
|----------------|-------------|---------------|----------|
| Limit          | 1 000       | 1 000         | 1.0x     |
| Gather Merge   | 949 999     | 1 000         | 950x     |
| Sort           | 395 833     | 515.33        | 768x     |
| Seq Scan       | 395 833     | 3 389.33      | **117x** |

`issue_count: 0` — despite the misestimates visible in `plan_nodes`.

### why the check stays silent

`EstimateMismatchCheck.gather_info` returns `None` for any node where
`node.is_under("Limit")` is `True`. For the `Seq Scan`:

```
Seq Scan ──parent──> Sort ──parent──> Gather Merge ──parent──> Limit
```

Three levels up, one `Limit`. `is_under("Limit")` walks the whole
ancestor chain in O(depth) and returns `True`. The check exits before
computing the ratio.

The old direct-parent check (`node.parent.node_type == "Limit"`)
would have seen `Sort`, not `Limit`, and fired — producing a
spurious `estimate_mismatch` warning on every `ORDER BY … LIMIT`
query in the wild.

### the misestimate is real — it is just not actionable

The planner **is** wrong: it thinks 395 833 rows match `val = 42` per
worker (95 % of the table). In reality, ~3 389 per worker.

But with `LIMIT 1000` on top, the misestimate does not change the
plan:

- top-N heapsort keeps at most N=1000 rows per worker in memory —
  306 kB regardless of how many input rows there are.
- the `Gather Merge` stops early.
- no temp files, no disk spill, execution is bounded by the scan's
  I/O time, not by the sort.

The `Seq Scan` cost estimate is inflated by the wrong selectivity,
but there is no cheaper alternative path (no index on `val` in this
fixture) and the Limit caps the work.

### what would change the picture

If `val` had an index and the planner knew the true selectivity
(~1 %), it would pick an index scan under the same Limit — reading
~10 000 rows instead of 1 000 000. **That** case is caught elsewhere:
`list_indexes` would show the index, and the plan shape itself would
change. `EstimateMismatchCheck` is not the right tool to police it,
because the issue is not "planner estimate is off" but "planner picked
the wrong access path" — which shows up as a different plan shape, not
as a ratio inside the same plan.

## what this proves

- `EstimateMismatchCheck` uses the **whole ancestor chain**, not just
  the direct parent, when deciding whether to suppress a misestimate.
  Sort, Gather Merge, Nested Loop — any node between Limit and the
  scan — do not bypass the guard.
- `plan_rows` on a node under a Limit is the full-scan estimate. It
  is the wrong baseline for comparing against `actual_rows`, which
  the Limit has already truncated.
- the LLM may still mention the misestimate in prose — it reads
  `plan_nodes` directly. This is intentional: the check is a
  deterministic signal, and the assistant adds context. A clean
  `issues` array does not mean "there is nothing to think about"; it
  means "no rule fired".

## comparison with the other `estimate_mismatch` examples

| file | Limit above? | misestimate fired? |
|---|---|---|
| [norm](sample_estimate_mismatch_on_norm.md) | no | — |
| [skewed](sample_estimate_mismatch_on_skewed.md) | no | **yes**, x94 |
| [skewed — other value](sample_estimate_mismatch_on_skewed_other_value.md) | no | — |
| [skewed — leak](sample_estimate_mismatch_on_skewed_leak.md) | no | — |
| **this file** | **yes, 3 levels up** | — (suppressed) |

## test environment

- **LLM**: `qwen 3.8 27b-splash` via LM Studio
- **PostgreSQL**: 18.6

Qwen produces structured reports with tables and headers. The raw
tool output above is model-independent; only the surrounding prose
changes.
