## IndexScanCheck: before / after

Reproducible two-part scenario, backed by the
[`mcp_explain_tool`](../fixtures/) PostgreSQL extension:

| Example | What it demonstrates |
|---|---|
| [Index Scan on a healthy table](pg_index_scan_adapters/sample_index_on_normal.md) | Index Only Scan with `Heap Fetches = 0`, no warnings |
| [Index Scan after heavy churn](pg_index_scan_adapters/sample_index_on_unclastered.md) | Stale visibility map, `index_scan_heap_locality` warning, then recovery after `VACUUM` |

Prerequisites:

```sql
CREATE EXTENSION mcp_explain_tool;
CALL mcp_explain_tool.fill_index_scan(5000000);
VACUUM ANALYZE mcp_explain_tool.data_index_scan_norm;

