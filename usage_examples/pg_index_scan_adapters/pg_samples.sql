-- =============================================================================
--  pg-explain-mcp — test scenario for IndexScanCheck
-- =============================================================================
--
--  This script reproduces the scenario used in:
--
--    * sample_index_on_normal.md       — clean table, Index Only Scan
--    * sample_index_on_unclastered.md  — after churn, stale visibility map
--
--  Steps:
--    1. Create the table and index, load 5M rows.
--    2. VACUUM ANALYZE to make the visibility map fresh.
--    3. Run the baseline query → expect Index Only Scan with Heap Fetches = 0.
--    4. Apply churn (UPDATE / DELETE / INSERT) without VACUUM.
--    5. Run the same query → expect stale visibility map.
--
--  Tested on PostgreSQL 18.
-- =============================================================================

\c test1

-- -----------------------------------------------------------------------------
-- 1. Create schema
-- -----------------------------------------------------------------------------

DROP TABLE IF EXISTS big_unclastered;

CREATE TABLE big_unclastered (
    id   bigserial PRIMARY KEY,
    val  text      NOT NULL,
    num  integer   NOT NULL,
    pad  text
);

-- Disable autovacuum so the visibility map stays stale after churn.
ALTER TABLE big_unclastered SET (autovacuum_enabled = false);

-- -----------------------------------------------------------------------------
-- 2. Load 5 million rows
-- -----------------------------------------------------------------------------

INSERT INTO big_unclastered (val, num, pad)
SELECT
    md5(i::text),
    (random() * 1_000_000)::int,
    repeat('x', 200)
FROM generate_series(1, 5_000_000) AS i;

CREATE INDEX idx_big_unclastered_val ON big_unclastered (val);

-- Fresh visibility map + statistics.
VACUUM ANALYZE big_unclastered;

-- -----------------------------------------------------------------------------
-- 3. Baseline query — "normal" state
-- -----------------------------------------------------------------------------

-- The plan should show:
--     Index Only Scan using idx_big_unclastered_val
--     Heap Fetches: 0
--
-- Run in psql to verify:
--
--     EXPLAIN (ANALYZE, BUFFERS)
--     SELECT val
--     FROM big_unclastered
--     WHERE val BETWEEN '000' AND '001'
--     LIMIT 10000;
--
-- Then ask pg-explain-mcp to analyze the same query.
-- Expected: issue_count = 0.
-- See sample_index_on_normal.md for the full output.

-- -----------------------------------------------------------------------------
-- 4. Churn — break the visibility map
-- -----------------------------------------------------------------------------

DO $$
DECLARE
    i integer;
BEGIN
    FOR i IN 1..20 LOOP
        UPDATE big_unclastered
        SET pad = repeat('y', 200)
        WHERE id % 5 = 0;

        DELETE FROM big_unclastered
        WHERE id % 7 = 0
          AND id > 500_000;

        INSERT INTO big_unclastered (val, num, pad)
        SELECT
            md5((10_000_000 + i * 100_000 + j)::text),
            (random() * 1_000_000)::int,
            repeat('z', 200)
        FROM generate_series(1, 50_000) AS j;

        RAISE NOTICE 'Iteration % done', i;
    END LOOP;
END $$;

-- Verify the damage:
--
--     SELECT n_live_tup, n_dead_tup,
--            ROUND(n_dead_tup::numeric / NULLIF(n_live_tup, 0) * 100, 2) AS dead_pct
--     FROM pg_stat_user_tables
--     WHERE relname = 'big_unclastered';
--
-- Expected: dead_pct > 20% (in our run it reached 519%).

-- -----------------------------------------------------------------------------
-- 5. Same query, now "unclustered" state
-- -----------------------------------------------------------------------------

-- The plan should show:
--     Index Only Scan using idx_big_unclastered_val
--     Heap Fetches: > actual rows
--
-- Run in psql to verify:
--
--     EXPLAIN (ANALYZE, BUFFERS)
--     SELECT val
--     FROM big_unclastered
--     WHERE val BETWEEN '000' AND '001'
--     LIMIT 10000;
--
-- Then ask pg-explain-mcp to analyze the same query.
-- Expected: issue_count = 1, type = index_scan_heap_locality.
-- See sample_index_on_unclastered.md for the full output.

-- -----------------------------------------------------------------------------
-- 6. Cleanup (optional)
-- -----------------------------------------------------------------------------

-- To restore the table to a healthy state:
--
--     VACUUM (ANALYZE) big_unclastered;
--
-- After VACUUM, re-run the query — Heap Fetches should return to 0 and
-- pg-explain-mcp should report issue_count = 0 again.

