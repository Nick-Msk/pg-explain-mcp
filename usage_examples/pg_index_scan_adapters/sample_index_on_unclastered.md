# Sample: Index Scan After Heavy Churn

> Scenario 2 of 2 for `IndexScanCheck`.
> Prerequisites: run `pg_samples.sql` through **step 5** (after churn).

## Context

The `big_unclastered` table was subjected to 20 iterations of
`UPDATE` / `DELETE` / `INSERT` churn **without `VACUUM`**. Autovacuum is
disabled on the table, so the visibility map is now stale.

After the churn, `pg_stat_user_tables` reports:

| Metric       | Value     |
|--------------|-----------|
| `n_live_tup` | 4,792,873 |
| `n_dead_tup` | 24,875,303 |
| `dead_pct`   | **519%**  |

An Index Only Scan can no longer trust the index — every row requires
a heap visit to confirm visibility.

## Input Query

```sql
SELECT val
FROM big_unclastered
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
      "message": "Index Only Scan on 'idx_big_unclastered_val' (big_unclastered) performed 2975 heap fetches for 1172.0 rows (253.8%). The visibility map is stale. Run VACUUM, or consider CLUSTER to improve locality.",
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
      "relation": "big_unclastered",
      "index": "idx_big_unclastered_val",
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

The plan uses an **Index Only Scan** on `idx_big_unclastered_val`, which
is normally the fastest access path for this query. But the plan reports
**2,975 heap fetches for 1,172 rows** — the index alone is no longer
sufficient.

An Index Only Scan can skip the heap *only* if the **visibility map**
marks the corresponding pages as all-visible. After heavy `UPDATE` /
`DELETE` activity without `VACUUM`, those flags are stale, and the engine
must visit the heap for each tuple. This turns what should be a pure
index traversal into a series of random heap reads.

### Recommendation

```sql
VACUUM (ANALYZE) big_unclastered;
```

- `VACUUM` rebuilds the visibility map — future Index Only Scans can
  skip the heap entirely.
- `ANALYZE` refreshes statistics — the planner estimated 1,484 rows
  but the actual count was 1,172.

For permanently clustered data:

```sql
CLUSTER big_unclastered USING idx_big_unclastered_val;
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
4. **Verify** — re-run after the fix and confirm the warning is gone.

