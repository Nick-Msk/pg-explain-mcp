/* create table tune_sessions (
    name           text    not null,
    ts             text    not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    is_terminated  integer not null default 0
                   check (is_terminated in (0, 1)),
    -- whatever else: sql_hash, target_db, who
    primary key (name, ts)
); */

create table tune_backups (
    id                 integer primary key autoincrement,
    ts                 text    not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    database           text    not null,
    path               text    not null,
    size_bytes         integer not null,
    sha256             text    not null,
    pg_version         text    not null,
    pg_explain_version text    not null,
    restored_ts        text,
    restored_by        text
);

create table tune_allows (
    action       text primary key,     -- 'create_index', 'set_guc', 'analyze', ...
    enabled      integer not null default 0
                 check (enabled in (0, 1)),
    default_enabled  integer not null default 0
                     check (default_enabled in (0, 1)),
    description  text not null
);

-- ---------------------------------------------------------------------------
-- Audit log
-- ---------------------------------------------------------------------------
-- Each applied (or rolled back) fix produces one tune_audit row. Numeric
-- metrics are stored separately, one row per metric per phase, so new
-- metrics (disk read, temp written, …) can be added by inserting into
-- tune_vec_params — no schema change required.

create table tune_audit (
    id            integer primary key autoincrement,
    ts            text    not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    session_id    text    not null,       -- opaque for now; FK to tune_sessions later
    action        text    not null,       -- 'set_session_guc', 'create_index', …
    target        text    not null,       -- table name, GUC name, index name
    payload       text    not null,       -- full SQL or GUC value
    applied       integer not null,       -- 1 applied, 0 rolled back
    issues_before text,                   -- JSON: issues[] before the fix
    issues_after  text,                   -- JSON: issues[] after the fix
    pg_explain_version text    not null,
    who           text    not null
);

create table tune_vec_params (
    name     text primary key,              -- 'ela_time', 'disk_read_bytes', …
    desc     text not null,
    measure  text not null,
    scope    text not null check (scope in ('root_meta', 'root_plan')),
    raw_key  text not null
);

create table tune_audit_vector (
    tune_audit_id  integer not null,
    phase          text    not null check (phase in ('B', 'A')),
    vec_name       text    not null,
    val            real,                  -- null when the metric is absent
    primary key (tune_audit_id, phase, vec_name),
    foreign key (tune_audit_id) references tune_audit (id) on delete cascade,
    foreign key (vec_name)      references tune_vec_params (name)
);

create index tune_audit_vector_lookup
    on tune_audit_vector (vec_name, phase);

