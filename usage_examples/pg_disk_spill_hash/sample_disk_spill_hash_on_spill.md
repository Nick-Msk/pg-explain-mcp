# sample: hash join spills to disk

> scenario 2 of 2 for `DiskSpillHashCheck`.

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

## input query

```sql
select count(*)
from mcp_explain_tool.data_disk_spill_hash_spill a
join mcp_explain_tool.data_disk_spill_hash_spill b on a.val = b.val;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 900.091,
  "planning_time_ms": 6.185,
  "total_time_ms": 906.276,
  "issues": [
    {
      "severity": "warning",
      "type": "disk_spill_hash",
      "message": "Hash operation spilled to disk (per worker, 3 workers): 4 batches, peak 37536kB per batch, estimated full size ≈ 146.6 MB. To keep the hash table in memory, set work_mem such that work_mem × hash_mem_multiplier > estimated full size. Call list_parameters for the current hash_mem_multiplier.",
      "node": "Hash"
    }
  ],
  "issue_count": 1,
  "summary": "Found 1 issues (1 critical). Execution time: 900.09 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Aggregate",
      "actual_rows": 1.0,
      "actual_loops": 1,
      "plan_rows": 1,
      "shared_read_blocks": 15732,
      "parallel_aware": false
    },
    {
      "depth": 1,
      "node_type": "Gather",
      "actual_rows": 3.0,
      "actual_loops": 1,
      "plan_rows": 2,
      "shared_read_blocks": 15732,
      "parallel_aware": false
    },
    {
      "depth": 2,
      "node_type": "Aggregate",
      "actual_rows": 1.0,
      "actual_loops": 3,
      "plan_rows": 1,
      "shared_read_blocks": 15732,
      "parallel_aware": false
    },
    {
      "depth": 3,
      "node_type": "Hash Join",
      "actual_rows": 333333.33,
      "actual_loops": 3,
      "plan_rows": 416667,
      "shared_read_blocks": 15732,
      "parallel_aware": true
    },
    {
      "depth": 4,
      "node_type": "Seq Scan",
      "relation": "data_disk_spill_hash_spill",
      "actual_rows": 333333.33,
      "actual_loops": 3,
      "plan_rows": 416667,
      "shared_read_blocks": 7866,
      "parallel_aware": true
    },
    {
      "depth": 4,
      "node_type": "Hash",
      "actual_rows": 333333.33,
      "actual_loops": 3,
      "plan_rows": 416667,
      "shared_read_blocks": 7866,
      "hash_buckets": 524288,
      "hash_batches": 4,
      "peak_memory_usage_kb": 37536,
      "parallel_aware": true
    },
    {
      "depth": 5,
      "node_type": "Seq Scan",
      "relation": "data_disk_spill_hash_spill",
      "actual_rows": 333333.33,
      "actual_loops": 3,
      "plan_rows": 416667,
      "shared_read_blocks": 7866,
      "parallel_aware": true
    }
  ]
}
```

## analysis

| metric             | value                     |
|--------------------|---------------------------|
| execution time     | 900.09 ms                 |
| join rows          | 1,000,000 (333,333 × 3)   |
| hash buckets       | 524,288                   |
| hash batches       | **4**                     |
| peak memory        | 37,536 kB per batch       |
| estimated full size | ≈ 146.6 MB               |
| workers            | 3                         |
| issues             | 1                         |

the plan uses a **parallel hash join** across 3 workers. each worker
built its own hash table, and each exceeded `work_mem`, so postgres
partitioned the data into **4 batches** and wrote them to disk.

`DiskSpillHashCheck` fires on `hash_batches > 1`. it also combines:

- `peak_memory_usage_kb × hash_batches` → estimated full hash size,
- `parallel_aware` + `actual_loops` → parallel annotation,
- `disk_usage_kb`, when present.

### `seq_scan` stays silent

both `Seq Scan` nodes read 1M rows each, but the analyzer reports **no**
`seq_scan` warning. that is because the scans have **no filter** — a
hash join needs every row from the build side. an index would not
change the plan.

### recommendation

effective hash memory is `work_mem × hash_mem_multiplier`. to keep the
hash table in memory:

```
work_mem × hash_mem_multiplier  >  estimated full size
work_mem                        >  146.6 MB / hash_mem_multiplier
```

with the default `hash_mem_multiplier = 2`:

```
work_mem > 146.6 / 2 = 73.4 MB
```

a safe setting that leaves headroom for larger datasets:

```sql
set work_mem = '256MB';
```

**note on llm behaviour.** the message tells the model to look up
`hash_mem_multiplier` via `list_parameters`, but models sometimes skip
this step and recommend a safe value (e.g. 256 MB) without showing the
arithmetic. the precise minimum here is `146.6 / 2 ≈ 73.4 MB` — so
`128MB` would already be enough. both answers prevent the spill; the
second one just reserves more memory than strictly necessary.

this is a limitation of the LLM side, not of the analyzer: the check
emits all the facts required for a precise recommendation, and the
formula is spelled out in the `message`.

## what this proves

- `DiskSpillHashCheck` detects parallel hash spilling and reports the
  batch count, per-batch peak memory, estimated full size, and worker
  count in a single message.
- `summarize_plan_node` preserves the fields that matter for hash
  analysis: `hash_batches`, `hash_buckets`, `peak_memory_usage_kb`,
  `parallel_aware`, `actual_loops`.
- `SeqScanCheck` stays silent on unfiltered hash-build scans, so the
  only issue reported is the actual one.
- the effective hash budget is `work_mem × hash_mem_multiplier`, not
  `work_mem` alone — `hash_mem_multiplier` is not part of the plan,
  so it must be fetched separately.

