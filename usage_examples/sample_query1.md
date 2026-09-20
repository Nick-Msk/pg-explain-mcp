# Query Execution Plan Analysis Report

## Input Query
The following SQL query was analyzed to evaluate its performance on the `big` table:

```sql
SELECT 
    AVG(n) AS mean,
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY n) AS q1,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY n) AS median,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY n) AS q3,
    MIN(n) AS min_val,
    MAX(n) AS max_val
FROM big;
```

---

## Analysis Summary

### 1. General Statistics
* **Execution Time:** ~6.09 s
* **Planning Time:** ~5.43 ms
* **Total Rows Processed:** 5,000,000

### 2. Primary Bottleneck
The analysis identified a critical performance issue: **Sequential Scan (Seq Scan)** on the `big` table.

**Details:**
The database is performing a full sequential scan of the `big` table to extract values from column `n`. For a table containing 5 million rows, this results in massive **I/O (Input/Output) overhead**, as the engine must read the entire table from the disk to find the necessary data.

### 3. Query Complexity Analysis
The query complexity arises from the combination of two different types of aggregate operations:

1.  **Simple Aggregates (`AVG`, `MIN`, `MAX`):** These are computationally inexpensive but still require a full pass through the data.
2.  **Ordered-Set Aggregates (`PERCENTILE_CONT`):** These functions are highly resource-intensive. To calculate quantiles (Q1, Median, Q3), the database engine must **sort** all 5 million values of column `n`. Sorting such a large dataset during a sequential scan is extremely CPU and memory intensive.

Since there is no `WHERE` clause to filter the rows, the database defaults to a sequential scan, which is the most expensive way to access the data.

### 4. Optimization Recommendations

To significantly improve performance, the goal is to transition from a `Seq Scan` to an **Index-Only Scan**.

**Recommended Solution:**
Create a B-tree index on the column being aggregated:

```sql
CREATE INDEX idx_big_n ON big(n);

Why this optimizes the query:

Index-Only Scan: Instead of reading the entire table (which may contain many other columns), PostgreSQL can retrieve all required values for column n directly from the compact B-tree index. This drastically reduces disk I/O.
Elimination of Sorting: B-tree indexes store data in a pre-sorted order. By using an index, the database can satisfy the PERCENTILE_CONT requirement by simply traversing the index, effectively eliminating the expensive sorting phase.
Expected Outcome: After applying the index, the execution time is expected to drop from seconds to milliseconds, significantly reducing the load on both CPU and Disk I/O.

