insert or ignore into tune_settings
    (category, name, value, default_value, desc) values

    -- ALLOWS: gating write actions
    ('ALLOWS', 'backup',                  '1', '1',
     'Snapshot the target database with pg_dump | zstd'),
    ('ALLOWS', 'restore',                 '0', '0',
     'Drop and restore from a tune_backups entry (DESTRUCTIVE)'),
    ('ALLOWS', 'set_session_guc',         '0', '0',
     'SET LOCAL in a rolled-back transaction'),
    ('ALLOWS', 'analyze',                 '0', '0',
     'ANALYZE on a table'),
    ('ALLOWS', 'create_index',            '0', '0',
     'CREATE INDEX (blocking)'),
    ('ALLOWS', 'create_index_concurrent', '0', '0',
     'CREATE INDEX CONCURRENTLY'),
    ('ALLOWS', 'drop_own_objects',        '0', '0',
     'DROP objects created by pg-tune'),

    -- SETTING: tune() defaults
    ('SETTING', 'default_cold_run',  '3',    '3',
     'warm-up runs before measuring, to fill the buffer cache'),
    ('SETTING', 'default_max_iters', '30',    '30',
     'maximum fix-apply-verify iterations'),
    ('SETTING', 'default_dry_run',   'true', 'true',
     'tune() proposes fixes but does not apply them');

insert into tune_vec_params (name, desc, measure, scope, raw_key) values
    ('ela_time',              'elapsed time',         'ms',     'root_meta', 'Execution Time'),
    ('shared_hit_blocks',     'buffer cache hits',    'blocks', 'root_plan', 'Shared Hit Blocks'),
    ('shared_read_blocks',    'disk reads',           'blocks', 'root_plan', 'Shared Read Blocks'),
    ('temp_read_blocks',      'temp file reads',      'blocks', 'root_plan', 'Temp Read Blocks'),
    ('temp_written_blocks',   'temp file writes',     'blocks', 'root_plan', 'Temp Written Blocks'),
    ('shared_i_o_read_time',  'I/O read time',        'ms',     'root_plan', 'Shared I/O Read Time'),
    ('temp_i_o_write_time',   'temp write time',      'ms',     'root_plan', 'Temp I/O Write Time');

-- Planned, add as features land:
--   ('disk_read_bytes',   'bytes read from disk'),
--   ('disk_write_bytes',  'bytes written to disk'),
--   ('temp_read_bytes',   'bytes read from temp files'),
--   ('temp_write_bytes',  'bytes written to temp files'),
--   ('plan_cost',         'planner total cost'),
--   ('plan_rows',         'planner estimated rows'),
--   ('actual_rows',       'actual rows processed');

