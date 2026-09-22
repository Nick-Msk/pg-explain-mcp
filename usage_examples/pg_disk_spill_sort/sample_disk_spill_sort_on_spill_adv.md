# sample: large sort spills to disk (precise work_mem calculation)

> scenario 2b of 2 for `DiskSpillSortCheck` — the *precise* variant.
> scenario 2a is [`sample_disk_spill_sort_on_spill.md`](sample_disk_spill_sort_on_spill.md).

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_disk_spill_sort(1000000);
```

## context

`data_disk_spill_sort_spill` contains 1M rows with a wide `pad`
column (~200 bytes). the sort payload exceeds `work_mem`, so postgres
switches from an in-memory quicksort to an **external merge sort** and
writes temporary files to disk.

This example shows the **precise** workflow: the check's message
already contains a ready-to-use recommendation, so the assistant can
cite it verbatim without performing arithmetic — and without the
common LLM mistake of subtracting the current `work_mem` from the
spill size.

## input query

```sql
select *
from mcp_explain_tool.data_disk_spill_sort_spill
order by val;
```

## prompt to the assistant

> analyze the plan of
> ```sql
> select * from mcp_explain_tool.data_disk_spill_sort_spill order by val;
> ```
> calculate adjustments precisely

## assistant response

### analysis

| metric             | value                     |
|--------------------|---------------------------|
| execution time     | 750.34 ms                 |
| rows sorted        | 1,000,000                 |
| sort method        | `external merge`          |
| spill size         | 221,208 kB ≈ 216.0 MB     |
| issues             | 1 (`disk_spill_sort`)     |

### calculation

```
minimum work_mem  ≥  216 MB
recommended value  =  256 MB   (from the check message, rounded up)
```

### recommendation

```sql
set work_mem = '256MB';
```

`work_mem` is per-operation, not per-connection. Raising it affects
every sort and hash operation in the session, so the total memory
footprint scales with concurrency.

## raw tool output

```json
{
  "execution_time_ms": 750.335,
  "planning_time_ms": 5.107,
  "total_time_ms": 755.442,
  "issues": [
    {
      "severity": "warning",
      "type": "disk_spill_sort",
      "message": "Sort spilled to disk (221208kB ≈ 216.0 MB). Recommended fix: set work_mem to at least 216 MB; a safe round value is 256 MB. Note: sorts use work_mem directly — hash_mem_multiplier does not apply. Current work_mem is not part of this calculation.",
      "node": "Sort"
    }
  ],
  "issue_count": 1,
  "summary": "Found 1 issues (1 critical). Execution time: 750.34 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Sort",
      "actual_rows": 1000000.0,
      "actual_loops": 1,
      "plan_rows": 1000000,
      "shared_read_blocks": 15089,
      "sort_method": "external merge",
      "sort_space_type": "Disk",
      "sort_space_used_kb": 221208,
      "parallel_aware": false
    },
    {
      "depth": 1,
      "node_type": "Seq Scan",
      "relation": "data_disk_spill_sort_spill",
      "actual_rows": 1000000.0,
      "actual_loops": 1,
      "plan_rows": 1000000,
      "shared_read_blocks": 15089,
      "parallel_aware": false
    }
  ]
}
```

## design note: why the message contains the answer

An earlier iteration of `DiskSpillSortCheck` produced a message like:

> "Sort spilled to disk. Set `work_mem` above the reported spill size.
> Call `list_parameters` for the current `work_mem`."

The LLM consistently interpreted this as a delta problem and computed:

```
Minimum Increase = Spill Size − Current work_mem
                 = 221,208 − 20,480
                 = 200,728 kB
```

That arithmetic is wrong. The spill size is the **total data volume
to sort**, not an increment on top of the current setting. The formula
is `work_mem ≥ spill size`, with `work_mem` as the left-hand side and
no subtraction.

Adding a "Do not compute a delta" rule to the system prompt did not
fix the behaviour — the pattern is too well-represented in the
model's training data.

The fix was to remove the ambiguity from the **tool output itself**:

- the message now contains the concrete recommended value (`256 MB`),
- it states explicitly that current `work_mem` is not part of the
  calculation,
- and it removes the "call list_parameters for current work_mem"
  phrase that invited the subtraction.

With this change the LLM produces the correct answer verbatim. The
general lesson: **when a model reliably mis-reads a field, fix the
field — not the prompt.**

## what this proves

- `DiskSpillSortCheck` now emits a self-contained recommendation:
  spill size, minimum required `work_mem`, and a safe rounded value.
- the message removes the temptation to subtract the current setting.
- sorts use `work_mem` directly; `hash_mem_multiplier` does not apply.
- precise recommendations can be delivered from the tool alone,
  without relying on the assistant to combine multiple tool calls.

