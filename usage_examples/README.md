# usage examples

Real-world runs of `pg-explain-mcp` against PostgreSQL.

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

