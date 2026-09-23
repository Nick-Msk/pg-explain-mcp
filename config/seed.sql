-- Default registry. Loaded by `--init`. Re-running `--init` rebuilds from scratch.

insert into checks (num, database, name, description, enabled) values
    (1, 'postgres', 'SeqScanCheck',          'sequential scan that discards most rows',        1),
    (2, 'postgres', 'EstimateMismatchCheck', 'planner cardinality misestimate',                1),
    (3, 'postgres', 'DiskSpillSortCheck',    'sort spilling to disk',                          1),
    (4, 'postgres', 'DiskSpillHashCheck',    'hash operation using multiple batches',          1),
    (5, 'postgres', 'NestedLoopCheck',       'Nested Loop with many inner iterations',         1),
    (6, 'postgres', 'BitmapHeapScanCheck',   'large Bitmap Heap Scan',                         1),
    (7, 'postgres', 'IndexScanCheck',        'stale visibility map / poor heap locality',      1);

insert into check_params (num, database, param, value) values
    (1, 'postgres', 'threshold_rows',    '1000'),
    (1, 'postgres', 'min_filter_ratio',  '0.9'),
    (2, 'postgres', 'threshold_ratio',   '10.0'),
    (2, 'postgres', 'min_rows',          '1000'),
    (3, 'postgres', 'min_spill_kb',      '0'),
    (4, 'postgres', 'min_batches',       '2'),
    (5, 'postgres', 'threshold_loops',   '1000'),
    (5, 'postgres', 'threshold_rows',    '100000'),
    (6, 'postgres', 'threshold_rows',    '100000'),
    (7, 'postgres', 'min_rows',          '1000'),
    (7, 'postgres', 'heap_fetch_ratio',  '0.10'),
    (7, 'postgres', 'min_disk_blocks',   '100');

