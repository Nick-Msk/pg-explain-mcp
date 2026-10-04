insert into tune_allows (action, enabled, description) values
    ('set_session_guc',    0, 'SET LOCAL work_mem / enable_* in a rolled-back transaction'),
    ('analyze',            0, 'ANALYZE on a table'),
    ('create_index',       0, 'CREATE INDEX (blocking)'),
    ('create_index_concurrent', 0, 'CREATE INDEX CONCURRENTLY'),
    ('drop_own_objects',   0, 'DROP objects created by pg-tune'),
    ('alter_role_guc',     0, 'ALTER ROLE ... SET ...'),
    ('alter_database_guc', 0, 'ALTER DATABASE ... SET ...')
    ('restore', 0, 'db restoration');

insert into tune_vec_params (name, desc) values
    ('ela_time', 'elapsed time, ms');

-- Planned, add as features land:
--   ('disk_read_bytes',   'bytes read from disk'),
--   ('disk_write_bytes',  'bytes written to disk'),
--   ('temp_read_bytes',   'bytes read from temp files'),
--   ('temp_write_bytes',  'bytes written to temp files'),
--   ('plan_cost',         'planner total cost'),
--   ('plan_rows',         'planner estimated rows'),
--   ('actual_rows',       'actual rows processed');

