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

create table data_index_scan_norm (
    id   bigserial primary key,
    val  text      not null,
    num  integer   not null,
    pad  text
);

create index idx_data_index_scan_norm_val on data_index_scan_norm (val);

create table data_index_scan_unclastered (
    id   bigserial primary key,
    val  text      not null,
    num  integer   not null,
    pad  text
);

alter table data_index_scan_unclastered set (autovacuum_enabled = false);

create index idx_data_index_scan_unclastered_val on data_index_scan_unclastered (val);

create procedure fill_index_scan(totalcount int)
language plpgsql
set search_path = mcp_explain_tool, pg_catalog
as $$
declare
    i integer;
begin
    insert into data_index_scan_norm (val, num, pad)
    select md5(j::text), (random() * 1_000_000)::int, repeat('x', 200)
    from generate_series(1, totalcount) as j;

    insert into data_index_scan_unclastered (val, num, pad)
    select md5(j::text), (random() * 1_000_000)::int, repeat('x', 200)
    from generate_series(1, totalcount) as j;

    for i in 1..20 loop
        update data_index_scan_unclastered
        set pad = repeat('y', 200)
        where id % 5 = 0;

        delete from data_index_scan_unclastered
        where id % 7 = 0 and id > totalcount / 10;

        insert into data_index_scan_unclastered (val, num, pad)
        select md5((totalcount * 2 + i * 100_000 + j)::text),
               (random() * 1_000_000)::int,
               repeat('z', 200)
        from generate_series(1, 50_000) as j;
    end loop;

    analyze data_index_scan_norm;
    analyze data_index_scan_unclastered;
end;
$$;

create procedure clear_index_scan()
language plpgsql
set search_path = mcp_explain_tool, pg_catalog
as $$
begin
    truncate data_index_scan_norm, data_index_scan_unclastered restart identity;
end;
$$;


-- -----------------------------------------------------------------------------
--  SeqScanCheck
-- -----------------------------------------------------------------------------

create table data_seq_scan_norm (
    id   bigserial primary key,
    val  integer   not null,
    pad  text
);

create index idx_data_seq_scan_norm_val on data_seq_scan_norm (val);

create table data_seq_scan_nonindex (
    id   bigserial primary key,
    val  integer   not null,
    pad  text
);

-- no index on val — the planner is forced into a seq scan.

create procedure fill_seq_scan(totalcount int)
language plpgsql
set search_path = mcp_explain_tool, pg_catalog
as $$
begin
    insert into data_seq_scan_norm (val, pad)
    select (random() * 1_000_000)::int, repeat('x', 200)
    from generate_series(1, totalcount) as i;

    insert into data_seq_scan_nonindex (val, pad)
    select (random() * 1_000_000)::int, repeat('x', 200)
    from generate_series(1, totalcount) as i;

    analyze data_seq_scan_norm;
    analyze data_seq_scan_nonindex;
end;
$$;

create procedure clear_seq_scan()
language plpgsql
set search_path = mcp_explain_tool, pg_catalog
as $$
begin
    truncate data_seq_scan_norm, data_seq_scan_nonindex restart identity;
end;
$$;

-- -----------------------------------------------------------------------------
--  disk_spill_sort
-- -----------------------------------------------------------------------------

create table data_disk_spill_sort_norm (
    id   bigserial primary key,
    val  integer   not null,
    pad  text
);

create table data_disk_spill_sort_spill (
    id   bigserial primary key,
    val  integer   not null,
    pad  text
);

create procedure fill_disk_spill_sort(totalcount int)
language plpgsql
set search_path = mcp_explain_tool, pg_catalog
as $$
begin
    -- norm: ~10k rows × 60 bytes ≈ 600 kB — fits in the default 4 MB work_mem
    insert into data_disk_spill_sort_norm (val, pad)
    select (random() * 1_000_000)::int, repeat('x', 50)
    from generate_series(1, 10000) as n;

    -- spill: 1M rows × 200 bytes ≈ 200 MB — spills to disk
    insert into data_disk_spill_sort_spill (val, pad)
    select (random() * 1_000_000)::int, repeat('x', 200)
    from generate_series(1, totalcount) as n;

    analyze data_disk_spill_sort_norm;
    analyze data_disk_spill_sort_spill;
end;
$$;

create procedure clear_disk_spill_sort()
language plpgsql
set search_path = mcp_explain_tool, pg_catalog
as $$
begin
    truncate data_disk_spill_sort_norm, data_disk_spill_sort_spill restart identity;
end;
$$;

-- -----------------------------------------------------------------------------
--  Aggregates (grow as adapters are added)
-- -----------------------------------------------------------------------------

-- -----------------------------------------------------------------------------
--  aggregates
-- -----------------------------------------------------------------------------

create procedure fill_all(totalcount int)
language plpgsql
set search_path = mcp_explain_tool, pg_catalog
as $$
begin
    call fill_seq_scan(totalcount);
    call fill_index_scan(totalcount);
    call fill_disk_spill_sort(totalcount);
end;
$$;

create procedure clear_all()
language plpgsql
set search_path = mcp_explain_tool, pg_catalog
as $$
begin
    call clear_seq_scan();
    call clear_index_scan();
    call clear_disk_spill_sort();
end;
$$;
