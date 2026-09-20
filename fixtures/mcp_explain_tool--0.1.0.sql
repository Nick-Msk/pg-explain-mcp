-- =============================================================================
--  pg_explain_tool -- test fixtures for pg-explain-mcp PlanCheck adapters
-- =============================================================================
--
--  Creates empty tables + fill_* / clear_* procedures for each PlanCheck.
--  Data is loaded on demand:
--
--      CALL pg_explain_tool.fill_all(5000000);
--      CALL pg_explain_tool.fill_index_scan(5000000);
--
--  VACUUM is not run here (cannot run inside an extension transaction).
--  See fixtures/README.md for the manual VACUUM step.
-- =============================================================================

-- -----------------------------------------------------------------------------
--  IndexScanCheck
-- -----------------------------------------------------------------------------

CREATE TABLE data_index_scan_norm (
    id   bigserial PRIMARY KEY,
    val  text      NOT NULL,
    num  integer   NOT NULL,
    pad  text
);

CREATE INDEX idx_data_index_scan_norm_val
    ON data_index_scan_norm (val);

CREATE TABLE data_index_scan_unclastered (
    id   bigserial PRIMARY KEY,
    val  text      NOT NULL,
    num  integer   NOT NULL,
    pad  text
);

ALTER TABLE data_index_scan_unclastered SET (autovacuum_enabled = false);

CREATE INDEX idx_data_index_scan_unclastered_val
    ON data_index_scan_unclastered (val);

CREATE PROCEDURE fill_index_scan(totalcount int)
LANGUAGE plpgsql
AS $$
DECLARE
    i integer;
BEGIN
    INSERT INTO data_index_scan_norm (val, num, pad)
    SELECT md5(i::text), (random() * 1_000_000)::int, repeat('x', 200)
    FROM generate_series(1, totalcount) AS i;

    INSERT INTO data_index_scan_unclastered (val, num, pad)
    SELECT md5(i::text), (random() * 1_000_000)::int, repeat('x', 200)
    FROM generate_series(1, totalcount) AS i;

    FOR i IN 1..20 LOOP
        UPDATE data_index_scan_unclastered
        SET pad = repeat('y', 200)
        WHERE id % 5 = 0;

        DELETE FROM data_index_scan_unclastered
        WHERE id % 7 = 0 AND id > totalcount / 10;

        INSERT INTO data_index_scan_unclastered (val, num, pad)
        SELECT md5((totalcount * 2 + i * 100_000 + j)::text),
               (random() * 1_000_000)::int,
               repeat('z', 200)
        FROM generate_series(1, 50_000) AS j;
    END LOOP;

    ANALYZE data_index_scan_norm;
    ANALYZE data_index_scan_unclastered;
END;
$$;

CREATE PROCEDURE clear_index_scan()
LANGUAGE plpgsql
AS $$
BEGIN
    TRUNCATE data_index_scan_norm, data_index_scan_unclastered
        RESTART IDENTITY;
END;
$$;

-- -----------------------------------------------------------------------------
--  Aggregates (grow as adapters are added)
-- -----------------------------------------------------------------------------

CREATE PROCEDURE fill_all(totalcount int)
LANGUAGE plpgsql
AS $$
BEGIN
    CALL fill_index_scan(totalcount);
END;
$$;

CREATE PROCEDURE clear_all()
LANGUAGE plpgsql
AS $$
BEGIN
    CALL clear_index_scan();
END;
$$;

