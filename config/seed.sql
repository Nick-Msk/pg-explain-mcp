-- Default registry. Loaded by `--init`. Re-running `--init` rebuilds
-- the database from scratch.

-- Parents first — every other table references this.

insert into databases (database) values
    ('postgres');

insert into tags (name) values
    ('GENERAL'),
    ('SPILL'),
    ('INDEX'),
    ('SORT'),
    ('JOIN'),
    ('ESTIMATE'),
    ('SCAN'),
    ('HASH');

insert into checks (num, database, name, description, enabled) values
    (1,  'postgres', 'SeqScanCheck',          'sequential scan that discards most rows',                0),
    (2,  'postgres', 'EstimateMismatchCheck', 'planner cardinality misestimate',                        0),
    (3,  'postgres', 'DiskSpillSortCheck',    'sort spilling to disk',                                  0),
    (4,  'postgres', 'DiskSpillHashCheck',    'hash operation using multiple batches',                  0),
    (5,  'postgres', 'NestedLoopCheck',       'Nested Loop with many inner iterations',                 0),
    (6,  'postgres', 'BitmapHeapScanCheck',   'large Bitmap Heap Scan',                                 0),
    (7,  'postgres', 'IndexRegularScanCheck', 'index scan reading too many blocks for rows returned',   0),
    (8,  'postgres', 'IndexOnlyScanCheck',    'index only scan with stale visibility map',              0),
    (9,  'postgres', 'PartitionPruningCheck', 'Append over many partitions — pruning may have failed',  0),
    (10, 'postgres', 'NonSargableCheck',      'non-sargable predicate on an indexed column',            1);

insert into checks_tags (num, database, tag) values
    -- Scans
    (1,  'postgres', 'SCAN'),        -- SeqScanCheck
    (6,  'postgres', 'SCAN'),        -- BitmapHeapScanCheck

    -- Sort / hash spills
    (3,  'postgres', 'SPILL'),
    (3,  'postgres', 'SORT'),
    (4,  'postgres', 'SPILL'),
    (4,  'postgres', 'HASH'),

    -- Joins
    (5,  'postgres', 'JOIN'),        -- NestedLoopCheck

    -- Index usability
    (7,  'postgres', 'INDEX'),       -- IndexRegularScanCheck
    (8,  'postgres', 'INDEX'),       -- IndexOnlyScanCheck
    (10, 'postgres', 'INDEX'),       -- NonSargableCheck

    -- Planner estimation
    (2,  'postgres', 'ESTIMATE'),    -- EstimateMismatchCheck

    -- Partitioning
    (9,  'postgres', 'PARTITION');   -- PartitionPruningCheck

insert into check_params (num, database, param, value, default_value) values
    (1, 'postgres', 'threshold_rows',    '1000',   '1000'),
    (1, 'postgres', 'min_filter_ratio',  '0.9',    '0.9'),
    (2, 'postgres', 'threshold_ratio',   '10.0',   '10.0'),
    (2, 'postgres', 'min_rows',          '1000',   '1000'),
    (3, 'postgres', 'min_spill_kb',      '0',      '0'),
    (4, 'postgres', 'min_batches',       '2',      '2'),
    (5, 'postgres', 'threshold_loops',   '1000',   '1000'),
    -- (5, 'postgres', 'threshold_rows',    '100000', '100000'),
    (6, 'postgres', 'threshold_rows',    '100000', '100000'),
    (7, 'postgres', 'min_rows',          '1000',   '1000'),
    (7, 'postgres', 'min_disk_blocks',   '100',    '100'),
    --(8, 'postgres', 'min_rows',          '1000',   '1000'),
    (8, 'postgres', 'min_rows',          '100',    '100'),
    (8, 'postgres', 'heap_fetch_ratio',  '0.10',   '0.10'),
    (9, 'postgres', 'max_children',      '3',      '3'),
    (10,'postgres', 'threshold_rows',    '1000',   '1000');

insert into plan_fields (database, raw, key, enabled) values
    -- Noise — hide entirely (false = no signal)
    ('postgres', 'Parallel Aware',          'parallel_aware',         0),
    ('postgres', 'Async Capable',           'async_capable',          0),
    ('postgres', 'Disabled',                'disabled',               0),

    -- Meaningful zeros — keep 0 (absence of problem is the signal)
    ('postgres', 'Heap Fetches',            'heap_fetches',           1),
    ('postgres', 'Rows Removed by Filter',  'rows_removed_by_filter', 1),
    ('postgres', 'Shared Read Blocks',      'shared_read_blocks',     1),
    ('postgres', 'Temp Read Blocks',        'temp_read_blocks',       1),
    ('postgres', 'Temp Written Blocks',     'temp_written_blocks',    1);

