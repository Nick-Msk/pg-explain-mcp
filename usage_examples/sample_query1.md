# Sample Query 1: Ordered-Set Aggregates over a Large Table

## Context

A query computing multiple aggregates — including ordered-set functions
like `PERCENTILE_CONT` — over a 5-million-row table without a `WHERE` clause.

## Input Query

```sql
SELECT
    AVG(n)                                        AS mean,
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY n) AS q1,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY n) AS median,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY n) AS q3,
    MIN(n)                                        AS min_val,
    MAX(n)                                        AS max_val
FROM big;
```

## Prompt to the Assistant

> Use the pg-explain tool to analyze the query above.

## Raw Output from pg-explain

```json
{
  "execution_time_ms": 6090.0,
  "planning_time_ms": 5.43,
  "total_time_ms": 6095.43,
  "issue_count": 1,
  "issues": [
    {
      "severity": "warning",
      "type": "seq_scan",
      "message": "Sequential scan on 'big' processed 5000000 rows. Consider adding an index.",
      "node": "Seq Scan"
    }
  ],
  "summary": "Found 1 issues (1 critical). Execution time: 6090.00 ms."
}
```

## Analysis Summary

### 1. General Statistics

| Metric                | Value         |
|-----------------------|---------------|
| Execution Time        | ~6.09 s       |
| Planning Time         | ~5.43 ms      |
| Total Rows Processed  | 5,000,000     |

### 2. Primary Bottleneck

**Sequential Scan (`Seq Scan`) on the `big` table.**

The database performs a full sequential scan of the `big` table to extract
values from column `n`. For a table containing 5 million rows, this results
in massive I/O overhead — the engine must read the entire table from disk.

### 3. Query Complexity Analysis

The query combines two types of aggregate operations:

- **Simple aggregates** (`AVG`, `MIN`, `MAX`) — computationally cheap,
  but still require a full pass through the data.
- **Ordered-set aggregates** (`PERCENTILE_CONT`) — highly resource-intensive.
  To compute quantiles (Q1, Median, Q3), the engine must **sort all
  5 million values** of column `n`. Sorting such a large dataset during
  a sequential scan is extremely CPU- and memory-intensive.

Since there is no `WHERE` clause to filter rows, the planner defaults to
a sequential scan — the most expensive way to access the data.

### 4. Optimization Recommendations

The goal is to transition from a **Seq Scan** to an **Index-Only Scan**.

```sql
CREATE INDEX idx_big_n ON big(n);
```

**Why this helps:**

- **Index-Only Scan** — instead of reading the entire table (which may
  contain many other columns), PostgreSQL retrieves all required values
  of column `n` directly from the compact B-tree index. This drastically
  reduces disk I/O.
- **Elimination of sorting** — B-tree indexes store data in pre-sorted
  order. By using the index, the database satisfies `PERCENTILE_CONT`
  simply by traversing the index, eliminating the expensive sorting phase.

**Expected outcome:** execution time drops from seconds to milliseconds,
significantly reducing CPU and disk I/O load.

