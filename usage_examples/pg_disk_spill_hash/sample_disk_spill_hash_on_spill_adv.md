# sample: hash join spills to disk (precise work_mem calculation)

> scenario 2b of 2 for `DiskSpillHashCheck` — the *precise* variant.
> scenario 2a is [`sample_disk_spill_hash_on_spill.md`](sample_disk_spill_hash_on_spill.md).

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_disk_spill_hash(1000000);
```

## context

`data_disk_spill_hash_spill` contains 1M rows with a wide `val`
key (~96 bytes). the hash table built for the self-join exceeds
`work_mem`, so postgres partitions it into multiple batches and writes
the excess to temporary files on disk.

This example shows the **precise** workflow: the assistant looks up
`hash_mem_multiplier` via `list_parameters` and shows the arithmetic
instead of guessing a value.

## input query

```sql
select count(*)
from mcp_explain_tool.data_disk_spill_hash_spill a
join mcp_explain_tool.data_disk_spill_hash_spill b on a.val = b.val;
```

## prompt to the assistant

> analyze plan for
> ```sql
> select count(*)
> from mcp_explain_tool.data_disk_spill_hash_spill a
> join mcp_explain_tool.data_disk_spill_hash_spill b on a.val = b.val;
> ```
> calculate adjustments precisely

The phrase **"calculate adjustments precisely"** is the key trigger:
it forces the assistant to call `list_parameters` and show the
arithmetic, instead of recommending a generic safe value.

## assistant response

The assistant made **two** tool calls:

1. `pg-explain.explain` — returns the plan with `disk_spill_hash`.
2. `pg-explain.list_parameters` — returns `hash_mem_multiplier = 2`.

### analysis

| metric              | value                     |
|---------------------|---------------------------|
| execution time      | 900.09 ms                 |
| join rows           | 1,000,000 (333,333 × 3)   |
| hash batches        | **4**                     |
| peak memory         | 37,536 kB per batch       |
| estimated full size | ≈ 146.6 MB                |
| workers             | 3                         |
| `hash_mem_multiplier` | 2                       |
| issues              | 1 (`disk_spill_hash`)     |

### calculation

```
work_mem × hash_mem_multiplier  >  estimated full size
work_mem × 2                    >  146.6 MB
work_mem                        >  73.3 MB
```

### recommendation

```sql
set work_mem = '128MB';   -- safety margin above the 73.3 MB minimum
```

The value is derived from the formula — not picked arbitrarily.

## raw tool output

The model combined the two tool results into a single structured
answer:

```json
{
  "analysis": {
    "issue_detected": true,
    "issue_details": {
      "type": "disk_spill_hash",
      "severity": "warning",
      "message": "Hash operation spilled to disk (per worker, 3 workers): 4 batches, peak 37536kB per batch, estimated full size ≈ 146.6 MB."
    },
    "parameters_detected": {
      "hash_mem_multiplier": 2
    },
    "calculation": {
      "formula": "work_mem * hash_mem_multiplier > estimated_full_size",
      "estimated_full_size_mb": 146.6,
      "hash_mem_multiplier": 2,
      "minimum_work_mem_mb": 73.3
    },
    "recommendation": {
      "action": "Increase work_mem to prevent disk spilling.",
      "suggested_value": "128MB",
      "sql_command": "SET work_mem = '128MB';"
    }
  }
}
```

The underlying tool outputs (the plan and the parameters) are shown
verbatim in scenario 2a — the JSON above is the assistant's summary.

## why the "precise" variant matters

compare the two scenarios:

| | scenario 2a | scenario 2b (this file) |
|---|---|---|
| prompt | `analyze the plan ...` | `analyze plan ... calculate adjustments precisely` |
| `list_parameters` called | no | yes |
| arithmetic shown | no | yes |
| recommended `work_mem` | 256 MB | 128 MB |
| derived from formula | no | yes |

both answers prevent the spill. scenario 2b is **more precise** and
uses less memory — which matters when `shared_buffers` is small or
when many concurrent queries each request their own `work_mem`.

## prompting tip

The directive **"calculate adjustments precisely"** is worth keeping
in mind for any `work_mem` / `shared_buffers` / `random_page_cost`
recommendation. Without it, the model tends to:

- skip `list_parameters`,
- fall back to a familiar value from its training data (e.g. 256 MB),
- omit the arithmetic entirely.

With it, the model commits to a calculation and looks up the missing
parameter.

## what this proves

- `DiskSpillHashCheck` emits all the facts needed for a precise
  recommendation: batch count, per-batch peak memory, estimated full
  size, worker count, and the formula itself.
- the missing piece — `hash_mem_multiplier` — is exposed by a separate
  tool (`list_parameters`), so the analyzer stays read-only and does
  not need to reach into `pg_settings` on its own.
- with an explicit prompt directive, the assistant produces a
  **derived** answer, not a generic one.

