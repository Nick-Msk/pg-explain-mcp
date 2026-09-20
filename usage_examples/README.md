# Usage Examples

Real-world runs of `pg-explain-mcp` against PostgreSQL.

## General samples

| Example | What it demonstrates |
|---|---|
| [Sample 1 — PERCENTILE_CONT](sample_query1.md) | Ordered-set aggregates over a 5M-row table (`SeqScanCheck`) |
| Example | What it demonstrates |
|---|---|
| [Sample 1 — PERCENTILE_CONT](sample_query1.md) | Ordered-set aggregates over a 5M-row table (`SeqScanCheck`) |
| [`list_indexes` usage](list_indexes.md) | Check existing indexes before recommending a new one |

## IndexScanCheck: before / after

Reproducible two-part scenario, backed by [`pg_index_scan_adapters/pg_samples.sql`](pg_index_scan_adapters/pg_samples.sql):

| Example | What it demonstrates |
|---|---|
| [Index Scan on a healthy table](pg_index_scan_adapters/sample_index_on_normal.md) | Index Only Scan with `Heap Fetches = 0`, no warnings |
| [Index Scan after heavy churn](pg_index_scan_adapters/sample_index_on_unclastered.md) | Stale visibility map, `index_scan_heap_locality` warning, then recovery after `VACUUM` |

