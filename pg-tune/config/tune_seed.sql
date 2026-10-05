insert into tune_allows
    (action, enabled, default_enabled, description) values
    ('backup',                  1, 1, 'Snapshot the target database with pg_dump | zstd'),
    ('restore',                 0, 0, 'Drop and restore from a tune_backups entry (DESTRUCTIVE)'),
    ('set_session_guc',         0, 0, 'SET LOCAL in a rolled-back transaction'),
    ('analyze',                 0, 0, 'ANALYZE on a table'),
    ('create_index',            0, 0, 'CREATE INDEX (blocking)'),
    ('create_index_concurrent', 0, 0, 'CREATE INDEX CONCURRENTLY'),
    ('drop_own_objects',        0, 0, 'DROP objects created by pg-tune');

insert or ignore into tune_vec_params (name, desc, measure) values
    ('ela_time',              'elapsed time',            'ms'),
    ('shared_hit_blocks',     'buffer cache hits',       'blocks'),
    ('shared_read_blocks',    'disk reads',              'blocks'),
    ('temp_read_blocks',      'temp file reads',         'blocks'),
    ('temp_written_blocks',   'temp file writes',        'blocks'),
    ('shared_i_o_read_time',  'I/O read time',           'ms'),
    ('temp_i_o_write_time',   'temp write time',         'ms');

-- Planned, add as features land:
--   ('disk_read_bytes',   'bytes read from disk'),
--   ('disk_write_bytes',  'bytes written to disk'),
--   ('temp_read_bytes',   'bytes read from temp files'),
--   ('temp_write_bytes',  'bytes written to temp files'),
--   ('plan_cost',         'planner total cost'),
--   ('plan_rows',         'planner estimated rows'),
--   ('actual_rows',       'actual rows processed');

