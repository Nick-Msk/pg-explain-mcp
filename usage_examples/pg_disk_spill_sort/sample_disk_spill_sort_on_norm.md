# sample: small sort stays in memory

> scenario 1 of 2 for `DiskSpillSortCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_disk_spill_sort(1000000);
```

## context

`data_disk_spill_sort_norm` contains 999 rows with a narrow `pad`
column (~50 bytes). the total sort payload is well under the default
`work_mem` (4–20 MB), so postgres sorts the data entirely in memory —
no disk spill.

## input query

```sql
select *
from mcp_explain_tool.data_disk_spill_sort_norm
order by val;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 1.207,
  "planning_time_ms": 4.155,
  "total_time_ms": 5.362,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 1.21 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Sort",
      "actual_rows": 999.0,
      "plan_rows": 999,
      "shared_read_blocks": 0,
      "sort_method": "quicksort"
    },
    {
      "depth": 1,
      "node_type": "Seq Scan",
      "relation": "data_disk_spill_sort_norm",
      "actual_rows": 999.0,
      "plan_rows": 999,
      "shared_read_blocks": 0
    }
  ]
}
```

## analysis

| metric          | value    |
|-----------------|----------|
| execution time  | 1.21 ms  |
| rows sorted     | 999      |
| sort method     | quicksort (in memory) |
| disk spill      | no       |
| issues          | 0        |

the plan uses a **quicksort**, which runs entirely in memory. the
`sort_method` field in `plan_nodes` shows `quicksort` — `DiskSpillSortCheck`
only fires when the method starts with `external`, so it stays silent.

`SeqScanCheck` also stays silent: the table has only 999 rows, just below
its `threshold_rows = 1000`.

## what this proves

- `DiskSpillSortCheck` does not produce false positives when the sort
  fits in `work_mem`.
- the plan tree exposes `sort_method` so the llm can distinguish
  in-memory from on-disk sorts without guessing.
- the two thresholds (1000 rows for seq_scan, 999 for this fixture) keep
  examples independent — each one isolates a single check.

## next step

see [`sample_disk_spill_sort_on_spill.md`](sample_disk_spill_sort_on_spill.md)
for the same query against a table large enough to spill.

