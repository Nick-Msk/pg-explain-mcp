# sample: large sort spills to disk

> scenario 2 of 2 for `DiskSpillSortCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_disk_spill_sort(1000000);
```

## context

`data_disk_spill_sort_spill` contains 1M rows with a wide `pad`
column (~200 bytes). the total sort payload exceeds `work_mem`, so
postgres switches from an in-memory quicksort to an **external merge
sort**, writing temporary data to disk.

## input query

```sql
select *
from mcp_explain_tool.data_disk_spill_sort_spill
order by val;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 684.233,
  "planning_time_ms": 4.985,
  "total_time_ms": 689.218,
  "issues": [
    {
      "severity": "warning",
      "type": "disk_spill_sort",
      "message": "Sort spilled to disk (221208kB ≈ 216.0MB written to disk — set work_mem above this value or add an index to avoid the sort). Increase work_mem or optimize the query.",
      "node": "Sort"
    },
    {
      "severity": "warning",
      "type": "seq_scan",
      "message": "Sequential scan on 'data_disk_spill_sort_spill' processed 1000000.0 rows. Consider adding an index.",
      "node": "Seq Scan"
    }
  ],
  "issue_count": 2,
  "summary": "Found 2 issues (2 critical). Execution time: 684.23 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Sort",
      "actual_rows": 1000000.0,
      "plan_rows": 1000000,
      "shared_read_blocks": 14794,
      "sort_method": "external merge",
      "sort_space_type": "Disk",
      "sort_space_used_kb": 221208
    },
    {
      "depth": 1,
      "node_type": "Seq Scan",
      "relation": "data_disk_spill_sort_spill",
      "actual_rows": 1000000.0,
      "plan_rows": 1000000,
      "shared_read_blocks": 14794
    }
  ]
}
```

## analysis

| metric          | value                  |
|-----------------|------------------------|
| execution time  | 684.23 ms              |
| rows sorted     | 1,000,000              |
| sort method     | external merge (disk)  |
| spill size      | 221,208 kB ≈ 216 MB    |
| issues          | 2                      |

the plan has two separate problems, each reported by its own check.

### 1. disk_spill_sort

`DiskSpillSortCheck` inspects the `Sort` node. PostgreSQL splits the
spill information across three JSON fields:

- `sort_method: "external merge"`
- `sort_space_type: "Disk"`
- `sort_space_used_kb: 221208`

the check combines them into a message that includes the exact spill
size, so the LLM can recommend a realistic `work_mem` value.

### 2. seq_scan

`SeqScanCheck` also fires because the underlying `Seq Scan` read 1M rows.
that is expected for this fixture — see
[`../pg_seq_scan_adapters/`](../pg_seq_scan_adapters/) for the dedicated
`SeqScanCheck` examples. here it is a secondary signal, not the main one.

## recommendations

### quick fix — raise `work_mem`

the analyzer reports a spill of **221,208 kB ≈ 216 MB**. to make the
sort fit in memory, `work_mem` must exceed that value:

```sql
set work_mem = '512MB';
```

| work_mem | sort method     | spill size              |
|----------|-----------------|-------------------------|
| 20 MB    | external merge  | 221,208 kB (still spills) |
| 64 MB    | external merge  | 221,144 kB (still spills) |
| 256 MB   | quicksort       | — (fits in memory)      |
| 512 MB   | quicksort       | — (fits in memory)      |

note: a generic "set work_mem = 64MB" advice would **not** help here.
the recommendation must be based on the reported spill size.

### structural fix — add an index on `val`

```sql
create index idx_data_disk_spill_sort_spill_val
    on mcp_explain_tool.data_disk_spill_sort_spill (val);
```

with an index, the planner can read rows already sorted by `val`,
eliminating both the `Sort` node and the `Seq Scan`.

which fix is right depends on the workload: `work_mem` helps all queries
in the session, an index helps only those that filter or sort by `val`.

## what this proves

- `DiskSpillSortCheck` extracts the spill size from the JSON plan
  (`sort_space_used_kb`) and includes it in the message.
- the LLM can now recommend a **specific, data-driven** `work_mem` value
  instead of guessing.
- multiple checks can fire on the same plan — they are independent and
  each answers a different question.

