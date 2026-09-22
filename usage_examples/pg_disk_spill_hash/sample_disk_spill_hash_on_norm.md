# sample: in-memory hash join

> scenario 1 of 2 for `DiskSpillHashCheck`.

## prerequisites

```sql
create extension mcp_explain_tool;
call mcp_explain_tool.fill_disk_spill_hash(1000000);
```

## context

`data_disk_spill_hash_norm` contains 25,000 rows with a wide `val`
key (~96 bytes). the hash table built for the self-join fits entirely
in `work_mem`, so the join completes in memory without touching disk.

## input query

```sql
select count(*)
from mcp_explain_tool.data_disk_spill_hash_norm a
join mcp_explain_tool.data_disk_spill_hash_norm b on a.val = b.val;
```

## prompt to the assistant

> analyze the plan for the query above.
> also show me the raw json output you received.

## raw output from pg-explain

```json
{
  "execution_time_ms": 61.926,
  "planning_time_ms": 11.264,
  "total_time_ms": 73.19,
  "issues": [],
  "issue_count": 0,
  "summary": "No issues found. Execution time: 61.93 ms.",
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
      "node_type": "Hash Join",
      "actual_rows": 25000.0,
      "actual_loops": 1,
      "plan_rows": 25000,
      "shared_read_blocks": 0,
      "parallel_aware": false
    },
    {
      "depth": 2,
      "node_type": "Seq Scan",
      "relation": "data_disk_spill_hash_norm",
      "actual_rows": 25000.0,
      "actual_loops": 1,
      "plan_rows": 25000,
      "shared_read_blocks": 0,
      "parallel_aware": false
    },
    {
      "depth": 2,
      "node_type": "Hash",
      "actual_rows": 25000.0,
      "actual_loops": 1,
      "plan_rows": 25000,
      "shared_read_blocks": 0,
      "parallel_aware": false,
      "hash_buckets": 32768,
      "hash_batches": 1,
      "peak_memory_usage_kb": 3406
    },
    {
      "depth": 3,
      "node_type": "Seq Scan",
      "relation": "data_disk_spill_hash_norm",
      "actual_rows": 25000.0,
      "actual_loops": 1,
      "plan_rows": 25000,
      "shared_read_blocks": 0,
      "parallel_aware": false
    }
  ]
}
```

## analysis

| metric             | value      |
|--------------------|------------|
| execution time     | 61.93 ms   |
| join rows          | 25,000     |
| hash buckets       | 32,768     |
| hash batches       | **1**      |
| peak memory usage  | 3,406 kB   |
| issues             | 0          |

the plan uses a **single-batch hash join** — the whole hash table fits
in `work_mem` and stays in memory. `hash_batches: 1` is the key signal:
`DiskSpillHashCheck` only fires when batches exceed 1.

### why `seq_scan` stays silent

both sides of the join are read with `Seq Scan`, and normally that
would trigger `SeqScanCheck`. here it does not, because the scan has
**no filter** — the planner must read the whole table to build the
hash, and an index would not change the plan. `SeqScanCheck` ignores
scans with `Rows Removed by Filter = 0`.

## what this proves

- `DiskSpillHashCheck` does not produce false positives when the hash
  table fits in memory.
- the plan tree exposes `hash_batches`, `hash_buckets`, and
  `peak_memory_usage_kb`, so the llm can reason about hash behaviour
  without guessing.
- `SeqScanCheck` correctly stays silent on unfiltered full scans,
  preventing misleading "create index" advice on hash-join build sides.

## next step

see [`sample_disk_spill_hash_on_spill.md`](sample_disk_spill_hash_on_spill.md)
for the same query against a table that exceeds `work_mem`.

