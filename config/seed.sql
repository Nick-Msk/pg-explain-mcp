-- Default registry. Loaded by `--init`. Re-running `--init` rebuilds
-- the database from scratch.

-- Parents first — every other table references this.

insert into databases (database) values
    ('postgres');

insert into checks (num, database, name, description, enabled) values
    (1, 'postgres', 'SeqScanCheck',          'sequential scan that discards most rows',        1),
    (2, 'postgres', 'EstimateMismatchCheck', 'planner cardinality misestimate',                1),
    (3, 'postgres', 'DiskSpillSortCheck',    'sort spilling to disk',                          1),
    (4, 'postgres', 'DiskSpillHashCheck',    'hash operation using multiple batches',          1),
    (5, 'postgres', 'NestedLoopCheck',       'Nested Loop with many inner iterations',         1),
    (6, 'postgres', 'BitmapHeapScanCheck',   'large Bitmap Heap Scan',                         1),
    (7, 'postgres', 'IndexScanCheck',        'stale visibility map / poor heap locality',      1),
    (8, 'postgres', 'PartitionPruningCheck', 'Append over many partitions — pruning may have failed', 1),
    (9, 'postgres', 'NonSargableCheck', 'non-sargable predicate on an indexed column',         1);

insert into check_params (num, database, param, value, default_value) values
    (1, 'postgres', 'threshold_rows',    '1000',   '1000'),
    (1, 'postgres', 'min_filter_ratio',  '0.9',    '0.9'),
    (2, 'postgres', 'threshold_ratio',   '10.0',   '10.0'),
    (2, 'postgres', 'min_rows',          '1000',   '1000'),
    (3, 'postgres', 'min_spill_kb',      '0',      '0'),
    (4, 'postgres', 'min_batches',       '2',      '2'),
    (5, 'postgres', 'threshold_loops',   '1000',   '1000'),
    (5, 'postgres', 'threshold_rows',    '100000', '100000'),
    (6, 'postgres', 'threshold_rows',    '100000', '100000'),
    (7, 'postgres', 'min_rows',          '1000',   '1000'),
    (7, 'postgres', 'heap_fetch_ratio',  '0.10',   '0.10'),
    (7, 'postgres', 'min_disk_blocks',   '100',    '100'),
    (8, 'postgres', 'max_children',      '3',      '3'),
    (9, 'postgres', 'threshold_rows',    '1000',   '1000');

insert into plan_fields (database, raw, key, enabled) values
    ('postgres', 'Relation Name',          'relation',               1),
    ('postgres', 'Index Name',             'index',                  1),
    ('postgres', 'Actual Rows',            'actual_rows',            1),
    ('postgres', 'Actual Loops',           'actual_loops',           1),
    ('postgres', 'Plan Rows',              'plan_rows',              1),
    ('postgres', 'Rows Removed by Filter', 'rows_removed_by_filter', 1),
    ('postgres', 'Heap Fetches',           'heap_fetches',           1),
    ('postgres', 'Shared Read Blocks',     'shared_read_blocks',     1),
    ('postgres', 'Sort Method',            'sort_method',            1),
    ('postgres', 'Sort Space Type',        'sort_space_type',        1),
    ('postgres', 'Sort Space Used',        'sort_space_used_kb',     1),
    ('postgres', 'Hash Buckets',           'hash_buckets',           1),
    ('postgres', 'Hash Batches',           'hash_batches',           1),
    ('postgres', 'Peak Memory Usage',      'peak_memory_usage_kb',   1),
    ('postgres', 'Disk Usage',             'disk_usage_kb',          1),
    ('postgres', 'Parallel Aware',         'parallel_aware',         1);

