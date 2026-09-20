# Sample: Index Scan After Heavy Churn

> Scenario 2 of 2 for `IndexScanCheck`.

## Prerequisites

```sql
CREATE EXTENSION mcp_explain_tool;
CALL mcp_explain_tool.fill_index_scan(5000000);
```

Note: `VACUUM` is intentionally **not** run on
`data_index_scan_unclastered`. Autovacuum is disabled for this table
(set inside the extension), so the visibility map stays stale.

## Context

`data_index_scan_unclastered` was loaded with 5M rows, then subjected to
20 iterations of `UPDATE` / `DELETE` / `INSERT` churn. The visibility map
is now stale, and an Index Only Scan can no longer trust the index — every
row requires a heap visit to confirm visibility.

Verify the damage:

```sql
SELECT n_live_tup, n_dead_tup,
       ROUND(n_dead_tup::numeric / NULLIF(n_live_tup, 0) * 100, 2) AS dead_pct
FROM pg_stat_user_tables
WHERE relname = 'data_index_scan_unclastered';
```

Expected: `dead_pct > 20%` (in our run it reached 519%).

## Input Query

```sql
SELECT val
FROM mcp_explain_tool.data_index_scan_unclastered
WHERE val BETWEEN '000' AND '001'
LIMIT 10000;
```

## Prompt to the Assistant

> Analyze the plan for the query above.
> Also show me the raw JSON output you received.

## Raw Output from pg-explain

```json
{
  "execution_time_ms": 11.167,
  "planning_time_ms": 6.336,
  "total_time_ms": 17.503,
  "issues": [
    {
      "severity": "warning",
      "type": "index_scan_heap_locality",
      "message": "Index Only Scan on 'idx_data_index_scan_unclastered_val' (data_index_scan_unclastered) performed 2975 heap fetches for 1172.0 rows (253.8%). The visibility map is stale. Run VACUUM, or consider CLUSTER to improve locality.",
      "node": "Index Only Scan"
    }
  ],
  "issue_count": 1,
  "summary": "Found 1 issues (1 critical). Execution time: 11.17 ms.",
  "plan_nodes": [
    {
      "depth": 0,
      "node_type": "Limit",
      "actual_rows": 1172.0,
      "plan_rows": 1484,
      "shared_read_blocks": 0
    },
    {
      "depth": 1,
      "node_type": "Index Only Scan",
      "relation": "data_index_scan_unclastered",
      "index": "idx_data_index_scan_unclastered_val",
      "actual_rows": 1172.0,
      "plan_rows": 1484,
      "heap_fetches": 2975,
      "shared_read_blocks": 0
    }
  ]
}
```

## Analysis

| Metric          | Value     |
|-----------------|-----------|
| Execution Time  | 11.17 ms  |
| Rows Returned   | 1,172     |
| Heap Fetches    | **2,975** |
| Heap Fetch Rate | 253.8%    |
| Issues          | 1         |

### Primary Issue: Stale Visibility Map

The plan uses an **Index Only Scan** on
`idx_data_index_scan_unclastered_val`, which is normally the fastest
access path for this query. But the plan reports **2,975 heap fetches
for 1,172 rows** — the index alone is no longer sufficient.

An Index Only Scan can skip the heap *only* if the **visibility map**
marks the corresponding pages as all-visible. After heavy `UPDATE` /
`DELETE` activity without `VACUUM`, those flags are stale, and the engine
must visit the heap for each tuple. This turns what should be a pure
index traversal into a series of random heap reads.

### Recommendation

```sql
VACUUM (ANALYZE) mcp_explain_tool.data_index_scan_unclastered;
```

- `VACUUM` rebuilds the visibility map — future Index Only Scans can
  skip the heap entirely.
- `ANALYZE` refreshes statistics — the planner estimated 1,484 rows
  but the actual count was 1,172.

For permanently clustered data:

```sql
CLUSTER mcp_explain_tool.data_index_scan_unclastered
  USING idx_data_index_scan_unclastered_val;
```

This physically reorders the heap to match the index, at the cost of an
exclusive lock.

## After VACUUM

Re-running the same query after `VACUUM ANALYZE`:

| Metric                     | Before  | After |
|----------------------------|---------|-------|
| Heap Fetches               | 2,975   | 0     |
| `issue_count`              | 1       | 0     |
| `index_scan_heap_locality` | warning | —     |

The analyzer reports **no issues** once the visibility map is refreshed —
confirming that the recommendation is effective.

## What This Proves

`IndexScanCheck` catches a class of problems that plain `EXPLAIN ANALYZE`
output does not surface by itself:

1. **Detect** — recognize a stale visibility map from `Heap Fetches`.
2. **Explain** — expose `heap_fetches` in the plan tree so the LLM can
   reason about the actual cause.
3. **Recommend** — target the root cause (`VACUUM`), not a symptom
   (adding another index).
4. **Verify** — re-run after the fix and confirm the warning is gone
.
