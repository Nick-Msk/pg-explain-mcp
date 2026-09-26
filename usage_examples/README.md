# usage examples

Real-world runs of `pg-explain-mcp` against PostgreSQL.

## test environment

All examples in this directory were captured on the following setup.
Results may vary on different hardware, PostgreSQL versions, or
configuration — especially numbers like execution time, spill size,
and `shared_read_blocks`.

| component          | value                                       |
|--------------------|---------------------------------------------|
| machine            | MacBook Pro (Apple Silicon, arm64)          |
| OS                 | macOS 26.x (Tahoe)                          |
| PostgreSQL         | 18.6 (built from source)                    |
| `work_mem`         | 20 MB                                       |
| `shared_buffers`   | 128 MB                                      |
| `random_page_cost` | 2 (configured for SSD; default is 4)        |
| Python             | 3.12.14 (Homebrew)                          |
| MCP client         | Continue.dev                                |
| LLM                | `google/gemma-4-26b-a4b-qat` via LM Studio  |

The two tables per adapter are sized so that:

- the `_norm` table stays **below** the thresholds of every `PlanCheck`
  (so no warnings are emitted), and
- the `_<failing>` table **exceeds** the threshold of exactly one check.

This keeps each example isolated: the `_norm` case should report
`issue_count: 0`, and the `_<failing>` case should report only the
specific issue the example is about — plus, occasionally, an
unrelated `seq_scan` when the underlying scan reads more than
1000 rows (see [`pg_seq_scan_adapters`/](pg_seq_scan_adapters/) for
details).

## prerequisites

Most examples share the same setup — install the
[`mcp_explain_tool`](../fixtures/) extension and populate the tables
needed for a specific check.

```sql
create extension mcp_explain_tool;
```

Or populate everything at once:

```sql
call mcp_explain_tool.fill_all(1000000);
```

See [`../fixtures/README.md`](../fixtures/README.md) for details.

## prompting tips

Some examples produce better results with an explicit directive in the
prompt:

- **`calculate adjustments precisely`** — forces the model to call
  `list_parameters` and show the arithmetic instead of recommending a
  generic value. See the precise variants:
  - [disk_spill_hash](pg_disk_spill_hash/sample_disk_spill_hash_on_spill_adv.md)
  - [disk_spill_sort](pg_disk_spill_sort/sample_disk_spill_sort_on_spill_adv.md)
- **`show me the raw json output`** — makes the model quote the tool
  output verbatim, useful for debugging.

---

## IndexScanCheck: before / after

Reproducible two-part scenario backed by
`mcp_explain_tool.data_index_scan_norm` and
`mcp_explain_tool.data_index_scan_unclastered`.

Prerequisites:

```sql
call mcp_explain_tool.fill_index_scan(5000000);
vacuum analyze mcp_explain_tool.data_index_scan_norm;
```

| Example | What it demonstrates |
|---|---|
| [Index Scan on a healthy table](pg_index_scan_adapters/sample_index_on_normal.md) | Index Only Scan with `Heap Fetches = 0`, no warnings |
| [Index Scan after heavy churn](pg_index_scan_adapters/sample_index_on_unclastered.md) | Stale visibility map, `index_scan_heap_locality` warning, then recovery after `VACUUM` |

---

## SeqScanCheck: before / after

Reproducible two-part scenario backed by
`mcp_explain_tool.data_seq_scan_norm` and
`mcp_explain_tool.data_seq_scan_nonindex`.

Prerequisites:

```sql
call mcp_explain_tool.fill_seq_scan(1000000);
```

| Example | What it demonstrates |
|---|---|
| [Range query with an index](pg_seq_scan_adapters/sample_seq_scan_on_norm.md) | Index Only Scan, no `seq_scan` warning |
| [Range query without an index](pg_seq_scan_adapters/sample_seq_scan_on_nonindex.md) | Seq Scan reads 1M rows for 100 matches, `seq_scan` fires |

---

## DiskSpillSortCheck: before / after

Reproducible two-part scenario backed by
`mcp_explain_tool.data_disk_spill_sort_norm` and
`mcp_explain_tool.data_disk_spill_sort_spill`.

Prerequisites:

```sql
call mcp_explain_tool.fill_disk_spill_sort(1000000);
```

| Example | What it demonstrates |
|---|---|
| [Small sort stays in memory](pg_disk_spill_sort/sample_disk_spill_sort_on_norm.md) | `quicksort`, no warnings |
| [Large sort spills to disk](pg_disk_spill_sort/sample_disk_spill_sort_on_spill.md) | `external merge`, `disk_spill_sort` fires with spill size |
| [Large sort, precise calc](pg_disk_spill_sort/sample_disk_spill_sort_on_spill_adv.md) | Derived `work_mem` from spill size, arithmetic shown |

---

## DiskSpillHashCheck: before / after

Reproducible two-part scenario backed by
`mcp_explain_tool.data_disk_spill_hash_norm` and
`mcp_explain_tool.data_disk_spill_hash_spill`.

Prerequisites:

```sql
call mcp_explain_tool.fill_disk_spill_hash(1000000);
```

| Example | What it demonstrates |
|---|---|
| [In-memory hash join](pg_disk_spill_hash/sample_disk_spill_hash_on_norm.md) | `hash_batches: 1`, no warnings |
| [Hash join spills to disk](pg_disk_spill_hash/sample_disk_spill_hash_on_spill.md) | `hash_batches: 4`, `disk_spill_hash` fires with estimated full size |
| [Hash join spills, precise calc](pg_disk_spill_hash/sample_disk_spill_hash_on_spill_adv.md) | Derived `work_mem` from `hash_mem_multiplier`, arithmetic shown |

---

## NestedLoopCheck: small / large outer

Reproducible two-part scenario backed by
`mcp_explain_tool.data_nested_loop_norm`,
`mcp_explain_tool.data_nested_loop_many`, and
`mcp_explain_tool.data_nested_loop_inner`.

Prerequisites:

```sql
call mcp_explain_tool.fill_nested_loop(1000000);
```

| Example | What it demonstrates |
|---|---|
| [Nested Loop, 100 outer rows](pg_nested_loop/sample_nested_loop_on_norm.md) | `actual_loops: 100`, no warnings |
| [Nested Loop, 5000 outer rows](pg_nested_loop/sample_nested_loop_on_many.md) | `actual_loops: 5000`, `INFO` note about linear growth |

---

## BitmapHeapScanCheck: narrow / wide range

Reproducible two-part scenario backed by a single table,
`mcp_explain_tool.data_bitmap_heap_scan`. The check fires based on the
selectivity of the query, not the state of the table.

Prerequisites:

```sql
call mcp_explain_tool.fill_bitmap_heap_scan(5000000);
```

| Example | What it demonstrates |
|---|---|
| [Narrow range (25k rows)](pg_bitmap_heap_scan/sample_bitmap_heap_scan_small_range.md) | Below threshold, no `bitmap_heap_scan` warning — but the LLM still notices poor heap clustering |
| [Wide range (200k rows)](pg_bitmap_heap_scan/sample_bitmap_heap_scan_large_range.md) | Above threshold, `bitmap_heap_scan` fires at `INFO` |

---

## EstimateMismatchCheck: five scenarios

Reproducible scenarios backed by
`mcp_explain_tool.data_estimate_mismatch_norm` and
`mcp_explain_tool.data_estimate_mismatch_skewed`.

Prerequisites:

```sql
call mcp_explain_tool.fill_estimate_mismatch(1000000);
```

| Example | What it demonstrates |
|---|---|
| [Norm — no index](pg_estimate_mismatch/sample_estimate_mismatch_on_norm.md) | Honest stats, `seq_scan` fires (99 % discarded) |
| [Norm + index — clean baseline](pg_estimate_mismatch/sample_estimate_mismatch_on_norm_indexed.md) | Honest stats + index → no issues at all |
| [Skewed — fake stats for `val = 42`](pg_estimate_mismatch/sample_estimate_mismatch_on_skewed.md) | Ratio ~94.5x, `estimate_mismatch` and `seq_scan` fire |
| [Skewed — different value `val = 43`](pg_estimate_mismatch/sample_estimate_mismatch_on_skewed_other_value.md) | Ratio ~1.02x, `estimate_mismatch` stays silent |
| [Skewed — leak on `val = 44`](pg_estimate_mismatch/sample_estimate_mismatch_on_skewed_leak.md) | Ratio ~19.6x, but absolute numbers below `min_rows` — check stays silent |

The skewed fixture uses `pg_restore_attribute_stats()` to overwrite
statistics for a single value, and disables autovacuum on the table so
the fake stats survive.

---

## PartitionPruningCheck: pruning works vs. pruning failed

Reproducible two-part scenario backed by
`mcp_explain_tool.data_partition_pruning` — a table partitioned by
month via the `create_monthly_partitions` helper.

Prerequisites:

```sql
call mcp_explain_tool.fill_partition_pruning(1000000);
```

| Example | What it demonstrates |
|---|---|
| [Range predicate — pruning works](pg_partition_pruning/sample_partition_pruning_norm.md) | `Append` over 3 partitions, no warnings |
| [Non-sargable predicate — pruning fails](pg_partition_pruning/sample_partition_pruning_trigger.md) | `Append` over 12 partitions, `partition_pruning` fires |


---

## NonSargableCheck: sargable vs. non-sargable predicate

Reproducible two-part scenario backed by
`mcp_explain_tool.data_non_sargable` — a table with a plain index on
`email`.

Prerequisites:

```sql
call mcp_explain_tool.fill_non_sargable(5000000);
```

| Example | What it demonstrates |
|---|---|
| [Sargable predicate](pg_non_sargable/sample_non_sargable_norm.md) | `Index Scan`, no warnings |
| [Non-sargable predicate](pg_non_sargable/sample_non_sargable_trigger.md) | `lower(email) = '...'`, `non_sargable` + `seq_scan` |
| [After the fix](pg_non_sargable/sample_non_sargable_fixed.md) | functional index on `lower(email)`, back to `Index Scan` |

